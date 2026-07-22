#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unified_search.py - PubMed + Google Scholar 双库合并检索（含去重）。

默认行为：
  - 同时检索 PubMed（官方 E-utilities API）与 Google Scholar 镜像（灯塔 JSON 优先）。
  - 默认开启「跨库去重」：以 DOI 或归一化标题为键，合并两端结果并剔除重复项。
  - 可用 --no-dedup 关闭去重（仍同时检索两库，但保留重复项）。

输出 JSON（stdout）：
  {
    "ok": true,
    "query": "...",
    "dedup_enabled": true,
    "dedup_note": "已开启多库去重，可手动关闭（--no-dedup）",
    "pubmed_count": N,
    "scholar_count": M,
    "merged_count": N+M,
    "deduped_count": K,
    "removed_count": (N+M)-K,
    "pubmed":   { "ok": ..., "source": "pubmed", "count": N, "articles": [...] },
    "scholar":  { "ok": ..., "source": "dotaindex", "count": M, "results": [...] },
    "results":  [ 统一记录 ... ]
  }

每条统一记录字段：
  db, title, authors, year, venue, snippet, citations, url, pdf_url,
  doi, doi_url, pmid, pubmed_url

所有字段均来自真实响应，绝不编造。
"""

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PUBMED_SCRIPT = os.path.join(HERE, "pubmed_search.py")
SCHOLAR_SCRIPT = os.path.join(HERE, "scholar_search.py")

DEDUP_ON_NOTE = "已开启多库去重，可手动关闭（--no-dedup）"
DEDUP_OFF_NOTE = "未开启去重，已保留跨库重复项（如需去重去掉 --no-dedup 即可）"


def _norm_title(title):
    """归一化标题用于去重：转小写、仅保留字母(含中文)/数字。"""
    if not title:
        return ""
    t = title.lower()
    # \w 在 Python 3 对 str 默认按 Unicode，可正确保留中文
    t = re.sub(r"[^\w]", "", t)
    return t


def _run_json(script, argv, timeout=120):
    """运行兄弟脚本，解析 stdout 的 JSON；失败返回 (None, errmsg)。"""
    try:
        proc = subprocess.run(
            [sys.executable, script] + argv,
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"运行 {os.path.basename(script)} 失败：{e}"
    if proc.returncode != 0 and not proc.stdout.strip():
        return None, (proc.stderr or "").strip()[:300] or f"{os.path.basename(script)} 退出码 {proc.returncode}"
    try:
        return json.loads(proc.stdout), ""
    except json.JSONDecodeError:
        return None, f"{os.path.basename(script)} 输出非 JSON：{proc.stdout[:200]}"


def _to_unified_pubmed(articles):
    out = []
    for a in articles or []:
        authors = a.get("authors", [])
        if isinstance(authors, list):
            authors = ", ".join(authors)
        out.append({
            "db": "pubmed",
            "title": a.get("title", ""),
            "authors": authors or "",
            "year": (a.get("pubdate", "") or "")[:4],
            "venue": a.get("journal", ""),
            "snippet": a.get("abstract", ""),
            "citations": 0,
            "url": a.get("pubmed_url", ""),
            "pdf_url": "",
            "doi": a.get("doi", ""),
            "doi_url": a.get("doi_url", ""),
            "pmid": a.get("pmid", ""),
            "pubmed_url": a.get("pubmed_url", ""),
        })
    return out


def _to_unified_scholar(results):
    out = []
    for r in results or []:
        out.append({
            "db": "scholar",
            "title": r.get("title", ""),
            "authors": r.get("authors", ""),
            "year": str(r.get("year", "")),
            "venue": r.get("venue", ""),
            "snippet": r.get("snippet", ""),
            "citations": int(r.get("citations", 0) or 0),
            "url": r.get("url", ""),
            "pdf_url": r.get("pdf_url", ""),
            "doi": "",
            "doi_url": "",
            "pmid": "",
            "pubmed_url": "",
        })
    return out


def dedup(records, enabled):
    """按 DOI 或归一化标题去重。enabled=False 时原样返回。"""
    if not enabled:
        return records, 0
    seen = set()
    out = []
    removed = 0
    for rec in records:
        doi = (rec.get("doi") or "").strip().lower()
        key = doi if doi else _norm_title(rec.get("title", ""))
        if key and key in seen:
            removed += 1
            continue
        seen.add(key)
        out.append(rec)
    return out, removed


def main():
    ap = argparse.ArgumentParser(
        description="PubMed + Google Scholar 双库合并检索（默认去重）")
    ap.add_argument("--query", required=True, help="检索词（同时用于 PubMed 与 Scholar）")
    ap.add_argument("--num", type=int, default=10, help="每库返回条数（默认 10，最大 50）")
    ap.add_argument("--no-dedup", action="store_true",
                    help="关闭跨库去重（默认开启）。开启去重时无需任何参数")
    ap.add_argument("--sort", default="relevance", choices=["relevance", "date"],
                    help="排序：relevance 相关度 / date 日期")
    ap.add_argument("--ylo", help="起始年份（如 2020），按年份下限过滤")
    ap.add_argument("--source", default="auto",
                    help="Scholar 数据源；默认 auto（灯塔→烂番薯→香港→官方）")
    ap.add_argument("--browser", action="store_true",
                    help="Scholar 被拦截时改用 Playwright 无头浏览器兜底")
    ap.add_argument("--api-key", help="NCBI API key（提升限速，可选；亦可用环境变量 NCBI_API_KEY）")
    ap.add_argument("--email", help="你的邮箱（NCBI 礼貌字段，可选）")
    args = ap.parse_args()

    num = max(1, min(args.num, 50))
    dedup_enabled = not args.no_dedup

    # 1) PubMed
    pub_argv = ["--query", args.query, "--retmax", str(num)]
    if args.sort == "date":
        pub_argv += ["--sort", "pub_date"]
    if args.ylo:
        pub_argv += ["--mindate", args.ylo]
    if args.api_key:
        pub_argv += ["--api-key", args.api_key]
    if args.email:
        pub_argv += ["--email", args.email]
    pub_data, pub_err = _run_json(PUBMED_SCRIPT, pub_argv)
    if pub_data is None:
        pub_records = []
        pub_ok = False
        pub_note = pub_err
    else:
        pub_ok = bool(pub_data.get("ok"))
        pub_note = pub_data.get("hint") or pub_data.get("error") or ""
        pub_records = _to_unified_pubmed(pub_data.get("articles", []))

    # 2) Scholar（auto 优先级回退）
    sch_argv = ["--query", args.query, "--num", str(num),
                "--source", args.source, "--sort", args.sort]
    if args.ylo:
        sch_argv += ["--ylo", args.ylo]
    if args.browser:
        sch_argv += ["--browser"]
    sch_data, sch_err = _run_json(SCHOLAR_SCRIPT, sch_argv)
    if sch_data is None:
        sch_records = []
        sch_ok = False
        sch_note = sch_err
        sch_source = ""
    else:
        sch_ok = bool(sch_data.get("ok"))
        sch_note = sch_data.get("note") or sch_data.get("error") or ""
        sch_source = sch_data.get("source", "")
        sch_records = _to_unified_scholar(sch_data.get("results", []))

    # 3) 合并 + 去重（PubMed 优先置于前）
    merged = pub_records + sch_records
    deduped, removed = dedup(merged, dedup_enabled)

    out = {
        "ok": pub_ok or sch_ok,
        "query": args.query,
        "dedup_enabled": dedup_enabled,
        "dedup_note": DEDUP_OFF_NOTE if args.no_dedup else DEDUP_ON_NOTE,
        "pubmed_count": len(pub_records),
        "scholar_count": len(sch_records),
        "merged_count": len(merged),
        "deduped_count": len(deduped),
        "removed_count": removed,
        "pubmed": {
            "ok": pub_ok, "source": "pubmed",
            "count": len(pub_records), "note": pub_note,
            "articles": pub_data.get("articles", []) if pub_data else [],
        },
        "scholar": {
            "ok": sch_ok, "source": sch_source,
            "count": len(sch_records), "note": sch_note,
            "results": sch_data.get("results", []) if sch_data else [],
        },
        "results": deduped,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
