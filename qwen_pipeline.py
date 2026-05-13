"""
법원경매공고 편집 파이프라인 — editing_standards.md만이 유일한 기준.

설계 원칙:
  - 시스템 프롬프트는 editing_standards.md 전문(완전 참조)을 그대로 주입한다.
  - 추가적인 가이드/요약/규칙 나열을 작성하지 않는다 (md를 임의로 해석/축약 금지).
  - 단계 분할은 "이번 단계에 어떤 영역에만 변경을 가하는가"만 표시하고,
    실제 변환 규칙은 모두 md에서 모델이 직접 참조해 적용한다.

단계:
  1) extract     : 원문 → 구조 추출 (편집 X)
  2) refine_addr : 사건 1건의 locations 편집
  3) refine_note : 사건 1건의 note 편집
  4) classify    : 사건 1건 그룹 분류
  finalize       : Python — 자연정렬

병렬: 사건 단위 ThreadPoolExecutor (기본 4 워커).
"""

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional

import requests
from ai_errors import AIServiceError, normalize_ai_exception
from rules import (
    compact_keun_rin,
    compact_nongji,
    compact_particles,
    convert_same_address,
    convert_share_notation,
    strip_building_materials,
    strip_city_prefix,
    strip_spaces_in_note,
    strip_trailing_period,
)

BASE = Path(__file__).resolve().parent

QWEN_URL = os.environ.get("QWEN_URL", "https://qwen.hyphen.it.com/v1/chat/completions")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "mlx-community/Qwen3.6-35B-A3B-4bit")
QWEN_TOKEN = os.environ.get("QWEN_TOKEN", "")
QWEN_MAX_TOKENS = int(os.environ.get("QWEN_MAX_TOKENS", "16384"))
PIPELINE_WORKERS = int(os.environ.get("PIPELINE_WORKERS", "4"))

SHARE_KEYWORDS = ("지분", "갑구", "공유자", "소유권", "을구", "채무자")
AREA_RE = re.compile(r"\d+(?:\.\d+)?㎡|\d+평\d*홉?|\d+정보")
LOT_TOKEN_RE = re.compile(r"(?:산)?\d+(?:-\d+)*")
STANDARD_SHARE_CLAUSE_RE = re.compile(
    r"(갑구\s*\d+(?:-\d+)?번)\s*"
    r"([가-힣A-Za-z0-9㈜\(\)\.\-·\s]+?)\s*"
    r"(\d+(?:\.\d+)?)\s*분의\s*(\d+(?:\.\d+)?)\s*"
    r"지분\s*(전부|일부)?"
)
OWNER_SHARE_CLAUSE_RE = re.compile(
    r"(공유자|소유자|채무자)\s*"
    r"([가-힣A-Za-z0-9㈜\(\)\.\-·\s]+?)\s*"
    r"(?:지분\s*중?\s*)?"
    r"(\d+(?:\.\d+)?)\s*분의\s*(\d+(?:\.\d+)?)\s*"
    r"지분\s*(전부|일부)?"
)


def _load_standards() -> str:
    p = BASE / "editing_standards.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


EDITING_STANDARDS = _load_standards()


# 모든 단계 공통 시스템 프롬프트: editing_standards.md 전문만 + 한 줄 지시.
BASE_SYSTEM = (
    "다음은 법원경매공고 편집의 유일한 기준 문서다. 이 문서의 모든 조항을 "
    "그대로 적용해 사용자가 제공한 입력을 편집한다. 이 문서 외의 추가 규칙은 따르지 않는다.\n\n"
    "<편집 기준 (editing_standards.md)>\n"
    + EDITING_STANDARDS
    + "\n</편집 기준>\n"
)


def _extract_json_block(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def _post_qwen(
    messages: list,
    *,
    timeout: int = 600,
    max_tokens: int = QWEN_MAX_TOKENS,
    temperature: float = 0.1,
) -> dict:
    payload = {
        "model": QWEN_MODEL,
        "messages": messages,
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {QWEN_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(QWEN_URL, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        raise AIServiceError(normalize_ai_exception(e, endpoint=QWEN_URL)) from e
    choices = data.get("choices") or []
    if not choices:
        return {"content": "", "finish_reason": ""}
    msg = choices[0].get("message") or {}
    return {
        "content": (msg.get("content") or "").strip(),
        "finish_reason": choices[0].get("finish_reason") or "",
    }


def _call_json(
    messages: list,
    *,
    timeout: int = 600,
    max_tokens: int = QWEN_MAX_TOKENS,
    max_continuations: int = 5,
) -> dict:
    """Qwen 호출 → JSON 파싱. 잘리면 누적 이어쓰기 최대 max_continuations회."""
    accumulated = ""
    cur_messages = list(messages)
    last_err: Optional[json.JSONDecodeError] = None

    for attempt in range(max_continuations + 1):
        result = _post_qwen(cur_messages, timeout=timeout, max_tokens=max_tokens)
        cont = result["content"]
        if attempt > 0:
            cont = re.sub(r"^```(?:json)?\s*", "", cont).strip()
            cont = re.sub(r"```\s*$", "", cont).strip()
        if not cont:
            break
        accumulated += cont
        block = _extract_json_block(accumulated)
        try:
            return json.loads(block)
        except json.JSONDecodeError as e:
            last_err = e
        # finish_reason이 'stop'이면 모델은 끝났다고 봤지만 JSON이 깨졌다는 의미 → 더 시도해도 무의미
        if result.get("finish_reason") == "stop" and attempt > 0:
            break
        cur_messages = list(messages) + [
            {"role": "assistant", "content": accumulated},
            {
                "role": "user",
                "content": (
                    "직전 출력이 토큰 한도로 잘렸다. 처음부터 다시 만들지 말고, "
                    "마지막 글자에서 곧바로 이어서 출력해 완전한 JSON을 완성하라. "
                    "코드펜스/설명 금지, JSON만 출력."
                ),
            },
        ]

    raise RuntimeError(
        f"JSON 파싱 실패 ({max_continuations + 1}회 시도): "
        f"{last_err.msg if last_err else '응답 없음'}. 끝부분: {accumulated[-300:]!r}"
    )


def _location_address(loc: dict) -> str:
    return str(
        loc.get("address_raw")
        or loc.get("address")
        or loc.get("addr")
        or ""
    )


def _location_use(loc: dict) -> str:
    return str(
        loc.get("use_raw")
        or loc.get("use")
        or loc.get("detail")
        or ""
    )


def _merge_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\r", " ").replace("\n", " ")).strip()


def _normalize_use_text(text: str) -> str:
    cleaned = _merge_space(text)
    cleaned = strip_building_materials(cleaned)
    cleaned = compact_keun_rin(cleaned)
    return cleaned.strip(" ,.;")


def _normalize_address_text(text: str) -> str:
    cleaned = _merge_space(text)
    cleaned = strip_city_prefix(cleaned)
    cleaned = strip_building_materials(cleaned)
    cleaned = compact_keun_rin(cleaned)
    cleaned = convert_share_notation(cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.;")
    return cleaned


def _normalize_note_text(text: str) -> str:
    note = (text or "").replace("\r", "\n")
    note = re.sub(r"\n+", " ", note)
    note = compact_nongji(note)
    note = compact_particles(note)
    note = strip_spaces_in_note(note)
    note = strip_trailing_period(note)
    return note


NOTE_GUARD_KEYWORDS = (
    "일괄매각",
    "지분매각",
    "제시외",
    "목록",
    "유치권",
    "성립여부",
    "불분명",
    "농지취득자격증명",
    "토지별도등기",
    "공유자우선매수권",
    "매각물건명세서",
)


def _choose_note_text(raw_note: str, edited_note: str) -> str:
    raw_norm = _normalize_note_text(raw_note)
    edited_norm = _normalize_note_text(edited_note)
    if not raw_norm:
        return edited_norm
    if not edited_norm:
        return raw_norm

    raw_compact = re.sub(r"\s+", "", raw_norm)
    edited_compact = re.sub(r"\s+", "", edited_norm)
    if len(edited_compact) < max(8, int(len(raw_compact) * 0.65)):
        return raw_norm

    missing_keywords = [kw for kw in NOTE_GUARD_KEYWORDS if kw in raw_compact and kw not in edited_compact]
    if missing_keywords:
        return raw_norm
    return edited_norm


def _normalize_multiline(text: str) -> List[str]:
    lines: List[str] = []
    for raw_line in (text or "").replace("\r", "\n").split("\n"):
        merged = _merge_space(raw_line)
        if merged:
            lines.append(merged)
    return lines


def _source_location_text(loc: dict) -> str:
    return _merge_space(f"{_location_address(loc)} {_location_use(loc)}")


def _address_match_text(loc: dict) -> str:
    use = _merge_space(_location_use(loc))
    share_start = len(use)
    for marker in ("(", "[", "갑구", "공유자", "소유자", "채무자", "소유권"):
        idx = use.find(marker)
        if idx >= 0:
            share_start = min(share_start, idx)
    use_head = use[:share_start].strip()
    return _merge_space(f"{_location_address(loc)} {use_head}")


def _extract_share_candidates(text: str) -> List[str]:
    cleaned = _merge_space(text)
    out: List[str] = []
    for m in re.finditer(r"\(([^()]*(?:지분|갑구|공유자|소유권|채무자)[^()]*)\)", cleaned):
        out.append(m.group(1).strip())
    if not out and any(k in cleaned for k in SHARE_KEYWORDS):
        start = min((cleaned.find(k) for k in SHARE_KEYWORDS if k in cleaned), default=-1)
        if start >= 0:
            out.append(cleaned[start:].strip(" []()"))
    return out


def _strip_share_segments(text: str) -> str:
    cleaned = _merge_space(text)
    if not cleaned:
        return ""
    cleaned = re.sub(
        r"\(([^()]*(?:지분|갑구|공유자|소유권|채무자)[^()]*)\)",
        "",
        cleaned,
    )
    cut = len(cleaned)
    for marker in ("[", "갑구", "공유자", "소유자", "채무자", "소유권"):
        idx = cleaned.find(marker)
        if idx >= 0:
            cut = min(cut, idx)
    cleaned = cleaned[:cut]
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;")
    return cleaned


def _strip_share_segments_multiline(text: str) -> List[str]:
    lines: List[str] = []
    for line in _normalize_multiline(text):
        stripped = _strip_share_segments(line)
        if stripped:
            lines.append(stripped)
    return lines


def _normalize_share_clause(text: str) -> str:
    clause = _merge_space(text)
    clause = clause.replace("전 소유권 중", "").replace("전소유권중", "").strip(" []()")
    clause = clause.replace("지분 중 일부", "지분일부")
    clause = clause.replace("지분중일부", "지분일부")
    clause = convert_share_notation(clause)
    clause = _merge_space(clause)

    def repl_standard(m: re.Match) -> str:
        holder = re.sub(r"\s+", "", m.group(2))
        tail = m.group(5) or ""
        return f"{m.group(1).replace(' ', '')}{holder}{m.group(4)}/{m.group(3)}지분{tail}"

    clause = STANDARD_SHARE_CLAUSE_RE.sub(repl_standard, clause)

    def repl_owner(m: re.Match) -> str:
        holder = re.sub(r"\s+", "", m.group(2))
        tail = m.group(5) or ""
        return f"{holder}{m.group(4)}/{m.group(3)}지분{tail}"

    clause = OWNER_SHARE_CLAUSE_RE.sub(repl_owner, clause)
    clause = re.sub(r"\s+", "", clause)
    return clause


def _normalized_source_shares(loc: dict) -> List[str]:
    shares: List[str] = []
    seen = set()
    for cand in _extract_share_candidates(_source_location_text(loc)):
        normalized = _normalize_share_clause(cand)
        if normalized and normalized not in seen:
            seen.add(normalized)
            shares.append(normalized)
    return shares


def _prepare_locations_for_llm(source_locs: List[dict]) -> List[dict]:
    prepared: List[dict] = []
    for loc in source_locs:
        prepared_loc = dict(loc)
        if "address_raw" in prepared_loc:
            prepared_loc["address_raw"] = _strip_share_segments(str(prepared_loc.get("address_raw", "")))
        if "use_raw" in prepared_loc:
            prepared_loc["use_raw"] = _strip_share_segments(str(prepared_loc.get("use_raw", "")))
        if "address" in prepared_loc:
            prepared_loc["address"] = _strip_share_segments(str(prepared_loc.get("address", "")))
        if "use" in prepared_loc:
            prepared_loc["use"] = _strip_share_segments(str(prepared_loc.get("use", "")))
        prepared.append(prepared_loc)
    return prepared


def _has_share_text(text: str) -> bool:
    merged = _merge_space(text)
    return any(k in merged for k in SHARE_KEYWORDS)


def _lot_tokens(text: str) -> List[str]:
    return LOT_TOKEN_RE.findall(_merge_space(text))


def _best_output_index(raw_loc: dict, out_locs: List[dict], fallback_idx: int) -> int:
    if not out_locs:
        return 0
    src_text = _address_match_text(raw_loc)
    src_tokens = _lot_tokens(src_text)
    best_idx = min(fallback_idx, len(out_locs) - 1)
    best_score = -1
    for idx, loc in enumerate(out_locs):
        addr = _merge_space(loc.get("address", ""))
        score = 0
        if src_tokens:
            out_tokens = set(_lot_tokens(addr))
            score += sum(1 for tok in src_tokens if tok in out_tokens)
        if _location_address(raw_loc):
            raw_addr = strip_city_prefix(_merge_space(_location_address(raw_loc)))
            raw_addr = raw_addr.split("(", 1)[0].strip()
            if raw_addr and raw_addr in addr:
                score += 3
        if score > best_score:
            best_score = score
            best_idx = idx
    return best_idx


def _append_share_if_missing(address: str, share: str) -> str:
    if not share:
        return address
    if share in re.sub(r"\s+", "", address):
        return address
    bracketed = f"[{share}]"
    if bracketed in address:
        return address
    return f"{address.rstrip()} {bracketed}".strip()


def _remove_leading_use(lines: List[str], use: str) -> List[str]:
    if not lines:
        return []
    normalized_use = re.sub(r"\s+", "", _normalize_use_text(use or ""))
    if not normalized_use:
        return lines

    out = list(lines)
    first = out[0]
    compact_first = re.sub(r"\s+", "", _normalize_address_text(first))
    if compact_first.startswith(normalized_use):
        remainder = compact_first[len(normalized_use):].strip()
        if remainder:
            out[0] = remainder
        else:
            out = out[1:]
    return out


def _restore_multiline_layout(source_loc: dict, edited_loc: dict) -> dict:
    restored = dict(edited_loc)
    source_addr_lines = [_normalize_address_text(line) for line in _normalize_multiline(_location_address(source_loc))]
    source_detail_lines = [_normalize_address_text(line) for line in _strip_share_segments_multiline(_location_use(source_loc))]
    source_detail_lines = _remove_leading_use(source_detail_lines, restored.get("use", ""))
    multiline_lines = source_addr_lines + source_detail_lines

    if len(multiline_lines) <= 1:
        restored["address"] = _normalize_address_text(restored.get("address", ""))
        return restored

    restored_address = str(restored.get("address", "")).strip()
    share_suffixes = re.findall(r"\[[^\[\]]+\]", restored_address)
    if share_suffixes:
        tail = multiline_lines[-1] if multiline_lines else ""
        compact_tail = re.sub(r"\s+", "", tail)
        for suffix in share_suffixes:
            compact_suffix = re.sub(r"\s+", "", suffix)
            if compact_suffix not in compact_tail:
                tail = f"{tail} {suffix}".strip()
        multiline_lines[-1] = tail

    restored["address"] = "\n".join(multiline_lines)
    return restored


def _fallback_location_from_raw(loc: dict) -> dict:
    address = strip_city_prefix(_merge_space(_location_address(loc)))
    detail = strip_city_prefix(_strip_share_segments(_location_use(loc)))
    address = _normalize_address_text(address)
    detail = _normalize_address_text(detail)
    merged = _merge_space(f"{address} {detail}")
    shares = _normalized_source_shares(loc)
    for share in shares:
        merged = _append_share_if_missing(merged, share)
    use = _normalize_use_text(_strip_share_segments(str(loc.get("use_raw") or loc.get("use") or "")))
    return {"address": merged, "use": use}


def _enforce_location_invariants(source_locs: List[dict], out_locs: List[dict]) -> List[dict]:
    normalized_out: List[dict] = []
    for loc in out_locs or []:
        normalized_out.append(
            {
                "address": _normalize_address_text(str(loc.get("address", ""))),
                "use": _normalize_use_text(str(loc.get("use", ""))),
            }
        )

    if not normalized_out:
        normalized_out = [_fallback_location_from_raw(loc) for loc in source_locs]

    if len(normalized_out) != len(source_locs):
        normalized_out = [_fallback_location_from_raw(loc) for loc in source_locs]

    any_output_has_share = any(_has_share_text(loc.get("address", "")) for loc in normalized_out)
    source_share_lists = [_normalized_source_shares(loc) for loc in source_locs]
    any_source_has_share = any(source_share_lists)

    target_indexes: List[int] = []
    for raw_idx, raw_loc in enumerate(source_locs):
        raw_shares = source_share_lists[raw_idx]
        if not raw_shares:
            continue
        target_idx = _best_output_index(raw_loc, normalized_out, raw_idx)
        target_indexes.append(target_idx)
        for share in raw_shares:
            if share in re.sub(r"\s+", "", normalized_out[target_idx].get("address", "")):
                continue
            normalized_out[target_idx]["address"] = _append_share_if_missing(
                normalized_out[target_idx]["address"], share
            )

    share_source_count = sum(1 for shares in source_share_lists if shares)
    if any_source_has_share and len(set(target_indexes)) != share_source_count:
        normalized_out = [_fallback_location_from_raw(loc) for loc in source_locs]

    if any_source_has_share and not any(_has_share_text(loc.get("address", "")) for loc in normalized_out):
        normalized_out = [_fallback_location_from_raw(loc) for loc in source_locs]

    if any_source_has_share:
        for raw_idx, raw_loc in enumerate(source_locs):
            raw_shares = source_share_lists[raw_idx]
            if not raw_shares:
                continue
            target_idx = _best_output_index(raw_loc, normalized_out, raw_idx)
            target_text = re.sub(r"\s+", "", normalized_out[target_idx].get("address", ""))
            if not all(share in target_text for share in raw_shares):
                normalized_out = [_fallback_location_from_raw(loc) for loc in source_locs]
                break

    addresses = [loc.get("address", "") for loc in normalized_out]
    converted = convert_same_address(addresses)
    for idx, address in enumerate(converted):
        normalized_out[idx]["address"] = address

    if any_source_has_share and not any_output_has_share:
        for idx, loc in enumerate(normalized_out):
            loc["address"] = re.sub(r"\s{2,}", " ", loc["address"]).strip()

    return normalized_out


# -------------- 단계 1: 추출 --------------
# 원문에서 구조만 분리한다. 이 단계에선 편집 기준의 변환을 적용하지 않는다.

EXTRACT_USER_TMPL = """[1단계 — 구조 추출]

목적: 원문에서 헤더와 모든 사건/물건을 구조화한다. 이 단계에서는 편집 기준의 변환을 적용하지 마라(다음 단계에서 한다). 텍스트는 원문 그대로 보존한다.

출력 스키마:
{{
  "header": {{
    "damdang": "...",
    "sale_date": "...",
    "decision_date": "...",
    "location": "..."
  }},
  "records": [
    {{
      "case_no": "YYYY타경NNN",
      "dup_tag": "원문에 (중복)/(병합) 사건번호 있으면 'YYYY타경NNN[중복]' 또는 '...[병합]', 없으면 빈 문자열",
      "item_no": "물건번호 숫자만",
      "locations_raw": [
        {{"address_raw": "원문 소재지+상세내역(구조 및 면적) 그대로", "use_raw": "원문 용도 행 또는 용도구분"}}
      ],
      "price": "감정평가액 숫자·콤마",
      "min_price": "최저매각가격 숫자·콤마",
      "note_raw": "원문 비고 그대로"
    }}
  ]
}}

오직 JSON 한 개만. 코드펜스/설명 금지.

원문:
{raw}"""


def step_extract(raw_text: str) -> dict:
    messages = [
        {"role": "system", "content": BASE_SYSTEM},
        {"role": "user", "content": EXTRACT_USER_TMPL.format(raw=raw_text)},
    ]
    data = _call_json(messages, timeout=900, max_tokens=QWEN_MAX_TOKENS)
    data.setdefault("header", {})
    data.setdefault("records", [])
    return data


# -------------- 단계 2: locations 편집 --------------

ADDR_USER_TMPL = """[2단계 — 소재지·면적·용도 편집]

다음 사건 1건의 'locations_raw'(소재지+상세내역, 용도)를 위 편집 기준에 따라 편집해 'locations'로 출력한다. 다른 필드(note_raw 등)는 입력 그대로 보존한다.

출력 스키마:
{{
  "case_no": "...",
  "dup_tag": "...",
  "item_no": "...",
  "locations": [{{"address": "...", "use": "..."}}],
  "price": "...",
  "min_price": "...",
  "note_raw": "<입력 그대로>"
}}

오직 JSON 한 개만.

입력 사건 JSON:
{rec}"""


ADDR_CHUNK_SIZE = int(os.environ.get("ADDR_CHUNK_SIZE", "10"))


def _refine_address_one_call(rec: dict) -> dict:
    """단일 호출로 사건의 locations 편집."""
    messages = [
        {"role": "system", "content": BASE_SYSTEM},
        {"role": "user", "content": ADDR_USER_TMPL.format(rec=json.dumps(rec, ensure_ascii=False, indent=2))},
    ]
    out = _call_json(messages, timeout=600)
    for k in ("case_no", "dup_tag", "item_no", "price", "min_price", "note_raw"):
        if not out.get(k) and rec.get(k) is not None:
            out[k] = rec[k]
    if not out.get("locations"):
        out["locations"] = []
    return out


def _refine_address_single_location(rec_meta: dict, source_loc: dict) -> dict:
    single_rec = dict(rec_meta)
    single_rec["locations"] = [_prepare_locations_for_llm([source_loc])[0]]
    out = _refine_address_one_call(single_rec)
    enforced = _enforce_location_invariants([source_loc], out.get("locations") or [])
    return enforced[0]


def step_refine_address(rec: dict) -> dict:
    """소재지 행 수를 유지하기 위해 location 단위로 편집 후 재조립."""
    source_locs = rec.get("locations") or rec.get("locations_raw") or []
    rec = dict(rec)
    rec["locations"] = source_locs
    rec.pop("locations_raw", None)

    edited: list = []
    meta = {
        "case_no": rec.get("case_no", ""),
        "dup_tag": rec.get("dup_tag", ""),
        "item_no": rec.get("item_no", ""),
        "price": rec.get("price", ""),
        "min_price": rec.get("min_price", ""),
        "note_raw": rec.get("note_raw", ""),
        "note": rec.get("note", ""),
    }
    for loc in source_locs:
        edited.append(_refine_address_single_location(meta, loc))

    edited = _enforce_location_invariants(source_locs, edited)
    edited = [
        _restore_multiline_layout(source_loc, out_loc)
        for source_loc, out_loc in zip(source_locs, edited)
    ]

    result = dict(rec)
    result["locations"] = edited
    return result


# -------------- 단계 3: note 편집 --------------

NOTE_USER_TMPL = """[3단계 — 비고 편집]

다음 사건 1건의 'note_raw'를 위 편집 기준에 따라 편집해 'note'로 출력한다. 다른 필드는 입력 그대로 보존한다. 'note_raw'는 결과에서 제외.

출력 스키마:
{{
  "case_no": "...",
  "dup_tag": "...",
  "item_no": "...",
  "locations": [...],
  "price": "...",
  "min_price": "...",
  "note": "<편집된 비고>"
}}

오직 JSON 한 개만.

입력 사건 JSON:
{rec}"""


def step_refine_note(rec: dict) -> dict:
    messages = [
        {"role": "system", "content": BASE_SYSTEM},
        {"role": "user", "content": NOTE_USER_TMPL.format(rec=json.dumps(rec, ensure_ascii=False, indent=2))},
    ]
    out = _call_json(messages, timeout=600)
    # 빈 값/누락 필드는 입력으로 복원 (모델이 다른 필드를 빈 배열·문자열로 덮어쓰는 사고 방지)
    for k in ("case_no", "dup_tag", "item_no", "price", "min_price", "locations"):
        if not out.get(k) and rec.get(k) is not None:
            out[k] = rec[k]
    out.pop("note_raw", None)
    out["note"] = _choose_note_text(
        rec.get("note_raw", "") or rec.get("note", ""),
        out.get("note", ""),
    )
    return out


# -------------- 단계 4: 그룹 분류 --------------

GROUP_VALUES = [
    "아파트",
    "연립주택/다세대/빌라",
    "단독주택,다가구주택",
    "상가/오피스텔,근린시설",
    "대지/임야/전답",
    "기타",
    # 게재제외: 자동차·선박 사건. 신문 게재본에 안 들어감 (편집 기준 운영 보강 E)
    "게재제외",
]

CLASSIFY_USER_TMPL = """[4단계 — 그룹 분류]

다음 사건의 group을 위 편집 기준의 표 수정 5번에 따라 다음 8개 중 하나로 결정한다(편집 기준에 명시된 값 외에는 사용 금지):
{groups}

다른 필드는 입력 그대로 보존한다.

출력 스키마:
{{
  "case_no": "...",
  "dup_tag": "...",
  "item_no": "...",
  "locations": [...],
  "price": "...",
  "min_price": "...",
  "note": "...",
  "group": "<8개 중 하나>"
}}

오직 JSON 한 개만.

입력 사건 JSON:
{rec}"""


def step_classify(rec: dict) -> dict:
    # 사용자 검수 학습 예시 (in-context few-shot)
    try:
        from learning import find_similar, format_examples_block
        examples_block = format_examples_block(find_similar(rec, top_k=3))
    except Exception:  # noqa: BLE001
        examples_block = ""

    user_msg = CLASSIFY_USER_TMPL.format(
        groups=" | ".join(f'"{g}"' for g in GROUP_VALUES),
        rec=json.dumps(rec, ensure_ascii=False, indent=2),
    )
    if examples_block:
        user_msg = examples_block + "\n" + user_msg

    messages = [
        {"role": "system", "content": BASE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    out = _call_json(messages, timeout=300, max_tokens=4096)
    for k in ("case_no", "dup_tag", "item_no", "price", "min_price", "locations", "note"):
        if not out.get(k) and rec.get(k) is not None:
            out[k] = rec[k]
    g = (out.get("group") or "").strip()
    if g not in GROUP_VALUES:
        out["group"] = "기타"
    return out


# -------------- finalize --------------

_NUM_RE = re.compile(r"(\d+)")


def _natural_key(s: str):
    out = []
    for tok in _NUM_RE.split(s or ""):
        if tok.isdigit():
            out.append((1, int(tok)))
        else:
            out.append((0, tok))
    return tuple(out)


def _record_sort_key(rec: dict):
    return (
        _natural_key(rec.get("case_no", "")),
        _natural_key(str(rec.get("item_no", ""))),
    )


def finalize(records: List[dict]) -> List[dict]:
    """그룹 통일(사건 단위) + 자연정렬."""
    # 같은 사건번호의 records가 서로 다른 group을 가지면 다수결로 통일
    by_case = {}
    for r in records:
        cn = r.get("case_no", "")
        by_case.setdefault(cn, []).append(r)
    for cn, recs in by_case.items():
        if len(recs) <= 1:
            continue
        groups = [r.get("group", "기타") for r in recs]
        if len(set(groups)) == 1:
            continue
        # 다수결: 가장 많은 group, 동률이면 게재제외/기타가 아닌 것 우선
        from collections import Counter
        counts = Counter(groups).most_common()
        # tie-break: 게재제외/기타 외 다른 그룹 우선
        non_other = [(g, c) for g, c in counts if g not in ("기타", "게재제외")]
        winner = (non_other[0][0] if non_other else counts[0][0])
        for r in recs:
            r["group"] = winner
    return sorted(records, key=_record_sort_key)


# -------------- 메인 파이프라인 --------------

def run_pipeline(
    raw_text: str,
    *,
    on_progress: Optional[Callable[[str], None]] = None,
    on_progress_detail: Optional[Callable[[dict], None]] = None,
) -> dict:
    def report(msg: str, **kw):
        if on_progress:
            on_progress(msg)
        if on_progress_detail:
            on_progress_detail({"phase": msg, **kw})

    report("구조 추출 중…", stage="extract", percent=2)
    extracted = step_extract(raw_text)
    header = extracted.get("header", {}) or {}
    raw_records = extracted.get("records", []) or []
    report(f"구조 추출 완료 — 사건 {len(raw_records)}건 발견",
           stage="extract", percent=10, total=len(raw_records))

    def process_one(rec: dict) -> dict:
        if "locations_raw" in rec and "locations" not in rec:
            rec["locations"] = rec.pop("locations_raw")
        rec = step_refine_address(rec)
        rec = step_refine_note(rec)
        rec = step_classify(rec)
        return rec

    refined: List[Optional[dict]] = [None] * len(raw_records)
    completed = [0]
    lock = threading.Lock()

    def task(i: int, r: dict):
        try:
            res = process_one(r)
        except AIServiceError:
            raise
        except Exception as e:  # noqa: BLE001
            res = {
                "case_no": r.get("case_no", "미상"),
                "dup_tag": r.get("dup_tag", ""),
                "item_no": r.get("item_no", ""),
                "locations": [{"address": "[편집 실패]", "use": ""}],
                "price": r.get("price", ""),
                "min_price": r.get("min_price", ""),
                "note": f"편집 실패: {str(e).strip() or 'AI 처리 중 오류가 발생했습니다.'}",
                "group": "기타",
            }
        with lock:
            refined[i] = res
            completed[0] += 1
            total = len(raw_records) or 1
            pct = 10 + int(80 * completed[0] / total)
            report(
                f"사건 {completed[0]}/{total} 편집 완료 — {r.get('case_no', '?')}",
                stage="edit",
                percent=pct,
                total=total,
                done=completed[0],
            )

    with ThreadPoolExecutor(max_workers=PIPELINE_WORKERS) as ex:
        futures = [ex.submit(task, i, r) for i, r in enumerate(raw_records)]
        for fut in as_completed(futures):
            exc = fut.exception()
            if exc:
                raise exc

    final_records = finalize([r for r in refined if r is not None])
    report("정렬·마무리 완료", stage="finalize", percent=92)
    return {"header": header, "records": final_records}
