#!/usr/bin/env python3
"""Report learning loop state: scores over time, top failure patterns.

사용:
    python3 tools/learning_report.py
    python3 tools/learning_report.py --days 30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from memory import RUNS_PATH, CORRECTIONS_PATH, PROMOTIONS_PATH, LESSONS_PATH  # noqa: E402

KB_DIR = ROOT / "knowledge"
REPORT_MD = KB_DIR / "report.md"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # allow both with/without tz
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


def _fmt_score(v):
    return f"{v:.3f}" if isinstance(v, (int, float)) else "-"


def build_report(days: int = 30) -> str:
    runs = _read_jsonl(RUNS_PATH)
    corrections = _read_jsonl(CORRECTIONS_PATH)
    promotions = _read_jsonl(PROMOTIONS_PATH)

    now = datetime.now()
    cutoff = now - timedelta(days=days)

    recent_runs = [r for r in runs if (_parse_ts(r.get("timestamp")) or now) >= cutoff]
    recent_runs_scored = [r for r in recent_runs if isinstance(r.get("score"), (int, float))]

    # Hourly/daily aggregation
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in recent_runs_scored:
        dt = _parse_ts(r.get("timestamp")) or now
        key = dt.strftime("%Y-%m-%d")
        buckets[key].append(float(r["score"]))
    daily = sorted(
        (k, sum(v) / len(v), len(v)) for k, v in buckets.items()
    )

    # Top failure patterns: mismatches' cell-field
    field_fail: Counter = Counter()
    for r in runs:
        for mm in r.get("mismatches") or []:
            cell = mm.get("cell") or ""
            parts = cell.split("|")
            field = parts[2] if len(parts) >= 3 else cell
            field_fail[field] += 1

    case_score: dict[str, list[float]] = defaultdict(list)
    for r in runs:
        if isinstance(r.get("score"), (int, float)):
            case_score[r.get("case_id", "?")].append(float(r["score"]))
    worst_cases = sorted(
        ((k, sum(v) / len(v), len(v)) for k, v in case_score.items()),
        key=lambda t: t[1],
    )[:5]

    # Improvement: compare first quartile vs last quartile of scored runs.
    improvement = None
    scored = [r for r in runs if isinstance(r.get("score"), (int, float))]
    if len(scored) >= 8:
        n = len(scored) // 4
        first_avg = sum(r["score"] for r in scored[:n]) / n
        last_avg = sum(r["score"] for r in scored[-n:]) / n
        improvement = (last_avg - first_avg, first_avg, last_avg)

    all_scores = [r["score"] for r in scored]
    overall_avg = sum(all_scores) / len(all_scores) if all_scores else None

    # Correction cell hotspots
    corr_fields: Counter = Counter()
    for c in corrections:
        parts = (c.get("cell_key") or "").split("|")
        corr_fields[parts[2] if len(parts) >= 3 else "?"] += 1

    lines: list[str] = []
    lines.append("# 학습 루프 리포트")
    lines.append(f"_생성: {now.strftime('%Y-%m-%d %H:%M:%S')}  · 최근 {days}일_")
    lines.append("")
    lines.append("## 요약")
    lines.append(f"- 총 실행 건수: **{len(runs)}**  (최근 {days}일 {len(recent_runs)})")
    lines.append(f"- 전체 평균 스코어: **{_fmt_score(overall_avg)}**")
    lines.append(f"- 누적 피드백(correction): **{len(corrections)}**")
    lines.append(f"- 승격된 예시: **{len(promotions)}**")
    lines.append(f"- lessons.md: {'있음' if LESSONS_PATH.exists() else '없음'}")
    if improvement is not None:
        delta, first, last = improvement
        lines.append(
            f"- 개선률(초반 25% vs 최근 25%): **{delta:+.3f}**  "
            f"({_fmt_score(first)} → {_fmt_score(last)})"
        )
    lines.append("")

    lines.append("## 날짜별 평균 스코어")
    if daily:
        lines.append("| 날짜 | 평균 스코어 | 실행 수 |")
        lines.append("|---|---|---|")
        for day, avg, n in daily[-14:]:
            lines.append(f"| {day} | {avg:.3f} | {n} |")
    else:
        lines.append("_(데이터 없음)_")
    lines.append("")

    lines.append("## 셀 필드별 실패 패턴 TOP")
    if field_fail:
        lines.append("| 필드 | 실패 건수 |")
        lines.append("|---|---|")
        for field, cnt in field_fail.most_common(10):
            lines.append(f"| {field} | {cnt} |")
    else:
        lines.append("_(데이터 없음)_")
    lines.append("")

    lines.append("## 가장 어려운 케이스")
    if worst_cases:
        lines.append("| case_id | 평균 스코어 | 실행 수 |")
        lines.append("|---|---|---|")
        for cid, avg, n in worst_cases:
            lines.append(f"| {cid} | {avg:.3f} | {n} |")
    else:
        lines.append("_(데이터 없음)_")
    lines.append("")

    lines.append("## 사용자 수정 핫스팟")
    if corr_fields:
        lines.append("| 필드 | correction 건수 |")
        lines.append("|---|---|")
        for f, cnt in corr_fields.most_common(10):
            lines.append(f"| {f} | {cnt} |")
    else:
        lines.append("_(데이터 없음)_")
    lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Learning loop report")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--output", type=Path, default=REPORT_MD)
    args = p.parse_args()
    report = build_report(args.days)
    print(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"\n저장: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
