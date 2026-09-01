#!/usr/bin/env python3
"""Backfill existing report_0730_*.json into the daytrade tracker site."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

web = Path.home() / ".hermes" / "www" / "daytrade-tracker"
rep = web / "data" / "reports"
rep.mkdir(parents=True, exist_ok=True)
cache = Path.home() / ".hermes" / "cache" / "finance"
entries = []

for f in sorted(cache.glob("report_0730_20*.json")):
    p = json.loads(f.read_text(encoding="utf-8"))
    d = str(p.get("screen_date") or f.stem.replace("report_0730_", ""))
    p["screen_date"] = d
    p["tracker"] = {
        "id": d,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "backfill",
    }
    (rep / f"{d}.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    md = p.get("report_text") or ""
    if md:
        (rep / f"{d}.md").write_text(md, encoding="utf-8")
    gate = p.get("gate") or {}
    idx = p.get("index") or {}
    results = p.get("results") or []
    tops = []
    for r in results[:5]:
        tops.append(f"{r.get('code')} {r.get('name')}")
    entries.append(
        {
            "date": d,
            "generated_at": p.get("generated_at"),
            "gate_tag": gate.get("tag"),
            "gate_advice": gate.get("advice"),
            "index_change": idx.get("change"),
            "index_change_pct": idx.get("change_pct"),
            "index_label": idx.get("label") or idx.get("source"),
            "count": len(results),
            "relaxed": bool((p.get("screen_meta") or {}).get("relaxed")),
            "top_codes": tops,
            "file": f"data/reports/{d}.json",
        }
    )
    print("ok", d, len(results))

entries.sort(key=lambda x: x["date"], reverse=True)
if entries:
    latest = json.loads((rep / f"{entries[0]['date']}.json").read_text(encoding="utf-8"))
    (rep / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    if latest.get("report_text"):
        (rep / "latest.md").write_text(latest["report_text"], encoding="utf-8")

doc = {
    "updated_at": datetime.now().isoformat(timespec="seconds"),
    "title": "TW Daytrade Desk",
    "timezone": "Asia/Taipei",
    "entries": entries,
    "count": len(entries),
}
(web / "data" / "index.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
print("index", len(entries), [e["date"] for e in entries])

# 與 07:30 相同：網頁只留近 6 日
sys.path.insert(0, str(Path.home() / ".hermes" / "scripts" / "finance"))
from daytrade_report_0730 import prune_tracker_reports  # noqa: E402

pruned = prune_tracker_reports()
print("pruned", pruned)
