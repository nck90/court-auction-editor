#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from overlay_template_pdf import normalize_entry_for_overlay
from render_final_notice import GROUP_ORDER, format_entry, parse_case_sort_key


FONT_FILE = "/System/Library/Fonts/Supplemental/AppleGothic.ttf"
SCALE = 4

SECTION_LAYOUTS = {
    "기타": {"body": [76, 8, 140, 244], "widths": [30, 10, 120, 20, 34, 22], "font": 5.2},
    "아파트": {"body": [150, 8, 167, 244], "widths": [30, 10, 120, 20, 34, 22], "font": 5.5},
    "연립주택/다세대/빌라": {"body": [176, 8, 188, 244], "widths": [30, 10, 120, 20, 34, 22], "font": 5.4},
    "대지/임야/전답": {"body": [50, 250, 122, 520], "widths": [30, 10, 145, 22, 36, 27], "font": 5.0},
    "상가/오피스텔,근린시설": {"body": [132, 250, 156, 520], "widths": [30, 10, 145, 22, 36, 27], "font": 5.1},
    "단독주택,다가구주택": {"body": [166, 250, 182, 520], "widths": [30, 10, 145, 22, 36, 27], "font": 5.0},
}


def wrap_for_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width_px: int) -> list[str]:
    lines: list[str] = []
    for raw in text.split("\n"):
        text_part = raw.strip()
        if not text_part:
            lines.append("")
            continue
        current = ""
        for ch in text_part:
            candidate = current + ch
            box = draw.textbbox((0, 0), candidate, font=font)
            if current and (box[2] - box[0]) > width_px:
                lines.append(current)
                current = ch
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


def grouped_entries(data_json: Path) -> dict[str, list[dict]]:
    payload = json.loads(data_json.read_text(encoding="utf-8"))
    groups = {group: [] for group in GROUP_ORDER}
    for entry in payload["entries"]:
        base = format_entry(entry)
        groups[base["group"]].append(normalize_entry_for_overlay(base))
    for group in GROUP_ORDER:
        groups[group] = sorted(groups[group], key=lambda row: parse_case_sort_key(row["case"], row["item"]))
    return groups


def measure_group(entries: list[dict], cfg: dict, font_size: float) -> tuple[int, Image.Image]:
    body = cfg["body"]
    width_pt = body[3] - body[1]
    height_pt = body[2] - body[0]
    width_px = int(width_pt * SCALE)
    height_px = int(height_pt * SCALE)
    image = Image.new("RGBA", (width_px, height_px), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    widths = [w * SCALE for w in cfg["widths"]]
    font_sizes = [
        max(10, int(font_size * SCALE * factor))
        for factor in [0.92, 0.9, 1.0, 0.92, 0.96, 0.9]
    ]
    fonts = [ImageFont.truetype(FONT_FILE, size=s) for s in font_sizes]
    x_positions = []
    x = 0
    for width in widths:
        x_positions.append(int(x))
        x += width
    y = 0
    line_height = max(12, int(font_size * SCALE * 1.02))
    for entry in entries:
        values = [entry["case"], entry["item"], entry["location"], entry["usage"], entry["price"], entry["note"]]
        wrapped_cols = []
        max_lines = 1
        for idx, value in enumerate(values):
            wrapped = wrap_for_width(draw, value, fonts[idx], int(widths[idx]) - 4)
            wrapped_cols.append(wrapped)
            max_lines = max(max_lines, len(wrapped))
        row_h = max_lines * line_height + 6
        for idx, wrapped in enumerate(wrapped_cols):
            cell_box = [x_positions[idx], y, x_positions[idx] + int(widths[idx]), y + row_h]
            draw.rectangle(cell_box, outline=(0, 0, 0, 120), width=1)
            draw.multiline_text(
                (cell_box[0] + 2, cell_box[1] + 1),
                "\n".join(wrapped),
                font=fonts[idx],
                fill=(0, 0, 0, 255),
                spacing=1,
            )
        y += row_h
    return y, image


def render_group(entries: list[dict], cfg: dict, out_path: Path) -> None:
    height_pt = cfg["body"][2] - cfg["body"][0]
    capacity = int(height_pt * SCALE)
    font_size = cfg["font"]
    image = None
    used = capacity + 1
    while font_size >= 3.6:
        used, candidate = measure_group(entries, cfg, font_size)
        image = candidate
        if used <= capacity:
            break
        font_size -= 0.2
    assert image is not None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="INDD 배치용 섹션 이미지를 생성합니다.")
    parser.add_argument("data_json", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, required=True)
    args = parser.parse_args()
    groups = grouped_entries(args.data_json)
    payload = {"sections": []}
    for group in GROUP_ORDER:
        cfg = SECTION_LAYOUTS[group]
        safe_group = group.replace("/", "_").replace(",", "_")
        img_path = args.output_dir / f"section_{safe_group}.png"
        render_group(groups[group], cfg, img_path)
        payload["sections"].append({"group": group, "body": cfg["body"], "image": str(img_path)})
    payload_path = args.output_dir / "indd_sections.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(payload_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
