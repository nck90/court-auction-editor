#!/usr/bin/env python3
"""하위 5개 케이스 재실행 (HWP parser recovery 효과 측정)."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
KB_DIR = ROOT / "knowledge"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ROOT / "tools"))

os.environ.setdefault("USE_LLM_REFINER", "1")
os.environ.setdefault("OLLAMA_URL", "https://ollama.hyphen.it.com/api/generate")
os.environ.setdefault("OLLAMA_MODEL", "gemma3:4b")
os.environ.setdefault("OLLAMA_UA", "curl/8.1.2 (court-auction-learner)")
os.environ.setdefault("OLLAMA_TIMEOUT", "60")
os.environ.setdefault("LLM_LOOP_PROMOTE_THRESHOLD", "0.85")

import batch_learn as bl  # noqa: E402
import memory  # noqa: E402

TARGETS = [
    "0327 인천지방법원 부천지원 경매7계",
    "0331 수원지방법원 성남지원 경매2계",
    "0326 춘천지방법원 속초지원 경매2계",
    "0331 서울서부지방법원 경매7계",
    "0326 대구지방법원 의성지원 경매1계",
]


def main() -> int:
    pairs = bl.collect_pairs()
    targets = [p for p in pairs if p["case_id"] in set(TARGETS)]
    bl.log(f"Targets: {len(targets)}")

    # Reset caches so format_entry runs fresh.
    for cache_name in ("llm_cache.json", "retrieval_query.json"):
        cp = KB_DIR / "cache" / cache_name
        if cp.exists():
            shutil.copy2(cp, cp.with_suffix(".json.bot.bak"))
            cp.write_text("{}", encoding="utf-8")

    # Reload parser + refiner
    import court_auction_editor
    importlib.reload(court_auction_editor)
    import llm_refiner
    importlib.reload(llm_refiner)
    import render_final_notice as _rfn
    importlib.reload(_rfn)
    import batch_learn as _bl2
    importlib.reload(_bl2)

    runs = memory.load_runs()
    latest: dict[str, tuple[str, float]] = {}
    for r in runs:
        cid = r.get("case_id", "").replace("#pass2", "").replace("#pass1", "").replace("#rerun", "")
        if cid in set(TARGETS):
            prev = latest.get(cid)
            if not prev or r.get("timestamp", "") > prev[0]:
                latest[cid] = (r.get("timestamp", ""), r.get("score"))

    results = []
    for i, pair in enumerate(targets, 1):
        bl.log(f"[{i}/{len(targets)}] {pair['case_id']}")
        res = _bl2._process_case_using(
            _rfn, pair, pass_label="botfix", render_pdf_for_case=False
        )
        prev = latest.get(pair["case_id"], (None, None))[1]
        new = res.get("score")
        delta = (new - prev) if (prev is not None and new is not None) else None
        res["prev_score"] = prev
        res["delta"] = delta
        results.append(res)
        bl.log(f"   prev={prev!s} new={new!s} delta={delta!s}")

    out = ROOT / "output" / "learning_batch" / "bottom_fix_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    scored = [r for r in results if r.get("score") is not None]
    if scored:
        mean_new = sum(r["score"] for r in scored) / len(scored)
        prev_scored = [r for r in results if r.get("prev_score") is not None]
        mean_prev = sum(r["prev_score"] for r in prev_scored) / len(prev_scored) if prev_scored else 0
        bl.log(f"botfix mean: {mean_new:.3f}  (prev: {mean_prev:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
