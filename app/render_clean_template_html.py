#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.server
import html
import json
import os
import socket
import shutil
import subprocess
import threading
from pathlib import Path

from overlay_template_pdf import normalize_entry_for_overlay
from render_final_notice import GROUP_ORDER, format_entry, parse_case_sort_key


PAGE_WIDTH_PT = 1048.82
PAGE_HEIGHT_PT = 581.10
COORD_SCALE = 0.5


SECTION_BOXES = {
    "기타": {"left": 22, "top": 284, "width": 650, "height": 618, "font": 12.2},
    "아파트": {"left": 22, "top": 953, "width": 650, "height": 124, "font": 14.0},
    "연립주택/다세대/빌라": {"left": 22, "top": 1101, "width": 650, "height": 58, "font": 12.6},
    "대지/임야/전답": {"left": 712, "top": 116, "width": 648, "height": 548, "font": 11.8},
    "상가/오피스텔,근린시설": {"left": 712, "top": 714, "width": 648, "height": 122, "font": 11.8},
    "단독주택,다가구주택": {"left": 712, "top": 870, "width": 648, "height": 180, "font": 11.8},
}


def load_grouped_entries(data_json: Path) -> dict[str, list[dict]]:
    payload = json.loads(data_json.read_text(encoding="utf-8"))
    grouped = {group: [] for group in GROUP_ORDER}
    for raw in payload["entries"]:
        base = format_entry(raw)
        normalized = normalize_entry_for_overlay(base)
        normalized["group"] = base["group"]
        grouped[base["group"]].append(normalized)
    for group in GROUP_ORDER:
        grouped[group] = sorted(grouped[group], key=lambda row: parse_case_sort_key(row["case"], row["item"]))
    return grouped


def section_table(entries: list[dict], font_size: float) -> str:
    rows = []
    for entry in entries:
        cells = [
            entry["case"],
            entry["item"],
            entry["location"],
            entry["usage"],
            entry["price"],
            entry["note"],
        ]
        tds = []
        for idx, cell in enumerate(cells):
            cls = ["case", "item", "location", "usage", "price", "note"][idx]
            tds.append(f'<td class="{cls}">{html.escape(cell).replace(chr(10), "<br>")}</td>')
        rows.append(f"<tr>{''.join(tds)}</tr>")
    return f"""
    <table style="font-size:{font_size}px">
      <colgroup>
        <col style="width:12%">
        <col style="width:4.5%">
        <col style="width:45%">
        <col style="width:11%">
        <col style="width:13%">
        <col style="width:14.5%">
      </colgroup>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """


def render_html(data_json: Path, background_image: Path) -> str:
    grouped = load_grouped_entries(data_json)
    sections = []
    for group in GROUP_ORDER:
        entries = grouped[group]
        if not entries:
            continue
        box = SECTION_BOXES[group]
        sections.append(
            f"""
            <section class="section" style="left:{box['left'] * COORD_SCALE}pt;top:{box['top'] * COORD_SCALE}pt;width:{box['width'] * COORD_SCALE}pt;height:{box['height'] * COORD_SCALE}pt;">
              {section_table(entries, box['font'] * COORD_SCALE)}
            </section>
            """
        )

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>법원 경매부동산의 매각 공고</title>
  <style>
    @page {{
      size: {PAGE_WIDTH_PT}pt {PAGE_HEIGHT_PT}pt;
      margin: 0;
    }}
    html, body {{
      margin: 0;
      padding: 0;
      width: {PAGE_WIDTH_PT}pt;
      height: {PAGE_HEIGHT_PT}pt;
      overflow: hidden;
      font-family: "Apple SD Gothic Neo", "Noto Sans Gothic", "Malgun Gothic", sans-serif;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
      background: #fff;
    }}
    .page {{
      position: relative;
      width: {PAGE_WIDTH_PT}pt;
      height: {PAGE_HEIGHT_PT}pt;
      overflow: hidden;
    }}
    .bg {{
      position: absolute;
      inset: 0;
      width: {PAGE_WIDTH_PT}pt;
      height: {PAGE_HEIGHT_PT}pt;
      display: block;
    }}
    .section {{
      position: absolute;
      background: #fff;
      overflow: hidden;
      z-index: 1;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      line-height: 1.02;
      color: #111;
    }}
    td {{
      border: 1px solid #c8c8c8;
      padding: 2px 3px 1px 3px;
      vertical-align: top;
      word-break: keep-all;
      overflow-wrap: anywhere;
      letter-spacing: -0.02em;
    }}
    td.item {{
      text-align: center;
    }}
    td.price, td.case, td.usage, td.note {{
      letter-spacing: -0.03em;
    }}
    td.note {{
      font-size: 0.92em;
    }}
    td.case {{
      font-size: 0.94em;
    }}
    td.price {{
      font-size: 0.95em;
    }}
  </style>
</head>
<body>
  <div class="page">
    <img class="bg" src="{html.escape(background_image.name)}">
    {''.join(sections)}
  </div>
</body>
</html>
"""


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def render_pdf(html_path: Path, pdf_path: Path) -> None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Arc.app/Contents/MacOS/Arc",
    ]
    chrome = next((c for c in candidates if Path(c).exists()), None) or shutil.which("google-chrome")
    if not chrome:
        raise RuntimeError("Chrome 계열 브라우저를 찾지 못했습니다.")
    port = find_free_port()
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args, directory=str(html_path.parent), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--print-to-pdf={pdf_path.resolve()}",
                f"http://127.0.0.1:{port}/{html_path.name}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="템플릿 배경 위에 HTML/CSS로 표를 재배치합니다.")
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--background", type=Path, required=True)
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--pdf", action="store_true")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    html_path = args.output_dir / f"{args.json_path.stem}.clean-template.html"
    html_path.write_text(render_html(args.json_path, args.background), encoding="utf-8")
    print(f"HTML: {html_path}")
    if args.pdf:
        pdf_path = args.output_dir / "대구지방법원 서부지원 경매4계 완성본.pdf"
        render_pdf(html_path, pdf_path)
        print(f"PDF: {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
