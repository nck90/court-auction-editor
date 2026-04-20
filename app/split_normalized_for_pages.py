#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from render_final_notice import format_entry


PAGE1_CAPACITY = {
    "기타": 4,
    "대지/임야/전답": 2,
    "상가/오피스텔,근린시설": 1,
    "연립주택/다세대/빌라": 1,
    "단독주택,다가구주택": 0,
    "아파트": 99,
}


def split_entries(data: dict) -> tuple[dict, dict]:
    grouped_counts = {k: 0 for k in PAGE1_CAPACITY}
    page1 = {"entries": []}
    page2 = {"entries": []}
    for entry in data["entries"]:
        group = format_entry(entry)["group"]
        cap = PAGE1_CAPACITY.get(group, 99)
        if grouped_counts.get(group, 0) < cap:
            page1["entries"].append(entry)
            grouped_counts[group] = grouped_counts.get(group, 0) + 1
        else:
            page2["entries"].append(entry)
    return page1, page2


def main() -> int:
    parser = argparse.ArgumentParser(description="정규화 JSON을 페이지별로 분할합니다.")
    parser.add_argument("input_json", type=Path)
    parser.add_argument("--page1", type=Path, required=True)
    parser.add_argument("--page2", type=Path, required=True)
    args = parser.parse_args()

    data = json.loads(args.input_json.read_text(encoding="utf-8"))
    page1, page2 = split_entries(data)
    args.page1.write_text(json.dumps(page1, ensure_ascii=False, indent=2), encoding="utf-8")
    args.page2.write_text(json.dumps(page2, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.page1)
    print(args.page2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
