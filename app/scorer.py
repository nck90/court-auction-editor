#!/usr/bin/env python3
"""파이프라인 출력에 스코어를 부여.

입력:
  - pipeline_output: format_entry 결과(엔트리별 리스트) 또는 { 'entries': [...] } dict
  - reference_pdf: 선택. pdftotext 로 추출, 셀 단위 fuzzy match
  - user_feedback: 선택. list of {cell_key, verdict: up/down, fix: optional}

출력:
  {
    "score": 0.0-1.0,
    "cell_scores": [{"cell": "...", "score": float, "match": bool}],
    "mismatches": [{"cell": "...", "got": "...", "want": "..."}],
    "source": "pdf" | "feedback" | "none"
  }

스코어 집계 후 memory.log_run 에 기록한다.
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    from memory import log_run as _log_run
except Exception:
    try:
        from .memory import log_run as _log_run  # type: ignore
    except Exception:
        _log_run = None  # type: ignore


def _normalize_for_compare(s: str) -> str:
    s = (s or "").replace("\u00a0", " ")
    s = re.sub(r"\s+", "", s)
    return s


def _fuzzy(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    a2 = _normalize_for_compare(a)
    b2 = _normalize_for_compare(b)
    if a2 == b2:
        return 1.0
    return difflib.SequenceMatcher(a=a2, b=b2).ratio()


def _extract_pdf_text(pdf_path: Path) -> str:
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return out.stdout
    except Exception:
        return ""


def _iter_entries(pipeline_output: Any):
    if isinstance(pipeline_output, dict) and "entries" in pipeline_output:
        for e in pipeline_output["entries"]:
            yield e
    elif isinstance(pipeline_output, list):
        for e in pipeline_output:
            yield e


def _cell_key(case_num: str, item: str, field: str, row: int = 0) -> str:
    return f"{case_num}|item{item}|{field}|{row}"


def score_case(
    case_id: str,
    pipeline_output: Any,
    reference_pdf: Path | str | None = None,
    user_feedback: list[dict] | None = None,
    *,
    log: bool = True,
    entries_raw: list[dict] | None = None,
) -> dict:
    cell_scores: list[dict] = []
    mismatches: list[dict] = []
    source = "none"

    if user_feedback:
        source = "feedback"
        total_votes = 0
        up_votes = 0
        for fb in user_feedback:
            verdict = (fb.get("verdict") or "").lower()
            total_votes += 1
            s = 1.0 if verdict in {"up", "ok", "good", "1"} else 0.0
            if s >= 1.0:
                up_votes += 1
            cell_scores.append({
                "cell": fb.get("cell_key") or fb.get("cell"),
                "score": s,
                "match": s >= 1.0,
            })
            if s < 1.0 and (fb.get("fix") or fb.get("after")):
                mismatches.append({
                    "cell": fb.get("cell_key") or fb.get("cell"),
                    "got": fb.get("before") or "",
                    "want": fb.get("fix") or fb.get("after") or "",
                })
        total_score = (up_votes / total_votes) if total_votes else 1.0

    elif reference_pdf and Path(reference_pdf).exists():
        source = "pdf"
        pdf_text = _extract_pdf_text(Path(reference_pdf))
        pdf_norm = _normalize_for_compare(pdf_text)
        sum_s = 0.0
        n = 0
        for entry in _iter_entries(pipeline_output):
            case_nums = entry.get("case_numbers") or entry.get("case") or []
            if isinstance(case_nums, str):
                case_nums = [case_nums]
            primary_case = (case_nums[0] if case_nums else "").splitlines()[0]
            item = entry.get("item") or entry.get("item_number") or ""
            locations = entry.get("locations") or []
            usages = entry.get("usages") or []
            note = entry.get("note") or ""

            # 셀 단위: 각 location/usage/note를 정규화해서 PDF 안에 포함되는지 확인
            for r, loc in enumerate(locations):
                n += 1
                loc_norm = _normalize_for_compare(loc)
                # 긴 주소는 처음 40자 기준 검색
                probe = loc_norm[: max(20, len(loc_norm) // 2)]
                match = bool(probe and probe in pdf_norm)
                if match:
                    s = 1.0
                else:
                    # Tiered partial-prefix credit: pdftotext 열 교차로 인한 false
                    # negative 완화. 전체 probe 가 안 맞으면 더 짧은 prefix 가
                    # corpus 에 있는지 확인하고 길이에 비례한 점수를 준다.
                    partial = 0.0
                    for ratio in (0.75, 0.5, 0.33):
                        cut = int(len(probe) * ratio)
                        if cut >= 8 and probe[:cut] in pdf_norm:
                            partial = ratio * 0.95  # 최대 0.71 까지만
                            break
                    s = max(partial, _fuzzy_probe(loc_norm, pdf_norm))
                sum_s += s
                if s < 0.85:
                    mismatches.append({
                        "cell": _cell_key(primary_case, item, "location", r),
                        "got": loc,
                        "want": "",  # unknown exact target
                    })
                cell_scores.append({
                    "cell": _cell_key(primary_case, item, "location", r),
                    "score": s,
                    "match": s >= 0.85,
                })
            for r, ug in enumerate(usages):
                n += 1
                ug_norm = _normalize_for_compare(ug)
                match = bool(ug_norm and ug_norm in pdf_norm)
                s = 1.0 if match else 0.4 if ug_norm else 1.0
                sum_s += s
                cell_scores.append({
                    "cell": _cell_key(primary_case, item, "usage", r),
                    "score": s,
                    "match": s >= 0.85,
                })
            if note:
                n += 1
                note_norm = _normalize_for_compare(note)
                # Loose: each segment (split by '.') appears.
                # pdftotext 열 교차로 장문 segment가 잘려 나올 수 있어, strict
                # containment가 실패하면 prefix match + fuzzy 로 부분 점수.
                segs = [x for x in note_norm.split(".") if x]
                if segs:
                    total = 0.0
                    for seg in segs:
                        if seg in pdf_norm:
                            total += 1.0
                            continue
                        # Prefix tier (장문 키워드 쪼개짐 대응)
                        partial = 0.0
                        for ratio in (0.7, 0.5, 0.35):
                            cut = max(6, int(len(seg) * ratio))
                            if cut < len(seg) and seg[:cut] in pdf_norm:
                                partial = ratio
                                break
                        if partial == 0.0:
                            partial = _fuzzy_probe(seg, pdf_norm) * 0.9
                        total += partial
                    s = total / len(segs)
                else:
                    s = 1.0
                sum_s += s
                cell_scores.append({
                    "cell": _cell_key(primary_case, item, "note", 0),
                    "score": s,
                    "match": s >= 0.85,
                })
                if s < 0.85:
                    mismatches.append({
                        "cell": _cell_key(primary_case, item, "note", 0),
                        "got": note,
                        "want": "",
                    })
        total_score = (sum_s / n) if n else 1.0
    else:
        # No ground truth. Use structural heuristics: row length consistency.
        source = "none"
        sum_s = 0.0
        n = 0
        for entry in _iter_entries(pipeline_output):
            locations = entry.get("locations") or []
            usages = entry.get("usages") or []
            note = entry.get("note") or ""
            n += 1
            ok = 1.0
            if len(usages) > len(locations):
                ok -= 0.3
            if re.search(r"\.\.", note) or ".지분매각.지분매각" in note:
                ok -= 0.3
            sum_s += max(0.0, ok)
        total_score = (sum_s / n) if n else 1.0

    total_score = max(0.0, min(1.0, total_score))

    result = {
        "score": total_score,
        "cell_scores": cell_scores,
        "mismatches": mismatches,
        "source": source,
    }

    if log and _log_run:
        try:
            _log_run(
                case_id=case_id,
                input_data=entries_raw if entries_raw is not None else case_id,
                output_data=pipeline_output,
                score=total_score,
                mismatches=mismatches,
                cell_scores=cell_scores,
                extra={"source": source},
            )
        except Exception:
            pass

    return result


def _fuzzy_probe(probe: str, corpus: str) -> float:
    if not probe:
        return 1.0
    # Slide window
    best = 0.0
    step = max(1, len(probe) // 4)
    for i in range(0, max(1, len(corpus) - len(probe) + 1), step):
        window = corpus[i : i + len(probe)]
        ratio = difflib.SequenceMatcher(a=probe, b=window).ratio()
        if ratio > best:
            best = ratio
        if best >= 0.95:
            break
    return best
