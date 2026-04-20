#!/usr/bin/env python3
"""법원경매공고 자동화 배치 오케스트레이터.

폴더를 받아서 각 사건 폴더마다
  1) CS_*.hwp / .hwpx (또는 경매(N계)*.hwp[x]) 원본 발견
  2) 편집기준에 맞춘 1차 수정본 DOCX (`{stem}-송.docx`) 생성
  3) 최종 표 PDF (`{법원} {계} {MMDD}({N단}).pdf`) 생성
  4) 같은 폴더에 소스 .indd 파일이 있고 `--indesign` 옵션이 주어지면
     InDesign JSX로 최종 PDF 재export
를 수행한다.

사용법::

    python3 app/batch_process.py "청주지방법원 제천지원 경매2계 0209(7단)"
    python3 app/batch_process.py "0320 대구지방법원 서부지원 경매4계-완료" --recursive
    python3 app/batch_process.py . --recursive --indesign --default-columns 5
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from court_auction_editor import build_document  # noqa: E402
from render_final_notice import (  # noqa: E402
    load_entries,
    render_html as render_final_html,
    render_pdf as render_final_pdf,
)
from render_hwp_friendly_rtf import render_docx  # noqa: E402


SOURCE_EXCLUDE_HINTS = ("-송", "수정", "최종", "작업", "편집")


@dataclass
class FolderMeta:
    folder: Path
    date_mmdd: Optional[str] = None
    court: Optional[str] = None
    division: Optional[str] = None
    columns: Optional[int] = None


@dataclass
class CaseResult:
    folder: Path
    status: str
    source: Optional[Path] = None
    docx: Optional[Path] = None
    pdf: Optional[Path] = None
    indesign_pdf: Optional[Path] = None
    error: str = ""
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 폴더/파일명 메타데이터 파싱
# ---------------------------------------------------------------------------


def parse_folder_meta(folder: Path) -> FolderMeta:
    """폴더 이름에서 {MMDD} {법원} {계} (N단) 추출."""

    meta = FolderMeta(folder=folder)
    name = folder.name

    date_match = re.search(r"(?<!\d)(\d{4})(?!\d)", name)
    if date_match:
        meta.date_mmdd = date_match.group(1)

    col_match = re.search(r"\((\d+)\s*단\)", name)
    if col_match:
        meta.columns = int(col_match.group(1))

    cleaned = re.sub(r"\(\d+\s*단\)", "", name)
    cleaned = re.sub(r"-?완료.*$", "", cleaned)
    cleaned = re.sub(r"(?<!\d)\d{4}(?!\d)", "", cleaned, count=1)
    cleaned = cleaned.strip()

    div_match = re.search(r"경매\s*\d+\s*계", cleaned)
    if div_match:
        meta.division = re.sub(r"\s+", "", div_match.group(0))
        meta.court = cleaned[: div_match.start()].strip()
    else:
        meta.court = cleaned or None

    if meta.court:
        meta.court = re.sub(r"\s+", " ", meta.court).strip()

    # 완료 폴더 안에 이미 있는 최종본 파일명에서 단수를 읽을 수도 있음.
    if meta.columns is None:
        for existing in folder.glob("*.pdf"):
            hint = re.search(r"\((\d+)단\)", existing.name)
            if hint:
                meta.columns = int(hint.group(1))
                break

    return meta


SOURCE_SUFFIXES = (".hwp", ".hwpx")


def find_source_file(folder: Path) -> Optional[Path]:
    """사건 폴더에서 원본 .hwp/.hwpx를 찾는다.

    우선순위:
      1) CS_YYYYMMDD_*.hwpx
      2) CS_YYYYMMDD_*.hwp
      3) '경매(N계)...' 패턴의 hwpx/hwp
      4) 그 외 .hwp / .hwpx (제외 키워드 포함 시 skip)
    """

    candidates: list[Path] = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        name = path.name
        if any(hint in name for hint in SOURCE_EXCLUDE_HINTS):
            continue
        candidates.append(path)

    if not candidates:
        return None

    def score(path: Path) -> tuple[int, int]:
        name = path.name.lower()
        suffix_bonus = 0 if path.suffix.lower() == ".hwpx" else 1
        if name.startswith("cs_"):
            return (0, suffix_bonus)
        if "경매" in path.name and "계" in path.name:
            return (1, suffix_bonus)
        return (2, suffix_bonus)

    candidates.sort(key=score)
    return candidates[0]


def final_pdf_name(meta: FolderMeta, default_columns: int) -> str:
    court = meta.court or "법원경매공고"
    division = meta.division or ""
    date_mmdd = meta.date_mmdd or ""
    columns = meta.columns or default_columns
    base = " ".join(part for part in (court, division, date_mmdd) if part).strip()
    return f"{base}({columns}단).pdf"


# ---------------------------------------------------------------------------
# InDesign JSX 호출
# ---------------------------------------------------------------------------


INDESIGN_APP = "Adobe InDesign 2026"


def find_source_indd(folder: Path, canonical_base: Optional[str] = None) -> Optional[Path]:
    """사건 폴더에서 최종본 InDesign 템플릿으로 쓸 파일을 고른다.

    우선순위:
      1) 최종 PDF와 정확히 같은 stem(`{법원} {계} {MMDD}({N단})`)을 가진 .indd
      2) 이름에 '무제' 같은 보조어가 없고, 수정/편집 힌트가 없는 .indd
      3) 나머지
    """

    indd_files = [p for p in folder.glob("*.indd") if not p.name.startswith(".")]
    if not indd_files:
        return None

    def score(path: Path) -> tuple[int, int, str]:
        stem = path.stem
        name = path.name
        hint_penalty = 0
        if any(hint in name for hint in SOURCE_EXCLUDE_HINTS):
            hint_penalty += 10
        if "무제" in name or re.search(r"^[0-9a-f\-]{20,}$", stem):
            hint_penalty += 5
        canonical_bonus = 0 if canonical_base and stem == canonical_base else 1
        return (canonical_bonus, hint_penalty, name)

    indd_files.sort(key=score)
    return indd_files[0]


def run_indesign_export(source_indd: Path, out_pdf: Path) -> None:
    """지정된 .indd 를 InDesign으로 열어 PDF로 export."""

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    jsx_body = (
        "var args = JSON.parse(readArgs());\n"
        "function readArgs() {\n"
        "  var f = File(arguments.callee.argsPath);\n"
        "  f.encoding = 'UTF-8';\n"
        "  f.open('r');\n"
        "  var raw = f.read();\n"
        "  f.close();\n"
        "  return raw;\n"
        "}\n"
    )
    # ExtendScript does not expose arguments.callee.argsPath easily, so we
    # generate a standalone script that inlines the needed paths directly.
    script_text = (
        "(function () {\n"
        f"    var srcFile = File({_jsx_string(str(source_indd.resolve()))});\n"
        f"    var outFile = File({_jsx_string(str(out_pdf.resolve()))});\n"
        "    if (!srcFile.exists) { throw new Error('missing indd: ' + srcFile.fsName); }\n"
        "    var doc = app.open(srcFile, false);\n"
        "    var preset;\n"
        "    try {\n"
        "        preset = app.pdfExportPresets.itemByName('[High Quality Print]');\n"
        "        preset.name;\n"
        "    } catch (e) {\n"
        "        preset = app.pdfExportPresets.firstItem();\n"
        "    }\n"
        "    doc.exportFile(ExportFormat.PDF_TYPE, outFile, false, preset);\n"
        "    doc.close(SaveOptions.NO);\n"
        "})();\n"
    )
    del jsx_body  # placeholder above is illustrative; not used.

    with tempfile.NamedTemporaryFile(
        "w", suffix=".jsx", prefix="indd-export-", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(script_text)
        jsx_path = Path(tmp.name)

    try:
        applescript = (
            f'tell application "{INDESIGN_APP}"\n'
            f'    do script (POSIX file "{jsx_path}") language javascript\n'
            f"end tell\n"
        )
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"InDesign export 실패: {result.stderr.strip() or result.stdout.strip()}"
            )
    finally:
        try:
            jsx_path.unlink()
        except OSError:
            pass


def _jsx_string(value: str) -> str:
    """ExtendScript에서 안전하게 쓸 수 있는 문자열 리터럴로 변환."""

    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f"'{escaped}'"


# ---------------------------------------------------------------------------
# 단일 폴더 처리
# ---------------------------------------------------------------------------


def process_case(
    folder: Path,
    *,
    default_columns: int,
    use_indesign: bool,
    overwrite: bool,
) -> CaseResult:
    result = CaseResult(folder=folder, status="pending")
    source = find_source_file(folder)
    if not source:
        result.status = "skipped"
        result.error = "원본 .hwp/.hwpx 파일을 찾지 못함"
        return result
    result.source = source

    meta = parse_folder_meta(folder)
    final_name = final_pdf_name(meta, default_columns)
    final_pdf_path = folder / final_name
    docx_path = folder / f"{source.stem}-송.docx"

    # 기존 파일(디자이너가 수작업으로 만든 레퍼런스 등)은 기본적으로 보호.
    # --overwrite 가 명시된 경우에만 덮어쓰고, 그 외에는 .auto 접미사를 붙인 경로에 쓴다.
    def resolved_output(primary: Path) -> tuple[Path, bool]:
        """(실제 쓰일 경로, 레퍼런스 충돌 여부) 를 돌려준다."""
        if overwrite or not primary.exists():
            return primary, False
        alt = primary.with_name(f"{primary.stem}.auto{primary.suffix}")
        return alt, True

    docx_target, docx_collided = resolved_output(docx_path)
    pdf_target, pdf_collided = resolved_output(final_pdf_path)

    if not overwrite and docx_target.exists() and pdf_target.exists():
        result.status = "cached"
        result.docx = docx_target
        result.pdf = pdf_target
        result.details.append("기존 자동생성본 재사용 (--overwrite 없음)")
        return result

    try:
        with tempfile.TemporaryDirectory(prefix="case-") as tmp_name:
            tmp_dir = Path(tmp_name)
            _, json_path = build_document(source, tmp_dir)
            doc = load_entries(json_path)

            tmp_docx = tmp_dir / f"{source.stem}-송.docx"
            render_docx(doc, tmp_docx)
            shutil.copy2(tmp_docx, docx_target)
            result.docx = docx_target
            if docx_collided:
                result.details.append(
                    f"기존 {docx_path.name} 보존 → {docx_target.name} 로 저장"
                )

            tmp_html = tmp_dir / f"{source.stem}.final.html"
            tmp_html.write_text(render_final_html(doc), encoding="utf-8")
            tmp_pdf = tmp_dir / f"{source.stem}.final.pdf"
            render_final_pdf(tmp_html, tmp_pdf)
            shutil.copy2(tmp_pdf, pdf_target)
            result.pdf = pdf_target
            if pdf_collided:
                result.details.append(
                    f"기존 {final_pdf_path.name} 보존 → {pdf_target.name} 로 저장"
                )

        if use_indesign:
            source_indd = find_source_indd(folder, canonical_base=final_pdf_path.stem)
            if source_indd:
                # InDesign export는 항상 별도 경로(`.indesign.pdf`)에 저장해서
                # Chrome 기반 A4 출력과 공존한다. 같은 이름의 레퍼런스가 있으면
                # .auto 접미사로 한 번 더 보호한다.
                indesign_base = final_pdf_path.with_name(
                    f"{final_pdf_path.stem}.indesign{final_pdf_path.suffix}"
                )
                indesign_target, indesign_collided = resolved_output(indesign_base)
                try:
                    run_indesign_export(source_indd, indesign_target)
                    result.indesign_pdf = indesign_target
                    note = f"InDesign export: {source_indd.name}"
                    if indesign_collided:
                        note += f" ({indesign_base.name} 보존 → {indesign_target.name})"
                    result.details.append(note)
                except Exception as indesign_err:  # noqa: BLE001
                    result.details.append(f"InDesign export 실패: {indesign_err}")
            else:
                result.details.append("소스 .indd 없음 → InDesign 단계 skip")

        result.status = "ok"
        return result
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        result.details.append(traceback.format_exc(limit=3))
        return result


# ---------------------------------------------------------------------------
# 폴더 탐색 + 배치 구동
# ---------------------------------------------------------------------------


def looks_like_case_folder(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    return find_source_file(folder) is not None


def iter_case_folders(root: Path, *, recursive: bool) -> list[Path]:
    if looks_like_case_folder(root):
        return [root]
    if not recursive:
        found = [p for p in sorted(root.iterdir()) if looks_like_case_folder(p)]
        return found
    collected: list[Path] = []
    for p in sorted(root.rglob("*")):
        if looks_like_case_folder(p):
            collected.append(p)
    return collected


def format_report(results: list[CaseResult]) -> str:
    lines = ["# 배치 처리 결과", ""]
    counters = {"ok": 0, "cached": 0, "skipped": 0, "error": 0}
    for r in results:
        counters[r.status] = counters.get(r.status, 0) + 1
        status_emoji = {
            "ok": "OK",
            "cached": "CACHE",
            "skipped": "SKIP",
            "error": "ERR",
        }.get(r.status, r.status)
        lines.append(f"- [{status_emoji}] {r.folder}")
        if r.source:
            lines.append(f"    원본: {r.source.name}")
        if r.docx:
            lines.append(f"    1차수정본: {r.docx.name}")
        if r.pdf:
            lines.append(f"    최종PDF: {r.pdf.name}")
        if r.indesign_pdf:
            lines.append(f"    InDesignPDF: {r.indesign_pdf.name}")
        if r.error:
            lines.append(f"    오류: {r.error}")
        for detail in r.details:
            if detail.strip():
                short = detail.strip().splitlines()[0]
                lines.append(f"    · {short}")
    lines.extend(
        [
            "",
            f"총 {len(results)}건 — OK {counters.get('ok', 0)}, "
            f"CACHE {counters.get('cached', 0)}, "
            f"SKIP {counters.get('skipped', 0)}, "
            f"ERR {counters.get('error', 0)}",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="법원경매공고 배치 오케스트레이터")
    parser.add_argument("root", type=Path, help="단일 사건 폴더 또는 부모 폴더")
    parser.add_argument(
        "--recursive", action="store_true", help="하위 폴더를 재귀적으로 탐색"
    )
    parser.add_argument(
        "--default-columns",
        type=int,
        default=5,
        help="폴더/파일명에서 단수를 찾지 못했을 때 기본값 (기본 5단)",
    )
    parser.add_argument(
        "--indesign",
        action="store_true",
        help="같은 폴더에 소스 .indd가 있으면 InDesign으로도 PDF를 export",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="이미 생성된 DOCX/PDF가 있어도 덮어쓰기",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="결과 요약 MD 저장 경로 (미지정 시 stdout으로만 출력)",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        default=None,
        help="결과 요약을 JSON으로도 저장",
    )
    args = parser.parse_args()

    if not args.root.exists():
        print(f"경로가 존재하지 않습니다: {args.root}", file=sys.stderr)
        return 1

    folders = iter_case_folders(args.root, recursive=args.recursive)
    if not folders:
        print("처리할 사건 폴더를 찾지 못했습니다.", file=sys.stderr)
        return 1

    results: list[CaseResult] = []
    for idx, folder in enumerate(folders, 1):
        print(f"[{idx}/{len(folders)}] {folder}")
        result = process_case(
            folder,
            default_columns=args.default_columns,
            use_indesign=args.indesign,
            overwrite=args.overwrite,
        )
        results.append(result)
        suffix = f" — {result.error}" if result.error else ""
        print(f"    {result.status}{suffix}")

    report = format_report(results)
    print()
    print(report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report + "\n", encoding="utf-8")
    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "folder": str(r.folder),
                "status": r.status,
                "source": str(r.source) if r.source else None,
                "docx": str(r.docx) if r.docx else None,
                "pdf": str(r.pdf) if r.pdf else None,
                "indesign_pdf": str(r.indesign_pdf) if r.indesign_pdf else None,
                "error": r.error,
                "details": r.details,
            }
            for r in results
        ]
        args.json_report.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return 0 if all(r.status in {"ok", "cached"} for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
