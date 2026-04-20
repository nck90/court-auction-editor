#!/usr/bin/env python3
"""pass1 결과를 재사용하고 distill + pass2 + 비교 리포트만 실행.

어제 pass1(25건)은 끝났지만 pass2/report가 미완. 같은 25건을 pass1 재실행 없이
다시 돌리기 위한 경량 래퍼. runs.jsonl 에서 pass1 결과를 복원한다.
"""
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

# Lower promotion threshold so pass1 top cases can promote on pass2 re-run.
os.environ.setdefault("LLM_LOOP_PROMOTE_THRESHOLD", "0.85")

import batch_learn as bl  # noqa: E402
import memory  # noqa: E402


def load_pass1_from_runs() -> list[dict]:
    """Reconstruct pass1 results from knowledge/runs.jsonl."""
    runs = memory.load_runs()
    # pass1 = 2026-04-19 batch runs
    pass1 = [r for r in runs if r.get("timestamp", "").startswith("2026-04-19T20")]
    # map to batch_learn result shape
    out: list[dict] = []
    for r in pass1:
        cid = r.get("case_id", "").replace("#pass1", "")
        out.append({
            "case_id": cid,
            "slug": bl._slug(cid),
            "pass": "pass1",
            "ok": True,
            "score": r.get("score"),
            "mismatches": r.get("mismatches") or [],
            "cell_scores": r.get("cell_scores") or [],
            "source": (r.get("source") if "source" in r else "pdf"),
            "error": "",
        })
    return out


def main() -> int:
    pairs = bl.collect_pairs()
    bl.log(f"Collected {len(pairs)} pairs")

    # 1) load pass1
    first = load_pass1_from_runs()
    bl.log(f"Pass1 reloaded from runs.jsonl: {len(first)} cases")

    # 2) distill
    corr = memory.load_corrections()
    bl.log(f"Corrections on disk: {len(corr)}")
    try:
        distill_info = memory.distill_lessons(force=True)
        bl.log(f"Distill: {distill_info}")
    except Exception as de:
        bl.log(f"Distill failed: {de}")
        distill_info = {"ok": False, "error": str(de)}

    lessons_preview = ""
    if memory.LESSONS_PATH.exists():
        lessons_preview = memory.LESSONS_PATH.read_text(encoding="utf-8")

    # 3) reset caches
    try:
        cache_path = KB_DIR / "cache" / "llm_cache.json"
        if cache_path.exists():
            backup = cache_path.with_suffix(".json.pass1.bak")
            shutil.copy2(cache_path, backup)
            cache_path.write_text("{}", encoding="utf-8")
            bl.log(f"Reset llm_cache.json (backup: {backup.name})")
    except Exception as ce:
        bl.log(f"Could not reset llm cache: {ce}")
    try:
        q = KB_DIR / "cache" / "retrieval_query.json"
        if q.exists():
            q.write_text("{}", encoding="utf-8")
    except Exception:
        pass

    # 4) reload refiner + final_notice
    import llm_refiner  # noqa: F401
    importlib.reload(llm_refiner)
    import render_final_notice as _rfn  # noqa: F401
    importlib.reload(_rfn)

    # 5) pass2
    bl.log("=== PASS 2: post-lessons ===")
    low_ids = {r["case_id"] for r in first if r.get("score") is not None and r["score"] < 0.9}
    second: list[dict] = []
    for i, pair in enumerate(pairs, 1):
        bl.log(f"[P2 {i}/{len(pairs)}] {pair['case_id']}")
        render_pdf_flag = pair["case_id"] in low_ids
        res = bl._process_case_using(
            _rfn, pair, pass_label="pass2", render_pdf_for_case=render_pdf_flag
        )
        second.append(res)

    # 6) promotions list
    promotions: list[str] = []
    if memory.PROMOTIONS_PATH.exists():
        for line in memory.PROMOTIONS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                promotions.append(
                    f"{rec.get('case_id')} → {rec.get('path')} (score={rec.get('score',0):.3f})"
                )
            except Exception:
                continue

    # 7) report
    bl.write_report(first, second, promotions, distill_info, lessons_preview)
    (bl.OUTPUT_ROOT / "pass2_only_results.json").write_text(
        json.dumps(second, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    bl.log(f"Done. Pass1 mean={sum(r['score'] for r in first if r.get('score'))/len(first):.3f} "
           f"Pass2 mean={sum(r['score'] for r in second if r.get('score'))/max(1,sum(1 for r in second if r.get('score') is not None)):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
