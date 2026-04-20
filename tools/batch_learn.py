#!/usr/bin/env python3
"""배치 학습 루프 실행기.

대상 폴더: /Users/bagjun-won/t/0320 대구지방법원 서부지원 경매4계-완료/
각 사건 폴더에서 CS_*.hwp 와 수정*.pdf (또는 수정N*.pdf 최신) 페어를 골라
1) build_document → normalized.json
2) render_final_notice.render_html (LLM refiner on) → HTML + format_entry별 rendered list
3) (옵션) PDF 렌더
4) scorer.score_case (reference = 가장 최신 수정본 PDF)
5) memory.log_run (scorer 내부에서 자동) + promote_to_example + 저득점 mismatch → record_correction
6) 리포트 생성 + distill_lessons
7) 2차 패스 재실행 + 비교 리포트

에러는 try/except 로 각 케이스 격리. 실시간 print 로 진행상황 보고.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import statistics
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
KB_DIR = ROOT / "knowledge"
sys.path.insert(0, str(APP_DIR))

# Ensure LLM refiner is on.
os.environ.setdefault("USE_LLM_REFINER", "1")

# Ollama env defaults.
os.environ.setdefault("OLLAMA_URL", "https://ollama.hyphen.it.com/api/generate")
os.environ.setdefault("OLLAMA_MODEL", "gemma3:4b")
os.environ.setdefault("OLLAMA_UA", "curl/8.1.2 (court-auction-learner)")
os.environ.setdefault("OLLAMA_TIMEOUT", "60")

from court_auction_editor import build_document  # noqa: E402
from render_final_notice import (  # noqa: E402
    format_entry,
    render_html,
    render_pdf,
    compact_common,
)
import memory  # noqa: E402
import scorer  # noqa: E402

SOURCE_ROOT = Path(
    "/Users/bagjun-won/t/0320 대구지방법원 서부지원 경매4계-완료"
)
OUTPUT_ROOT = Path("/Users/bagjun-won/t/output/learning_batch")
REPORT_PATH = KB_DIR / "batch_report.md"

PER_CASE_TIMEOUT_SEC = int(os.environ.get("BATCH_CASE_TIMEOUT", "300"))

SUSPICIOUS_USAGE = {"자동차", "선박", "건설기계", "항공기"}


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _slug(folder_name: str) -> str:
    s = folder_name.replace(" ", "_").replace("/", "_")
    s = re.sub(r"[^\w가-힣_\-]", "", s, flags=re.UNICODE)
    return s.strip("_") or "case"


def _pick_reference_pdf(folder: Path) -> Path | None:
    """Pick best designer-produced reference PDF.

    Priority:
      1) latest 수정N*.pdf (higher N = newer revision)
      2) 수정*.pdf
      3) single designer PDF matching "<법원> <계> MMDD(...단).pdf" pattern
         (the non-modified final rendition)
    Reject auto-generated ones (`.auto.pdf`, `.final.pdf`, `.indesign.pdf`).
    """
    revised: list[tuple[int, float, Path]] = []
    plain: list[Path] = []
    for p in folder.iterdir():
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        name = p.name
        low = name.lower()
        if any(skip in low for skip in (".auto.pdf", ".final.pdf", ".indesign.pdf")):
            continue
        m = re.match(r"수정(\d*)\s", name)
        if m:
            rank = int(m.group(1)) if m.group(1) else 1
            revised.append((rank, p.stat().st_mtime, p))
            continue
        # Looks like a designer final PDF: 법원 or 지방법원 name + MMDD
        if re.search(r"\d{4}\s*\(\d+단\)\.pdf$", name):
            plain.append(p)
    if revised:
        revised.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return revised[0][2]
    if plain:
        # Pick most recently modified.
        plain.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return plain[0]
    return None


def _find_hwp(folder: Path) -> Path | None:
    """Prefer CS_*.hwp without 송 suffix; fallback to any CS_*.hwp."""
    cs = [p for p in folder.glob("CS_*.hwp") if p.is_file()]
    if not cs:
        return None
    # Prefer non-송/수정.
    def score(p: Path) -> tuple[int, float]:
        name = p.name
        penalty = 0
        if "-송" in name or "송.hwp" in name:
            penalty += 2
        if "수정" in name:
            penalty -= 1  # actually prefer (수정) source
        return (penalty, -p.stat().st_mtime)
    cs.sort(key=score)
    return cs[0]


def collect_pairs() -> list[dict]:
    """Return list of {folder, hwp, ref_pdf, case_id, slug}."""
    pairs: list[dict] = []
    for sub in sorted(SOURCE_ROOT.iterdir()):
        if not sub.is_dir():
            continue
        hwp = _find_hwp(sub)
        if not hwp:
            continue
        ref = _pick_reference_pdf(sub)
        if not ref:
            continue
        pairs.append({
            "folder": sub,
            "hwp": hwp,
            "ref_pdf": ref,
            "case_id": sub.name,
            "slug": _slug(sub.name),
        })
    return pairs


# ---------------------------------------------------------------------------
# Per-case processing
# ---------------------------------------------------------------------------


def _run_with_timeout(fn, *args, timeout_sec=PER_CASE_TIMEOUT_SEC, **kwargs):
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn, *args, **kwargs)
        return fut.result(timeout=timeout_sec)


def process_case(
    pair: dict,
    *,
    pass_label: str,
    render_pdf_for_case: bool = False,
) -> dict:
    folder = pair["folder"]
    hwp = pair["hwp"]
    ref_pdf = pair["ref_pdf"]
    case_id = pair["case_id"]
    slug = pair["slug"]

    out_dir = OUTPUT_ROOT / pass_label / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    status = {
        "case_id": case_id,
        "slug": slug,
        "folder": str(folder),
        "hwp": str(hwp),
        "ref_pdf": str(ref_pdf),
        "pass": pass_label,
        "ok": False,
        "score": None,
        "mismatches": [],
        "error": "",
        "html_path": None,
        "pdf_path": None,
    }
    start = time.time()

    try:
        # 1) build normalized
        _, json_path = _run_with_timeout(
            build_document, hwp, out_dir, timeout_sec=PER_CASE_TIMEOUT_SEC
        )
        doc = json.loads(Path(json_path).read_text(encoding="utf-8"))

        # 2) render html (also produces per-entry rendered list for scoring)
        rendered_entries: list[dict] = []
        entries_raw = []
        for e in doc.get("entries") or []:
            if compact_common(e.get("usage", "")) in SUSPICIOUS_USAGE:
                continue
            entries_raw.append(e)
            try:
                rendered_entries.append(format_entry(e))
            except Exception as fe:
                log(f"  ! {case_id}: format_entry failed: {fe}")
                # Fallback: minimal placeholder keeping case info.
                rendered_entries.append({
                    "case": "\n".join(e.get("case_numbers") or []),
                    "item": e.get("item_number", ""),
                    "locations": [],
                    "usages": [],
                    "note": "",
                    "group": "기타",
                    "price": "",
                })
        # Render full html for archival.
        html = render_html(doc)
        html_path = out_dir / "final.html"
        html_path.write_text(html, encoding="utf-8")
        status["html_path"] = str(html_path)

        # 3) optional PDF.
        if render_pdf_for_case:
            pdf_path = out_dir / "final.pdf"
            try:
                render_pdf(html_path, pdf_path)
                status["pdf_path"] = str(pdf_path)
            except Exception as pe:
                log(f"  ! {case_id}: PDF render failed: {pe}")

        # 4) scoring vs reference PDF.
        sc = scorer.score_case(
            case_id=f"{case_id}#{pass_label}",
            pipeline_output=rendered_entries,
            reference_pdf=ref_pdf,
            log=True,
            entries_raw=entries_raw,
        )
        status["score"] = sc["score"]
        status["mismatches"] = sc["mismatches"]
        status["cell_scores"] = sc.get("cell_scores") or []
        status["source"] = sc.get("source")
        status["ok"] = True

        # 5) promotion (>= PROMOTE_THRESHOLD, default 0.9; task spec says 0.95).
        try:
            if sc["score"] is not None and sc["score"] >= 0.95:
                promoted = memory.promote_to_example(
                    case_id=case_id,
                    score=sc["score"],
                    entries=entries_raw,
                    rendered_entries=rendered_entries,
                    source=f"batch_{pass_label}",
                    note=f"Auto-promoted from batch pass {pass_label} (score={sc['score']:.3f}).",
                )
                status["promoted"] = str(promoted) if promoted else None
        except Exception as pe:
            log(f"  ! {case_id}: promotion failed: {pe}")

        # 6) record mismatches as corrections (low score only, to accumulate).
        if sc["score"] is not None and sc["score"] < 0.9:
            for mm in sc["mismatches"][:20]:
                try:
                    memory.record_correction(
                        case_id=case_id,
                        cell_key=mm.get("cell") or "",
                        before=mm.get("got") or "",
                        after=mm.get("want") or "",
                        reason=f"score={sc['score']:.3f} source={sc.get('source')}",
                        input_data=entries_raw,
                    )
                except Exception:
                    pass

        dur = time.time() - start
        label = f"{case_id}"
        if len(label) > 70:
            label = label[:67] + "..."
        log(
            f"  {label}: score={sc['score']:.3f} mism={len(sc['mismatches'])} "
            f"dur={dur:.1f}s source={sc.get('source')}"
        )

    except FTimeout:
        status["error"] = f"timeout after {PER_CASE_TIMEOUT_SEC}s"
        log(f"  TIMEOUT {case_id}")
    except Exception as ex:
        status["error"] = f"{type(ex).__name__}: {ex}"
        log(f"  ERROR {case_id}: {status['error']}")
        status["traceback"] = traceback.format_exc(limit=3)

    return status


# ---------------------------------------------------------------------------
# Aggregate + reporting
# ---------------------------------------------------------------------------


def summarize(pass_results: list[dict]) -> dict:
    scores = [r["score"] for r in pass_results if r.get("score") is not None]
    if not scores:
        return {
            "n": len(pass_results),
            "ok": 0,
            "skipped": len(pass_results),
            "scores": [],
        }
    s = sorted(scores)
    return {
        "n": len(pass_results),
        "ok": len(scores),
        "skipped": len(pass_results) - len(scores),
        "mean": statistics.fmean(scores),
        "median": statistics.median(scores),
        "min": min(scores),
        "max": max(scores),
        "p25": s[max(0, int(0.25 * len(s)) - 1)] if len(s) >= 4 else s[0],
        "p75": s[min(len(s) - 1, int(0.75 * len(s)))] if len(s) >= 4 else s[-1],
    }


def collect_mismatch_patterns(results: list[dict]) -> dict:
    """Aggregate mismatch patterns: by field type and top cell keys/snippets."""
    by_field: Counter = Counter()
    by_usage_style: Counter = Counter()
    by_location_head: Counter = Counter()
    by_note_keyword: Counter = Counter()

    for r in results:
        for mm in (r.get("mismatches") or []):
            cell = mm.get("cell") or ""
            got = mm.get("got") or ""
            # Field type
            m = re.search(r"\|(\w+)\|\d+$", cell)
            field = m.group(1) if m else "?"
            by_field[field] += 1
            if field == "usage":
                # extract usage text
                snippet = re.sub(r"\s+", " ", got.strip())[:20]
                by_usage_style[snippet] += 1
            elif field == "location":
                # take first 12 chars (주소 스타일)
                head = re.sub(r"\s+", " ", got.strip())[:18]
                by_location_head[head] += 1
            elif field == "note":
                # detect keywords
                for kw in ("일괄매각", "지분매각", "제시외", "농지취득", "토지별도",
                           "공유자우선매수", "기계기구목록", "유치권", "목록",
                           "분묘", "제시외물건매각제외", "근린시설"):
                    if kw in got:
                        by_note_keyword[kw] += 1

    return {
        "by_field": by_field.most_common(),
        "by_usage_style": by_usage_style.most_common(10),
        "by_location_head": by_location_head.most_common(10),
        "by_note_keyword": by_note_keyword.most_common(10),
    }


def write_report(
    first: list[dict],
    second: list[dict] | None,
    promotions: list[str],
    distill_info: dict,
    lessons_preview: str,
) -> None:
    lines: list[str] = ["# Batch Learning Report", ""]
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Pass 1 summary.
    s1 = summarize(first)
    lines.append("## Pass 1 Summary")
    lines.append("")
    lines.append(f"- Total cases: {s1['n']}")
    lines.append(f"- Scored: {s1['ok']}")
    lines.append(f"- Skipped/errored: {s1['skipped']}")
    if s1.get("scores") != []:
        lines.append(f"- Mean: {s1.get('mean', 0):.3f}")
        lines.append(f"- Median: {s1.get('median', 0):.3f}")
        lines.append(f"- Min: {s1.get('min', 0):.3f}")
        lines.append(f"- Max: {s1.get('max', 0):.3f}")
        lines.append(f"- p25: {s1.get('p25', 0):.3f}")
        lines.append(f"- p75: {s1.get('p75', 0):.3f}")
    lines.append("")

    # Top/Bottom.
    scored = sorted(
        [r for r in first if r.get("score") is not None],
        key=lambda r: -r["score"],
    )
    lines.append("### Top 10 (Pass 1)")
    lines.append("")
    for r in scored[:10]:
        lines.append(f"- {r['score']:.3f} — {r['case_id']}")
    lines.append("")
    lines.append("### Bottom 10 (Pass 1)")
    lines.append("")
    for r in scored[-10:][::-1]:
        lines.append(f"- {r['score']:.3f} — {r['case_id']} (mismatches={len(r.get('mismatches') or [])})")
    lines.append("")

    # Mismatch patterns.
    patt1 = collect_mismatch_patterns(first)
    lines.append("### Top mismatch patterns (Pass 1)")
    lines.append("")
    lines.append("By field:")
    for k, v in patt1["by_field"][:10]:
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("By usage style (got):")
    for k, v in patt1["by_usage_style"]:
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    lines.append("By location head (got):")
    for k, v in patt1["by_location_head"]:
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    lines.append("By note keyword:")
    for k, v in patt1["by_note_keyword"]:
        lines.append(f"- {k}: {v}")
    lines.append("")

    # Errors.
    errs = [r for r in first if r.get("error")]
    lines.append(f"### Errors/Skips (Pass 1): {len(errs)}")
    lines.append("")
    for r in errs[:20]:
        lines.append(f"- {r['case_id']}: {r['error']}")
    lines.append("")

    # Promotions.
    lines.append(f"## Promotions: {len(promotions)} case(s)")
    lines.append("")
    for p in promotions:
        lines.append(f"- {p}")
    lines.append("")

    # Distill info.
    lines.append("## Distill")
    lines.append("")
    lines.append(f"- Result: {distill_info}")
    lines.append("")
    if lessons_preview:
        lines.append("### lessons.md preview")
        lines.append("")
        lines.append("```")
        lines.append(lessons_preview[-2000:])
        lines.append("```")
        lines.append("")

    # Second pass.
    if second is not None:
        s2 = summarize(second)
        lines.append("## Pass 2 Summary")
        lines.append("")
        lines.append(f"- Total: {s2['n']}, Scored: {s2['ok']}, Skipped: {s2['skipped']}")
        if s2.get("mean") is not None:
            lines.append(f"- Mean: {s2.get('mean', 0):.3f}  (Δ vs P1: {s2.get('mean', 0) - s1.get('mean', 0):+.3f})")
            lines.append(f"- Median: {s2.get('median', 0):.3f}")
            lines.append(f"- Min: {s2.get('min', 0):.3f}")
            lines.append(f"- Max: {s2.get('max', 0):.3f}")
        lines.append("")

        # Improvement / regression table
        idx1 = {r["case_id"]: r for r in first}
        idx2 = {r["case_id"]: r for r in second}
        improved = []
        regressed = []
        for cid, r2 in idx2.items():
            r1 = idx1.get(cid)
            if not r1 or r1.get("score") is None or r2.get("score") is None:
                continue
            diff = r2["score"] - r1["score"]
            if diff > 0.005:
                improved.append((cid, r1["score"], r2["score"], diff))
            elif diff < -0.005:
                regressed.append((cid, r1["score"], r2["score"], diff))
        improved.sort(key=lambda t: -t[3])
        regressed.sort(key=lambda t: t[3])

        lines.append(f"### Improved cases (Pass1 < Pass2): {len(improved)}")
        lines.append("")
        for cid, s1v, s2v, d in improved[:20]:
            lines.append(f"- {cid}: {s1v:.3f} → {s2v:.3f} ({d:+.3f})")
        lines.append("")
        lines.append(f"### Regressed cases (Pass1 > Pass2): {len(regressed)}")
        lines.append("")
        for cid, s1v, s2v, d in regressed[:20]:
            lines.append(f"- {cid}: {s1v:.3f} → {s2v:.3f} ({d:+.3f})")
        lines.append("")

        # cases below 0.9 that improved
        fixed = [t for t in improved if t[1] < 0.9]
        lines.append(f"### Pass1<0.9 → improved in Pass2: {len(fixed)}")
        lines.append("")
        for cid, s1v, s2v, d in fixed[:20]:
            lines.append(f"- {cid}: {s1v:.3f} → {s2v:.3f}")
        lines.append("")

        # Still low in pass 2.
        still_low = sorted(
            [r for r in second if (r.get("score") or 0) < 0.9 and r.get("score") is not None],
            key=lambda r: r["score"],
        )
        lines.append(f"### Still <0.9 after Pass 2: {len(still_low)}")
        lines.append("")
        for r in still_low[:20]:
            lines.append(f"- {r['case_id']}: {r['score']:.3f} (mismatches={len(r.get('mismatches') or [])})")
        lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    log(f"Wrote report: {REPORT_PATH}")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-pass", action="store_true", help="Run only pass 1")
    parser.add_argument("--skip-distill", action="store_true")
    parser.add_argument("--pdf-threshold", type=float, default=0.9,
                        help="Pass 2: render PDF for cases below this score")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N (debug)")
    args = parser.parse_args()

    pairs = collect_pairs()
    log(f"Collected {len(pairs)} pairs (folders with CS_*.hwp + 수정*.pdf)")

    if args.limit:
        pairs = pairs[: args.limit]
        log(f"DEBUG: limiting to {len(pairs)} pairs")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    # -------------- Pass 1 --------------
    log("=== PASS 1: baseline (HTML + score, no PDF) ===")
    first: list[dict] = []
    for i, pair in enumerate(pairs, 1):
        log(f"[P1 {i}/{len(pairs)}] {pair['case_id']}")
        res = process_case(pair, pass_label="pass1", render_pdf_for_case=False)
        first.append(res)

    # Collect promotions (from filesystem marker)
    promotions_after: list[str] = []
    if memory.PROMOTIONS_PATH.exists():
        for line in memory.PROMOTIONS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if rec.get("source", "").startswith("batch_pass1"):
                    promotions_after.append(
                        f"{rec.get('case_id')} → {rec.get('path')} (score={rec.get('score',0):.3f})"
                    )
            except Exception:
                continue

    # -------------- Distill --------------
    distill_info = {"ok": False, "reason": "skipped"}
    if not args.skip_distill:
        corr = memory.load_corrections()
        log(f"Corrections on disk: {len(corr)}")
        try:
            distill_info = memory.distill_lessons(force=True)
            log(f"Distill: {distill_info}")
        except Exception as de:
            log(f"Distill failed: {de}")
            distill_info = {"ok": False, "error": str(de)}

    lessons_preview = ""
    if memory.LESSONS_PATH.exists():
        lessons_preview = memory.LESSONS_PATH.read_text(encoding="utf-8")

    # -------------- Pass 2 --------------
    second: list[dict] | None = None
    if not args.single_pass:
        log("=== PASS 2: post-lessons ===")
        # Bust LLM cache so refiner actually re-consults lessons.
        try:
            cache_path = KB_DIR / "cache" / "llm_cache.json"
            if cache_path.exists():
                backup = cache_path.with_suffix(".json.pass1.bak")
                shutil.copy2(cache_path, backup)
                cache_path.write_text("{}", encoding="utf-8")
                log(f"  Reset llm_cache.json (backup: {backup.name})")
        except Exception as ce:
            log(f"  Could not reset llm cache: {ce}")
        # Also reset retrieval query cache
        try:
            q = KB_DIR / "cache" / "retrieval_query.json"
            if q.exists():
                q.write_text("{}", encoding="utf-8")
        except Exception:
            pass

        # Force reload modules relying on cache.
        import importlib
        import llm_refiner  # type: ignore
        importlib.reload(llm_refiner)
        # re-import render_final_notice so its `from llm_refiner import refine` picks up new module
        import render_final_notice as _rfn  # type: ignore
        importlib.reload(_rfn)

        second = []
        # Decide which to render PDF for based on pass 1 scores.
        low_ids = {
            r["case_id"] for r in first
            if r.get("score") is not None and r["score"] < args.pdf_threshold
        }
        for i, pair in enumerate(pairs, 1):
            log(f"[P2 {i}/{len(pairs)}] {pair['case_id']}")
            render_pdf_flag = pair["case_id"] in low_ids
            # Use reloaded modules — but our import above is at module scope; to ensure
            # reloaded format_entry is used, call via the reloaded module.
            res = _process_case_using(
                _rfn, pair, pass_label="pass2", render_pdf_for_case=render_pdf_flag
            )
            second.append(res)

    # -------------- Report --------------
    write_report(first, second, promotions_after, distill_info, lessons_preview)
    # Also write raw jsons
    (OUTPUT_ROOT / "pass1_results.json").write_text(
        json.dumps(first, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if second is not None:
        (OUTPUT_ROOT / "pass2_results.json").write_text(
            json.dumps(second, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


def _process_case_using(rfn_module, pair, *, pass_label, render_pdf_for_case):
    """Like process_case but uses a (reloaded) render_final_notice module."""
    folder = pair["folder"]
    hwp = pair["hwp"]
    ref_pdf = pair["ref_pdf"]
    case_id = pair["case_id"]
    slug = pair["slug"]

    out_dir = OUTPUT_ROOT / pass_label / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    status = {
        "case_id": case_id,
        "slug": slug,
        "folder": str(folder),
        "hwp": str(hwp),
        "ref_pdf": str(ref_pdf),
        "pass": pass_label,
        "ok": False,
        "score": None,
        "mismatches": [],
        "error": "",
    }
    start = time.time()
    try:
        _, json_path = _run_with_timeout(
            build_document, hwp, out_dir, timeout_sec=PER_CASE_TIMEOUT_SEC
        )
        doc = json.loads(Path(json_path).read_text(encoding="utf-8"))
        rendered_entries = []
        entries_raw = []
        for e in doc.get("entries") or []:
            if rfn_module.compact_common(e.get("usage", "")) in SUSPICIOUS_USAGE:
                continue
            entries_raw.append(e)
            try:
                rendered_entries.append(rfn_module.format_entry(e))
            except Exception as fe:
                log(f"  ! {case_id}: format_entry failed: {fe}")
                rendered_entries.append({
                    "case": "\n".join(e.get("case_numbers") or []),
                    "item": e.get("item_number", ""),
                    "locations": [],
                    "usages": [],
                    "note": "",
                    "group": "기타",
                    "price": "",
                })
        html = rfn_module.render_html(doc)
        html_path = out_dir / "final.html"
        html_path.write_text(html, encoding="utf-8")
        status["html_path"] = str(html_path)
        if render_pdf_for_case:
            pdf_path = out_dir / "final.pdf"
            try:
                rfn_module.render_pdf(html_path, pdf_path)
                status["pdf_path"] = str(pdf_path)
            except Exception as pe:
                log(f"  ! {case_id}: PDF render failed: {pe}")
        sc = scorer.score_case(
            case_id=f"{case_id}#{pass_label}",
            pipeline_output=rendered_entries,
            reference_pdf=ref_pdf,
            log=True,
            entries_raw=entries_raw,
        )
        status["score"] = sc["score"]
        status["mismatches"] = sc["mismatches"]
        status["source"] = sc.get("source")
        status["ok"] = True
        if sc["score"] is not None and sc["score"] >= 0.95:
            try:
                promoted = memory.promote_to_example(
                    case_id=case_id,
                    score=sc["score"],
                    entries=entries_raw,
                    rendered_entries=rendered_entries,
                    source=f"batch_{pass_label}",
                )
                status["promoted"] = str(promoted) if promoted else None
            except Exception:
                pass
        dur = time.time() - start
        log(f"  {case_id[:70]}: score={sc['score']:.3f} dur={dur:.1f}s source={sc.get('source')}")
    except FTimeout:
        status["error"] = f"timeout after {PER_CASE_TIMEOUT_SEC}s"
        log(f"  TIMEOUT {case_id}")
    except Exception as ex:
        status["error"] = f"{type(ex).__name__}: {ex}"
        log(f"  ERROR {case_id}: {status['error']}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
