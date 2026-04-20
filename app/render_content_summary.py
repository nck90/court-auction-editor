#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
from pathlib import Path

from render_final_notice import GROUP_ORDER, format_entry, load_entries, parse_case_sort_key, render_pdf


def render_html(doc: dict) -> str:
    entries = [format_entry(entry) for entry in doc["entries"]]
    grouped = {group: [] for group in GROUP_ORDER}
    for entry in entries:
        grouped[entry["group"]].append(entry)

    cards: list[str] = []
    total_count = 0
    for group in GROUP_ORDER:
        rows = sorted(grouped[group], key=lambda row: parse_case_sort_key(row["case"], row["item"]))
        if not rows:
            continue
        total_count += len(rows)
        item_html: list[str] = []
        for row in rows:
            item_html.append(
                "<article class='item'>"
                f"<div class='item-head'><span class='case'>{html.escape(row['case']).replace(chr(10), '<br>')}</span>"
                f"<span class='item-no'>물건 {html.escape(row['item'])}</span></div>"
                f"<div class='field'><strong>소재지 및 면적</strong><p>{html.escape(row['location']).replace(chr(10), '<br>')}</p></div>"
                f"<div class='split'>"
                f"<div class='field'><strong>용도</strong><p>{html.escape(row['usage']).replace(chr(10), '<br>')}</p></div>"
                f"<div class='field'><strong>감정가 / 최저가</strong><p>{html.escape(row['price']).replace(chr(10), '<br>')}</p></div>"
                "</div>"
                f"<div class='field'><strong>비고</strong><p>{html.escape(row['note']) if row['note'] else '-'}</p></div>"
                "</article>"
            )
        cards.append(
            "<section class='group'>"
            f"<h2>{html.escape(group)} <span>{len(rows)}건</span></h2>"
            f"{''.join(item_html)}"
            "</section>"
        )

    court_line = html.escape(doc.get("court_line") or "법원 경매부동산의 매각 공고")
    auction_datetime = html.escape(doc.get("auction_datetime") or "-")
    decision_datetime = html.escape(doc.get("decision_datetime") or "-")
    officer_line = html.escape(doc.get("officer_line") or "-")

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>법원경매공고 내용 정리본</title>
<style>
@page {{ size: A4; margin: 14mm; }}
body {{
  font-family: "Apple SD Gothic Neo","Malgun Gothic",sans-serif;
  color: #111;
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
}}
h1, h2, p {{ margin: 0; }}
.page {{
  padding: 4mm 1mm 0;
}}
.header {{
  border-bottom: 2px solid #222;
  padding-bottom: 10px;
  margin-bottom: 14px;
}}
.header h1 {{
  font-size: 24px;
  margin-bottom: 4px;
}}
.subtitle {{
  color: #555;
  font-size: 13px;
}}
.meta {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 18px;
  margin-top: 10px;
  font-size: 12px;
}}
.summary {{
  margin: 10px 0 14px;
  padding: 8px 10px;
  background: #f3f4f6;
  border: 1px solid #d5d9df;
  border-radius: 8px;
}}
.group {{
  margin-bottom: 18px;
  break-inside: avoid;
}}
.group h2 {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 16px;
  padding-bottom: 4px;
  margin-bottom: 8px;
  border-bottom: 1px solid #bbb;
}}
.group h2 span {{
  font-size: 11px;
  color: #666;
}}
.item {{
  border: 1px solid #cfd4da;
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 8px;
  break-inside: avoid;
}}
.item-head {{
  display: flex;
  justify-content: space-between;
  gap: 16px;
  font-weight: 700;
  margin-bottom: 8px;
}}
.case {{
  font-size: 13px;
}}
.item-no {{
  white-space: nowrap;
}}
.field {{
  margin-top: 6px;
}}
.field strong {{
  display: block;
  margin-bottom: 2px;
  font-size: 11px;
  color: #444;
}}
.field p {{
  word-break: break-word;
}}
.split {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}}
</style>
</head>
<body>
  <main class="page">
    <header class="header">
      <h1>법원경매공고 내용 정리본</h1>
      <p class="subtitle">{court_line}</p>
      <div class="meta">
        <div><strong>매각일시</strong> {auction_datetime}</div>
        <div><strong>매각결정기일</strong> {decision_datetime}</div>
        <div><strong>담당계</strong> 경매 4계</div>
        <div><strong>집행관</strong> {officer_line}</div>
      </div>
    </header>
    <section class="summary">
      전체 {total_count}건을 용도 그룹별로 재정리한 내용 확인용 문서입니다.
    </section>
    {''.join(cards)}
  </main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="정규화 JSON을 내용 정리용 HTML/PDF로 렌더링합니다.")
    parser.add_argument("json_path", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--pdf", action="store_true")
    args = parser.parse_args()

    doc = load_entries(args.json_path)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    html_path = args.output_dir / f"{args.json_path.stem}.content-summary.html"
    html_path.write_text(render_html(doc), encoding="utf-8")
    print(f"HTML: {html_path}")
    if args.pdf:
        pdf_path = args.output_dir / f"{args.json_path.stem}.content-summary.pdf"
        render_pdf(html_path, pdf_path)
        print(f"PDF: {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
