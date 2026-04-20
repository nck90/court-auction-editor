#!/usr/bin/env python3
"""Distill corrections.jsonl into lessons.md via the LLM.

사용:
    python3 tools/distill.py [--force]

--force 옵션은 쿨다운/임계치 무시.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from memory import distill_lessons, load_corrections, LESSONS_PATH  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Distill corrections into lessons.")
    p.add_argument("--force", action="store_true", help="무시 cooldown & 최소 수정 수")
    args = p.parse_args()

    corrections = load_corrections()
    print(f"누적 corrections: {len(corrections)}개")
    print(f"lessons.md: {LESSONS_PATH} ({'exists' if LESSONS_PATH.exists() else 'missing'})")

    result = distill_lessons(force=args.force)
    print(f"distill 결과: {result}")
    if not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
