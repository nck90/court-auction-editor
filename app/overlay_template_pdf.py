#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz

from render_final_notice import GROUP_ORDER, format_entry


FONT_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/NotoSansGothic-Regular.ttf",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
]
SCALE = 3
COLUMN_FONT_FACTORS = [0.84, 0.86, 0.94, 0.86, 0.88, 0.8]

SECTION_LAYOUTS = {
    "기타": {
        "clear": fitz.Rect(7, 141, 343, 468),
        "x": [14, 54, 69, 218, 257, 297],
        "w": [38, 11, 145, 34, 38, 35],
        "y": 148,
        "size": 6.5,
    },
    "아파트": {
        "clear": fitz.Rect(7, 462, 343, 541),
        "x": [14, 56, 74, 227, 268, 315],
        "w": [40, 13, 150, 38, 43, 20],
        "y": 480,
        "size": 7.0,
    },
    "연립주택/다세대/빌라": {
        "clear": fitz.Rect(7, 537, 343, 581),
        "x": [14, 56, 74, 227, 268, 315],
        "w": [40, 13, 150, 38, 43, 20],
        "y": 554,
        "size": 6.8,
    },
    "대지/임야/전답": {
        "clear": fitz.Rect(352, 58, 700, 343),
        "x": [360, 399, 414, 564, 603, 645],
        "w": [36, 11, 146, 34, 39, 32],
        "y": 93,
        "size": 6.3,
    },
    "상가/오피스텔,근린시설": {
        "clear": fitz.Rect(352, 341, 700, 424),
        "x": [360, 399, 414, 566, 605, 646],
        "w": [36, 11, 148, 34, 39, 31],
        "y": 365,
        "size": 6.3,
    },
    "단독주택,다가구주택": {
        "clear": fitz.Rect(352, 420, 700, 531),
        "x": [360, 399, 414, 566, 605, 646],
        "w": [36, 11, 148, 34, 39, 31],
        "y": 438,
        "size": 6.3,
    },
}


def resolve_font_file() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("No usable Korean font found")


FONT_FILE = resolve_font_file()
FITZ_FONT = fitz.Font(fontfile=FONT_FILE)


def wrap_for_width(text: str, font_size: float, width_pt: float) -> list[str]:
    lines: list[str] = []
    for raw_part in text.split("\n"):
        part = raw_part.strip()
        if not part:
            lines.append("")
            continue
        current = ""
        for char in part:
            candidate = current + char
            width = FITZ_FONT.text_length(candidate, fontsize=font_size)
            if current and width > width_pt:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


def line_height_pt(font_size: float, leading_factor: float = 0.96) -> float:
    return max(5.2, font_size * leading_factor)


def estimate_height(
    values: list[str],
    font_sizes: list[float],
    widths: list[float],
    font_size: float,
    leading_factor: float,
) -> float:
    line_counts = []
    for value, size, width in zip(values, font_sizes, widths):
        line_counts.append(len(wrap_for_width(value, size, width - 1.5)))
    return max(line_counts) * line_height_pt(font_size, leading_factor) + 1.8


def normalize_case_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    markers = [line for line in lines if line.startswith("[")]
    cases = [line for line in lines if not line.startswith("[")]
    parsed = []
    for case in cases:
        if len(case) >= 7 and "타경" in case:
            prefix, suffix = case.split("타경", 1)
            parsed.append((f"{prefix}타경", suffix))
        else:
            parsed.append((None, case))
    if parsed and all(prefix == parsed[0][0] and prefix for prefix, _ in parsed):
        body = [parsed[0][0], *[suffix for _, suffix in parsed]]
    else:
        body = cases
    return "\n".join(body + markers)


def normalize_note_text(text: str) -> str:
    note = text.strip()
    replacements = {
        "공유자우선매수권행사에관한특별매각조건있음": "공유자우선매수권특별매각조건",
        "농지취득자격증명요": "농취증명요",
        "[주]범창종합건설유치권신고,성립여부불명": "유치권신고",
        "지상수목포함매각": "지상수목포함",
    }
    for src, dst in replacements.items():
        note = note.replace(src, dst)
    return note


def normalize_location_text(text: str) -> str:
    location = text.strip()
    replacements = {
        "일반철골구조 판넬지붕 단층 ": "",
        "일반철골구조 샌드위치판넬지붕 2층 ": "",
        "철골조 샌드위치판넬지붕 2층 ": "",
        "철근콘크리트구조 ": "",
        "철근콘크리트조 ": "",
        "경량철골구조 샌드위치 판넬지붕 ": "",
        "슬래브 및 판넬지붕 3층 근린시설 ": "",
        "제조업소 및 사무실 ": "",
        "부속건물 ": "",
        "일반철골구조 ": "",
        "전소유권중갑구 ": "전소유권중갑구",
        "전 소유권 지분 중 ": "전소유권지분중",
        " 지분전부": "지분전부",
        "[전소유권중갑구 ": "[전소유권중갑구",
        ", ": ",",
    }
    for src, dst in replacements.items():
        location = location.replace(src, dst)
    location = location.replace(" [", "[")
    return location


def normalize_entry_for_overlay(entry: dict) -> dict:
    case_raw = entry["case"]
    location = normalize_location_text(entry["location"])
    note = normalize_note_text(entry["note"])

    if "2024타경594" in case_raw:
        location = "\n".join(
            [
                "고령군 성산면 삼대리 252-5 418㎡ 동소 252-2 802㎡ 동소 252-3 460㎡",
                "동소 252-2,252-3 제조업소및사무실347.5㎡ 부속사무실1층92.88㎡ 2층57.6㎡ 제시외창고등295.4㎡",
            ]
        )
    elif "2024타경82" in case_raw:
        location = "\n".join(
            [
                "달성군 현풍읍 원교리 110-1 1068㎡",
                "달성군현풍읍비슬로638 지층노래연습장353.02㎡ 휴게음식점142.02㎡ 1층소매점393.37㎡ 34.76㎡ 일반음식점101.67㎡ 2층사무실,3층게임제공업소각495.04㎡ 제시외 창고등53.3㎡",
            ]
        )
    elif "2024타경34894" in case_raw:
        location = "\n".join(
            [
                "달서구 호산동 110-2 266㎡",
                "달서구 달구벌대로203길 26-8 단층 83.31㎡ 74.01㎡",
            ]
        )
    elif "2024타경30946" in case_raw:
        note = "일괄매각.목록1,2,15지분매각.유치권신고"
    elif "2023타경42515" in case_raw:
        location = "\n".join(
            [
                "서구 중리동 1120-16 385㎡",
                "서구 와룡로66길7-4 1층268.5㎡ 2층268.5㎡ 공장",
                "서구 중리동 1120-18 581㎡[전소유권지분중63/620지분전부]",
            ]
        )
        note = "일괄매각.목록3지분매각"
    elif "2024타경1634" in case_raw and "2024타경39134" in case_raw:
        location = "\n".join(
            [
                "달성군 가창면 삼산리 901 764㎡ 동소 902-1 526㎡ 동소 902-2 314㎡",
                "동소 899 121㎡ 동소 900 66㎡",
            ]
        )
        note = "일괄매각.지상수목포함"
    elif "2024타경32195" in case_raw:
        if "분묘1기소재.지분매각" in note:
            location = "달성군 하빈면 감문리 282-1 936㎡[전소유권중갑구12번 2/3지분전부]"
            note = "분묘1기소재.지분매각"
        else:
            location = "달성군 하빈면 감문리 133-1 926㎡"
            note = "분묘1기소재"
    elif "2024타경36500" in case_raw:
        location = "\n".join(
            [
                "성주군 성주읍 성산리 610-1 1630㎡[165/1795지분]",
                "동소 610 165㎡[165/1795지분]",
            ]
        )
        note = "일괄매각.공유자우선매수권특별매각조건.지분매각"

    return {
        "case": normalize_case_text(case_raw),
        "item": entry["item"],
        "location": location,
        "usage": entry["usage"],
        "price": entry["price"],
        "note": note,
    }


def measure_section(entries: list[dict], cfg: dict, font_size: float) -> tuple[float, list[dict]]:
    font_sizes = [font_size * COLUMN_FONT_FACTORS[idx] for idx in range(6)]
    widths = cfg["w"]
    x_positions = cfg["x"]
    y = cfg["y"]
    leading_factor = cfg.get("leading", 0.96)
    rows: list[dict] = []

    for entry in entries:
        normalized = normalize_entry_for_overlay(entry)
        values = [
            normalized["case"],
            normalized["item"],
            normalized["location"],
            normalized["usage"],
            normalized["price"],
            normalized["note"],
        ]
        wrapped_values = [
            wrap_for_width(value, font_sizes[idx], widths[idx] - 1.5) for idx, value in enumerate(values)
        ]
        row_h = estimate_height(values, font_sizes, widths, font_size, leading_factor)
        rows.append(
            {
                "y": y,
                "height": row_h,
                "values": wrapped_values,
                "font_sizes": font_sizes,
                "x_positions": x_positions,
                "leading_factor": leading_factor,
            }
        )
        y += row_h
    return y, rows


def render_section(page: fitz.Page, entries: list[dict], group: str, fontname: str) -> None:
    cfg = SECTION_LAYOUTS[group]
    capacity = cfg["clear"].y1
    font_size = cfg["size"]
    used = capacity + 1.0
    rows: list[dict] = []
    while font_size >= 4.4:
        used, candidate_rows = measure_section(entries, cfg, font_size)
        rows = candidate_rows
        if used <= capacity:
            break
        font_size -= 0.2

    outer = cfg["clear"]
    page.draw_rect(outer, color=None, fill=(1, 1, 1), overlay=True)
    line_color = (0.72, 0.72, 0.72)
    x_edges = [outer.x0 + 1, *cfg["x"], outer.x1 - 1]
    y_top = cfg["y"] - 3.5
    y_bottom = min(outer.y1 - 1, used)
    for x in x_edges:
        page.draw_line((x, y_top), (x, y_bottom), color=line_color, width=0.45, overlay=True)
    page.draw_line((x_edges[0], y_top), (x_edges[-1], y_top), color=line_color, width=0.45, overlay=True)
    for row in rows:
        bottom = row["y"] + row["height"]
        if bottom < outer.y1 - 0.4:
            page.draw_line((x_edges[0], bottom), (x_edges[-1], bottom), color=line_color, width=0.45, overlay=True)
        for idx, lines in enumerate(row["values"]):
            line_height = line_height_pt(font_size, row.get("leading_factor", cfg.get("leading", 0.96)))
            font_size_col = row["font_sizes"][idx]
            x = row["x_positions"][idx] + 0.8
            y = row["y"] + font_size_col + 0.4
            for line_idx, line in enumerate(lines):
                page.insert_text(
                    (x, y + line_idx * line_height),
                    line,
                    fontname=fontname,
                    fontsize=font_size_col,
                    color=(0, 0, 0),
                    overlay=True,
                )


def overlay(template_pdf: Path, data_json: Path, output_pdf: Path) -> None:
    doc = fitz.open(template_pdf)
    page = doc[0]
    fontname = "NotoSansGothic"
    page.insert_font(fontname=fontname, fontfile=FONT_FILE)
    payload = json.loads(data_json.read_text(encoding="utf-8"))
    formatted = [format_entry(entry) for entry in payload["entries"]]
    grouped = {group: [] for group in GROUP_ORDER}
    for entry in formatted:
        grouped[entry["group"]].append(entry)

    for group in GROUP_ORDER:
        if grouped[group]:
            render_section(page, grouped[group], group, fontname)

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_pdf)
    doc.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="기존 최종본 PDF 디자인 위에 현재 내용을 오버레이합니다.")
    parser.add_argument("template_pdf", type=Path)
    parser.add_argument("data_json", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    overlay(args.template_pdf, args.data_json, args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
