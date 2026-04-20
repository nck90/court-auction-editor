#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from render_final_notice import format_entry


PAGE_SLOT_PLANS = [
    [
        ("기타", 1),
        ("대지/임야/전답", 2),
        ("상가/오피스텔,근린시설", 1),
        ("아파트", 3),
        ("연립주택/다세대/빌라", 1),
        ("단독주택,다가구주택", 1),
    ],
    [
        ("기타", 1),
        ("대지/임야/전답", 1),
        ("상가/오피스텔,근린시설", 1),
        ("기타", 4),
        ("기타", 1),
        (None, 0),
    ],
]


def split_to_slot_pages(data: dict) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for entry in data["entries"]:
        grouped[format_entry(entry)["group"]].append(entry)
    meta = {
        "court_line": data.get("court_line", ""),
        "auction_datetime": data.get("auction_datetime", ""),
        "decision_datetime": data.get("decision_datetime", ""),
        "officer_line": data.get("officer_line", ""),
    }

    # `기타`는 밀도가 높아서 일반 cap 분배보다 수동 배치가 더 안정적이다.
    if len(grouped["기타"]) >= 6:
        etc = grouped["기타"]
        page1 = {
            "meta": meta,
            "slots": [
                {"group": "기타", "entries": etc[0:1]},
                {"group": "대지/임야/전답", "entries": grouped["대지/임야/전답"][:2]},
                {"group": "상가/오피스텔,근린시설", "entries": grouped["상가/오피스텔,근린시설"][:1]},
                {"group": "아파트", "entries": grouped["아파트"][:3]},
                {"group": "연립주택/다세대/빌라", "entries": grouped["연립주택/다세대/빌라"][:1]},
                {"group": "단독주택,다가구주택", "entries": grouped["단독주택,다가구주택"][:1]},
            ]
        }
        page2 = {
            "meta": meta,
            "slots": [
                {"group": "기타", "entries": etc[1:2]},
                {"group": "대지/임야/전답", "entries": grouped["대지/임야/전답"][2:3]},
                {"group": "상가/오피스텔,근린시설", "entries": grouped["상가/오피스텔,근린시설"][1:2]},
                {"group": "기타", "entries": [etc[2], etc[3], etc[4]]},
                {"group": "기타", "entries": [etc[5], etc[6]]},
                {"group": None, "entries": []},
            ]
        }
        return [page1, page2]

    pages: list[dict] = []
    for plan in PAGE_SLOT_PLANS:
        slots = []
        for group, limit in plan:
            if not group or limit <= 0:
                slots.append({"group": None, "entries": []})
                continue
            entries = grouped[group][:limit]
            grouped[group] = grouped[group][limit:]
            slots.append({"group": group, "entries": entries})
        pages.append({"meta": meta, "slots": slots})
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description="슬롯 단위 페이지 페이로드를 생성합니다.")
    parser.add_argument("input_json", type=Path)
    parser.add_argument("--page1", type=Path, required=True)
    parser.add_argument("--page2", type=Path, required=True)
    args = parser.parse_args()

    data = json.loads(args.input_json.read_text(encoding="utf-8"))
    page1, page2 = split_to_slot_pages(data)
    args.page1.write_text(json.dumps(page1, ensure_ascii=False, indent=2), encoding="utf-8")
    args.page2.write_text(json.dumps(page2, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.page1)
    print(args.page2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
