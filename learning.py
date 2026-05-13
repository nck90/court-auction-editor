"""
검수 학습 시스템.

- 사용자가 검수 UI에서 수정한 사건을 learned/corrections.jsonl에 누적
- 분류 단계(step_classify)가 호출 시 가장 유사한 학습 예시를 반환해 LLM 프롬프트에 포함
- 키워드 기반 단순 유사도 (외부 임베딩 의존 없이)
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, List

BASE = Path(__file__).resolve().parent
LEARN_DIR = BASE / "learned"
LEARN_DIR.mkdir(exist_ok=True)
CORR_FILE = LEARN_DIR / "corrections.jsonl"

KEYWORD_PATTERN = re.compile(r"[가-힣A-Za-z0-9]{2,}")
STOPWORDS = {
    "사건", "물건", "매각", "포함", "있음", "없음", "기타", "일부", "전부",
    "단독", "공유", "공부", "현황", "이용", "주택",
}


def _tokens(text: str) -> set:
    if not text:
        return set()
    return {t for t in KEYWORD_PATTERN.findall(text) if len(t) >= 2 and t not in STOPWORDS}


def _record_signature(record: dict) -> str:
    """사건의 핵심 키워드 추출 (소재지·용도·비고)."""
    parts = []
    for loc in record.get("locations") or []:
        parts.append(loc.get("address", ""))
        parts.append(loc.get("use", ""))
    parts.append(record.get("note", ""))
    return " ".join(parts)


def append_corrections(corrections: List[dict]):
    """수정 사례를 jsonl에 추가.
    각 항목: {timestamp, group, signature_text, locations, note, original_group}
    """
    if not corrections:
        return
    with CORR_FILE.open("a", encoding="utf-8") as f:
        for c in corrections:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


def load_examples(limit: int = 200) -> List[dict]:
    """가장 최근 학습 예시 limit개."""
    if not CORR_FILE.exists():
        return []
    lines = CORR_FILE.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def find_similar(record: dict, top_k: int = 3) -> List[dict]:
    """현재 사건과 키워드 유사도가 높은 예시 top_k개 반환."""
    examples = load_examples()
    if not examples:
        return []
    target_tokens = _tokens(_record_signature(record))
    if not target_tokens:
        return []

    scored = []
    for ex in examples:
        ex_tokens = _tokens(ex.get("signature_text", ""))
        if not ex_tokens:
            continue
        overlap = len(target_tokens & ex_tokens)
        if overlap == 0:
            continue
        # Jaccard 유사도
        union = len(target_tokens | ex_tokens)
        score = overlap / union
        scored.append((score, ex))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ex for _, ex in scored[:top_k]]


def format_examples_block(examples: List[dict]) -> str:
    """LLM 프롬프트에 넣을 학습 예시 블록 생성."""
    if not examples:
        return ""
    lines = ["[사용자 검수로 학습된 분류 예시]"]
    for i, ex in enumerate(examples, 1):
        sig = ex.get("signature_text", "")[:200].replace("\n", " ")
        lines.append(f"{i}. 사건: {ex.get('case_no','?')} → group=\"{ex.get('group','기타')}\"")
        lines.append(f"   특징: {sig}")
    lines.append("위 예시와 패턴이 비슷하면 같은 group으로 분류한다.")
    return "\n".join(lines) + "\n"


def make_correction_record(rec: dict, original_group: str, corrected_group: str) -> dict:
    """검수 후 저장용 레코드 생성."""
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "case_no": rec.get("case_no", ""),
        "item_no": rec.get("item_no", ""),
        "original_group": original_group,
        "group": corrected_group,
        "signature_text": _record_signature(rec),
        "locations": rec.get("locations") or [],
        "note": rec.get("note", ""),
    }


def stats() -> dict:
    """학습 데이터 통계."""
    examples = load_examples(limit=10**6)
    by_group = {}
    for ex in examples:
        g = ex.get("group", "?")
        by_group[g] = by_group.get(g, 0) + 1
    return {
        "total": len(examples),
        "by_group": by_group,
        "file": str(CORR_FILE),
    }
