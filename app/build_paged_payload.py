#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from overlay_template_pdf import normalize_entry_for_overlay
from render_final_notice import GROUP_ORDER, format_entry, parse_case_sort_key


PAGE1_CAPACITY = {
    "기타": 4,
    "대지/임야/전답": 2,
    "상가/오피스텔,근린시설": 1,
    "연립주택/다세대/빌라": 1,
    "단독주택,다가구주택": 0,
    "아파트": 99,
}


def make_rows(data_json: Path) -> dict[str, list[list[str]]]:
    payload = json.loads(data_json.read_text(encoding="utf-8"))
    groups = {group: [] for group in GROUP_ORDER}
    for raw in payload["entries"]:
        base = format_entry(raw)
        normalized = normalize_entry_for_overlay(base)
        groups[base["group"]].append(
            [
                normalized["case"],
                normalized["item"],
                normalized["location"],
                normalized["usage"],
                normalized["price"],
                normalized["note"],
            ]
        )
    for group in GROUP_ORDER:
        groups[group] = sorted(groups[group], key=lambda row: parse_case_sort_key(row[0], row[1]))
    return groups


def build_pages(groups: dict[str, list[list[str]]]) -> dict:
    page1 = {}
    page2 = {}
    for group in GROUP_ORDER:
        rows = groups[group]
        cap = PAGE1_CAPACITY.get(group, len(rows))
        page1[group] = rows[:cap]
        page2[group] = rows[cap:]
    return {"pages": [page1, page2]}


def main() -> int:
    parser = argparse.ArgumentParser(description="InDesign 2페이지 분할용 payload를 생성합니다.")
    parser.add_argument("data_json", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_pages(make_rows(args.data_json))
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
