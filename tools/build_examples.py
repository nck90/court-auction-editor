#!/usr/bin/env python3
"""Build knowledge/examples/<case_id>.md for completed cases.

Pairs normalized JSON entries with the human PDF text (pdftotext -layout), so
the LLM can retrieve them at runtime.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/bagjun-won/t")
sys.path.insert(0, str(ROOT / "app"))

from court_auction_editor import build_document  # noqa: E402
from render_final_notice import format_entry, load_entries  # noqa: E402

BASE = ROOT / "0320 대구지방법원 서부지원 경매4계-완료"
EX_DIR = ROOT / "knowledge" / "examples"
EX_DIR.mkdir(parents=True, exist_ok=True)

CASE_NUM_RE = re.compile(r"(20\d{2}타경\d+)")


def slug(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_]+", "_", name.strip())


def find_case(case_dir: Path):
    hwps = sorted(case_dir.glob("CS_*.hwp"))
    if not hwps:
        # fallback: any hwp starting with Korean court name digit prefix
        hwps = [p for p in case_dir.glob("*.hwp") if not p.name.startswith("~")]
    pdfs_mod = [p for p in case_dir.glob("*.pdf") if "수정" in p.name]
    pdfs = pdfs_mod or [p for p in case_dir.glob("*.pdf") if "지방법원" in p.name or "법원" in p.name]
    return (hwps[0] if hwps else None), (pdfs[0] if pdfs else None)


def pdf_text(pdf_path: Path) -> str:
    try:
        res = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return res.stdout
    except Exception:
        return ""


def find_case_chunk(pdf_content: str, case_num: str) -> str:
    # Get ~500 chars around each occurrence of the case number
    idx = pdf_content.find(case_num)
    if idx < 0:
        return ""
    # Normalize case_num may appear as '2024타경' on one line and number on another
    return pdf_content[max(0, idx - 50) : idx + 1000]


def build_case(case_dir_name: str) -> bool:
    case_dir = BASE / case_dir_name
    if not case_dir.is_dir():
        return False
    hwp, pdf = find_case(case_dir)
    if not hwp or not pdf:
        return False

    out_dir = ROOT / "output" / "batch_test" / slug(case_dir_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        _, json_path = build_document(hwp, out_dir)
    except Exception as e:
        print(f"  skip {case_dir_name}: {e}")
        return False
    doc = load_entries(json_path)
    pdf_txt = pdf_text(pdf)
    file_stem = slug(case_dir_name)
    md_path = EX_DIR / f"{file_stem}.md"
    lines = [f"# Example: {case_dir_name}", ""]
    for entry in doc.get("entries", []):
        if (entry.get("usage") or "") in {"자동차", "선박", "건설기계", "항공기"}:
            continue
        case_nums = entry.get("case_numbers") or []
        item = entry.get("item_number") or ""
        formatted = format_entry(entry)
        lines.append(f"## {case_nums[0] if case_nums else ''} 물건{item}")
        lines.append("")
        lines.append("### 입력 (raw)")
        lines.append("```json")
        lines.append(
            json.dumps(
                {
                    "case_numbers": case_nums,
                    "usage": entry.get("usage"),
                    "note_lines": entry.get("note_lines") or [],
                    "properties": entry.get("properties") or [],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        lines.append("```")
        lines.append("")
        lines.append("### 자동 파이프라인 출력")
        lines.append(f"- locations: `{formatted.get('locations')}`")
        lines.append(f"- usages: `{formatted.get('usages')}`")
        lines.append(f"- note: `{formatted.get('note')}`")
        lines.append("")
        if case_nums:
            chunk = find_case_chunk(pdf_txt, case_nums[0])
            if chunk:
                lines.append("### 사람 최종본 (PDF 추출)")
                lines.append("```")
                lines.append(chunk.strip())
                lines.append("```")
                lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {md_path.relative_to(ROOT)}")
    return True


CASES = [
    "0320 청주지방법원 경매7계-완료",
    "0320 의정부지방법원 경매3계-완료",
    "0321 인천지방법원 경매1계-완료",
    "0321 춘천지방법원 원주지원 경매3계-완료",
    "0321 서울동부지방법원 경매1계-완료",
    "0321 서울동부지방법원 경매6계-완료",
    "0321 서울북부지방법원 경매8계-완료",
    "0321 서울북부지방법원 경매9계-완료",
    "0324 대구지방법원 안동지원 경매2계-완료",
    "0324 수원지방법원 성남지원 경매5계-완료 64142 확인",
    "0325 인천지방법원 경매23계-완료",
    "0325 대구지방법원 상주지원 경매1계-완료",
    "0325 대전지방법원 천안지원 경매1계-완료",
    "0325 인천지방법원 부천지원 경매3계-완료",
    "0325 대구지방법원 서부지원 경매5계-완료",
    "0325 의정부지방법원 고양지원 경매2계-완료",
    "0326 인천지방법원 경매3계-완료",
    "0326 의정부지방법원 고양지원 경매14계-완료",
    "0327 의정부지방법원 남양주지원 경매4계-완료",
    "0328 춘천지방법원 원주지원 경매4계-완료",
    "0331 대전지방법원 경매1계-완료",
    "0331 청주지방법원 충주지원 경매2계-완료",
    "0331 춘천지방법원 강릉지원 경매2계-완료",
    "0331 수원지방법원 성남지원 경매8계-완료",
    "0331 서울중앙지방법원 경매21계-완료",
    "0331 서울서부지방법원 경매5계-완료",
    "0331 서울서부지방법원 경매7계",
]


def main() -> int:
    ok = 0
    for c in CASES:
        print(f"== {c} ==")
        if build_case(c):
            ok += 1
    print(f"\nBuilt {ok} / {len(CASES)} example files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
