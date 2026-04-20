#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from overlay_template_pdf import normalize_entry_for_overlay
from render_final_notice import GROUP_ORDER, format_entry, parse_case_sort_key


def build_payload(data_json: Path) -> dict:
    payload = json.loads(data_json.read_text(encoding="utf-8"))
    groups = {group: [] for group in GROUP_ORDER}
    for entry in payload["entries"]:
        base = format_entry(entry)
        formatted = normalize_entry_for_overlay(base)
        groups[base["group"]].append(
            {
                "case": formatted["case"],
                "item": formatted["item"],
                "location": formatted["location"],
                "usage": formatted["usage"],
                "price": formatted["price"],
                "note": formatted["note"],
            }
        )
    for group in GROUP_ORDER:
        groups[group] = sorted(groups[group], key=lambda row: parse_case_sort_key(row["case"], row["item"]))
    return {"groups": groups}


def main() -> int:
    parser = argparse.ArgumentParser(description="InDesign 재구성용 그룹 페이로드를 생성합니다.")
    parser.add_argument("data_json", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_payload(args.data_json)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
