#!/usr/bin/env python3
"""회귀 케이스 6건만 재실행 후 이전 pass2 결과와 비교."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import time
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

REGRESSION_CASES = [
    "0324 의정부지방법원 고양지원 경매1계",
    "0320 대전지방법원 공주지원 경매2계",
    "0326 대구지방법원 의성지원 경매1계",
    "0326 춘천지방법원 속초지원 경매2계",
    "0327 대구지방법원 서부지원 경매6계",
    "0325 의정부지방법원 경매8계",
]


def main() -> int:
    pairs = bl.collect_pairs()
    targets = [p for p in pairs if p["case_id"] in set(REGRESSION_CASES)]
    bl.log(f"Target: {len(targets)} regression cases (of {len(pairs)} total)")
    if not targets:
        bl.log("no matching pairs")
        return 1

    # Clear LLM cache for these cases (by key they'd be scattered — safest: reset entirely).
    try:
        cache_path = KB_DIR / "cache" / "llm_cache.json"
        if cache_path.exists():
            backup = cache_path.with_suffix(".json.prererun.bak")
            shutil.copy2(cache_path, backup)
            cache_path.write_text("{}", encoding="utf-8")
            bl.log(f"Reset llm_cache.json (backup: {backup.name})")
    except Exception as ce:
        bl.log(f"Could not reset llm cache: {ce}")

    import llm_refiner  # noqa: F401
    importlib.reload(llm_refiner)
    import render_final_notice as _rfn  # noqa: F401
    importlib.reload(_rfn)

    # Pull previous scores from runs.jsonl for comparison.
    runs = memory.load_runs()
    prev_scores: dict[str, float] = {}
    for r in runs:
        cid = r.get("case_id", "")
        stripped = cid.replace("#pass2", "").replace("#pass1", "")
        if stripped in set(REGRESSION_CASES):
            prev_scores.setdefault(stripped, []).append((r.get("timestamp", ""), r.get("score")))
    # latest per case
    latest: dict[str, float] = {}
    for cid, lst in prev_scores.items():
        lst.sort(key=lambda t: t[0])
        if lst:
            latest[cid] = lst[-1][1]

    results = []
    for i, pair in enumerate(targets, 1):
        bl.log(f"[{i}/{len(targets)}] {pair['case_id']}")
        res = bl._process_case_using(
            _rfn, pair, pass_label="rerun", render_pdf_for_case=False
        )
        prev = latest.get(pair["case_id"])
        new = res.get("score")
        delta = (new - prev) if (prev is not None and new is not None) else None
        res["prev_score"] = prev
        res["delta"] = delta
        results.append(res)
        prev_s = f"{prev:.3f}" if prev is not None else "?"
        new_s = f"{new:.3f}" if new is not None else "?"
        d_s = f"{delta:+.3f}" if delta is not None else "?"
        bl.log(f"   prev={prev_s} new={new_s} delta={d_s}")

    out = ROOT / "output" / "learning_batch" / "rerun_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    bl.log(f"Wrote: {out}")

    ok = [r for r in results if r.get("score") is not None]
    if ok:
        mean_new = sum(r["score"] for r in ok) / len(ok)
        ok_prev = [r for r in results if r.get("prev_score") is not None]
        mean_prev = sum(r["prev_score"] for r in ok_prev) / len(ok_prev) if ok_prev else 0
        bl.log(f"Rerun mean: {mean_new:.3f}  (prev mean: {mean_prev:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
