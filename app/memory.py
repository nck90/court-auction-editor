#!/usr/bin/env python3
"""Memory / 학습 상태 저장소.

파일 레이아웃 (모두 /knowledge 내부):
  runs.jsonl          - 실행별 기록 (score, mismatches, input_hash, ...)
  corrections.jsonl   - 사용자 수정 기록 (cell_key, before, after, reason)
  promotions.jsonl    - promote_to_example() 로 승격된 케이스 (idempotency)
  lessons.md          - distill_lessons() 의 출력 (증류된 규칙)
  examples/<case>.md  - 고득점으로 승격된 예시 (build_examples.py 형식 호환)
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
KB_DIR = ROOT / "knowledge"
RUNS_PATH = KB_DIR / "runs.jsonl"
CORRECTIONS_PATH = KB_DIR / "corrections.jsonl"
PROMOTIONS_PATH = KB_DIR / "promotions.jsonl"
LESSONS_PATH = KB_DIR / "lessons.md"
EXAMPLES_DIR = KB_DIR / "examples"
DISTILL_STAMP = KB_DIR / "cache" / "last_distill.json"

PROMOTE_THRESHOLD = float(os.environ.get("LLM_LOOP_PROMOTE_THRESHOLD", "0.9"))
DISTILL_MIN = int(os.environ.get("LLM_LOOP_DISTILL_MIN", "20"))
DISTILL_COOLDOWN_SEC = int(os.environ.get("LLM_LOOP_DISTILL_COOLDOWN", "1800"))

OLLAMA_URL = os.environ.get("OLLAMA_URL", "https://ollama.hyphen.it.com/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:26b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "120"))


def _ensure_dir() -> None:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    (KB_DIR / "cache").mkdir(parents=True, exist_ok=True)
    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, record: dict) -> None:
    _ensure_dir()
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _input_hash(payload: Any) -> str:
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    else:
        text = str(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Run logging
# ---------------------------------------------------------------------------


def log_run(
    case_id: str,
    input_data: Any,
    output_data: Any,
    score: float | None = None,
    *,
    mismatches: list | None = None,
    cell_scores: list | None = None,
    model: str | None = None,
    extra: dict | None = None,
) -> dict:
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "case_id": case_id,
        "input_hash": _input_hash(input_data),
        "output_hash": _input_hash(output_data),
        "score": score if score is not None else None,
        "mismatches": mismatches or [],
        "cell_scores": cell_scores or [],
        "model": model or OLLAMA_MODEL,
        "llm_enabled": os.environ.get("USE_LLM_REFINER", "0") not in {"0", "", "false", "False"},
    }
    if extra:
        record.update(extra)
    _append_jsonl(RUNS_PATH, record)
    return record


# ---------------------------------------------------------------------------
# Corrections (👎 + fix)
# ---------------------------------------------------------------------------


def record_correction(
    case_id: str,
    cell_key: str,
    before: str,
    after: str,
    reason: str = "",
    *,
    input_data: Any = None,
) -> dict:
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "case_id": case_id,
        "cell_key": cell_key,
        "before": before,
        "after": after,
        "reason": reason or "",
        "input_hash": _input_hash(input_data) if input_data is not None else "",
    }
    _append_jsonl(CORRECTIONS_PATH, record)
    return record


def load_corrections() -> list[dict]:
    return _read_jsonl(CORRECTIONS_PATH)


def load_runs() -> list[dict]:
    return _read_jsonl(RUNS_PATH)


# ---------------------------------------------------------------------------
# Promotion — 고득점 + 미등록 케이스를 examples/ 로 복사
# ---------------------------------------------------------------------------


def _slug(name: str) -> str:
    import re

    s = name.replace(" ", "_").replace("/", "_")
    s = re.sub(r"[^\w가-힣_-]", "", s, flags=re.UNICODE)
    return s.strip("_") or "case"


def _promotion_seen(case_id: str) -> bool:
    for rec in _read_jsonl(PROMOTIONS_PATH):
        if rec.get("case_id") == case_id:
            return True
    return False


def promote_to_example(
    case_id: str,
    *,
    score: float,
    entries: list[dict] | None = None,
    rendered_entries: list[dict] | None = None,
    source: str = "auto",
    note: str = "",
) -> Path | None:
    """Promote a high-scoring case into knowledge/examples/<slug>.md.

    Only promotes if score >= PROMOTE_THRESHOLD and case not yet promoted.
    Returns the written path, or None if skipped.
    """
    if score is None or score < PROMOTE_THRESHOLD:
        return None
    if _promotion_seen(case_id):
        return None
    _ensure_dir()

    slug = _slug(case_id)
    out = EXAMPLES_DIR / f"auto_{slug}.md"
    # Don't overwrite hand-curated examples
    if out.exists():
        return None

    lines: list[str] = [f"# Example (auto-promoted): {case_id}", ""]
    if note:
        lines += [f"> {note}", ""]
    for idx, (entry, rendered) in enumerate(
        zip(entries or [], rendered_entries or []), start=1
    ):
        case_nums = entry.get("case_numbers") or []
        case_title = case_nums[0] if case_nums else f"entry{idx}"
        item = entry.get("item_number") or ""
        lines.append(f"## {case_title} 물건{item}")
        lines.append("")
        lines.append("### 입력 (raw)")
        lines.append("```json")
        lines.append(json.dumps({
            "case_numbers": entry.get("case_numbers"),
            "usage": entry.get("usage"),
            "note_lines": entry.get("note_lines"),
            "properties": entry.get("properties"),
        }, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
        lines.append("### 자동 파이프라인 출력")
        lines.append(f"- locations: `{rendered.get('locations')}`")
        lines.append(f"- usages: `{rendered.get('usages')}`")
        lines.append(f"- note: `{rendered.get('note')}`")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    _append_jsonl(PROMOTIONS_PATH, {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "case_id": case_id,
        "score": score,
        "path": str(out.relative_to(ROOT)),
        "source": source,
    })
    return out


# ---------------------------------------------------------------------------
# Lesson distillation (LLM)
# ---------------------------------------------------------------------------


def _last_distill_time() -> float:
    if not DISTILL_STAMP.exists():
        return 0.0
    try:
        return float(json.loads(DISTILL_STAMP.read_text(encoding="utf-8")).get("at", 0))
    except Exception:
        return 0.0


def _save_distill_stamp() -> None:
    _ensure_dir()
    DISTILL_STAMP.write_text(
        json.dumps({"at": time.time()}, ensure_ascii=False), encoding="utf-8"
    )


_UA = os.environ.get("OLLAMA_UA", "curl/8.1.2 (court-auction-learner)")


def _call_llm_for_distill(prompt: str) -> str:
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": _UA,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("response", "")).strip()


import re as _re


def _mask_case_numbers(text: str) -> str:
    """사건번호(예: 2024타경40279)를 placeholder로 치환.

    distill에 리터럴 사건번호가 들어가면 LLM이 번호마다 규칙을 만들어버리는
    문제가 있어 일반화된 토큰으로 바꾼다.
    """
    if not text:
        return text
    return _re.sub(r"\d{4}타경\d+", "<사건번호>", text)


def _is_useful_correction(c: dict) -> bool:
    """쓸모없는 correction 필터링."""
    before = (c.get("before") or "").strip()
    after = (c.get("after") or "").strip()
    # 둘 다 비어있으면 시그널 없음
    if not before and not after:
        return False
    # before==after 면 변화 없음
    if before == after:
        return False
    # 'after' 가 "내용이없음" 류의 placeholder만이면 가치 낮음
    if after in {"내용이없음", "내용이 없음", "-", "."} and not before:
        return False
    return True


def distill_lessons(force: bool = False) -> dict:
    """Group corrections into patterns and append to lessons.md.

    Rate-limited to DISTILL_COOLDOWN_SEC (default 30 min) unless force=True.
    Returns a dict with 'ok', 'reason', 'appended'.
    """
    corrections = load_corrections()
    if not force and len(corrections) < DISTILL_MIN:
        return {"ok": False, "reason": f"need {DISTILL_MIN} corrections, have {len(corrections)}"}
    if not force and (time.time() - _last_distill_time()) < DISTILL_COOLDOWN_SEC:
        return {"ok": False, "reason": "cooldown active"}

    useful = [c for c in corrections if _is_useful_correction(c)]
    samples = useful[-200:]
    if not samples:
        return {"ok": False, "reason": "no useful corrections after filtering"}
    rows = []
    for c in samples:
        rows.append(
            f"- cell={_mask_case_numbers(c.get('cell_key') or '')} | "
            f"before={_mask_case_numbers(c.get('before') or '')!r} | "
            f"after={_mask_case_numbers(c.get('after') or '')!r} | "
            f"reason={c.get('reason') or ''}"
        )
    prompt = (
        "당신은 한국 법원 경매 공고 편집 전문가다. 아래는 자동 출력에 대한 사용자 수정 기록이다.\n"
        "공통 패턴을 뽑아서 '이런 입력 → 이렇게 고쳐야 한다' 형식의 편집 규칙으로 증류하라.\n"
        "\n"
        "제약:\n"
        "- 구체적인 사건번호·주소·건물명을 리터럴로 인용하지 마라.\n"
        "  (사건번호는 이미 <사건번호> 로 마스킹되어 있다)\n"
        "- 전이 가능한 일반 규칙만 만들어라. 하나의 샘플에만 해당되는 규칙은 배제.\n"
        "- 'before 필드가 비어있으면 해당 필드는 비워짐' 같은 trivial 규칙은 제외.\n"
        "- 중복 제거하고 확실한 패턴만 남겨라.\n"
        "- 각 규칙은 한 줄, 불릿 리스트로만 작성.\n"
        "\n"
        "수정 기록:\n" + "\n".join(rows) + "\n\n"
        "출력 형식:\n"
        "- <규칙 1>\n"
        "- <규칙 2>\n"
        "...\n"
    )
    try:
        response = _call_llm_for_distill(prompt)
    except Exception as exc:
        return {"ok": False, "reason": f"llm error: {exc}"}

    if not response:
        return {"ok": False, "reason": "empty llm response"}

    _ensure_dir()
    existing = LESSONS_PATH.read_text(encoding="utf-8") if LESSONS_PATH.exists() else ""
    # Split into lines and dedup.
    existing_lines = {ln.strip() for ln in existing.splitlines() if ln.strip()}
    new_lines = []
    for ln in response.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln in existing_lines:
            continue
        new_lines.append(ln)
        existing_lines.add(ln)

    if not new_lines:
        _save_distill_stamp()
        return {"ok": True, "reason": "no new lessons", "appended": 0}

    header = f"\n\n## Distilled at {time.strftime('%Y-%m-%d %H:%M:%S')} (n={len(corrections)})\n"
    LESSONS_PATH.open("a", encoding="utf-8").write(header + "\n".join(new_lines) + "\n")
    _save_distill_stamp()
    return {"ok": True, "appended": len(new_lines)}


# Public convenience for retrieval layer — build lessons preamble for prompts.
def lessons_snippet(limit_chars: int = 3000) -> str:
    if not LESSONS_PATH.exists():
        return ""
    text = LESSONS_PATH.read_text(encoding="utf-8")
    return text[-limit_chars:]
