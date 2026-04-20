#!/usr/bin/env python3
"""LLM refiner - post-processes rule-based format_entry output using an LLM
when the rule-based output is ambiguous / likely wrong.

Design:
- The rule-based pipeline is the default; refiner only corrects specific cells.
- Every refinement is cached on a deterministic key.
- Failures fallback to rule-based output silently.
- Gated by env var `USE_LLM_REFINER=1` (default off to preserve current CI).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
KB_DIR = ROOT / "knowledge"
EXAMPLES_DIR = KB_DIR / "examples"
CACHE_PATH = KB_DIR / "cache" / "llm_cache.json"
RULES_PATH = KB_DIR / "rules.md"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "https://ollama.hyphen.it.com/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "60"))

_cache: dict[str, Any] | None = None


def _load_cache() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    if CACHE_PATH.exists():
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _fingerprint(payload: dict[str, Any]) -> str:
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]


def is_enabled() -> bool:
    return os.environ.get("USE_LLM_REFINER", "0") not in {"0", "", "false", "False"}


# ---------------------------------------------------------------------------
# Heuristics-based fixups that don't need the LLM. These encode patterns we
# learned from the 편집 기준 PDF + real examples.
# ---------------------------------------------------------------------------


def _heuristic_fix_locations(entry: dict, rendered: dict) -> dict:
    """Apply deterministic post-fixups.

    Input `entry` is the raw normalized JSON entry (case_numbers, usage,
    properties, note_lines). `rendered` is the output of format_entry.
    Returns a potentially updated `rendered` dict.
    """
    properties = entry.get("properties") or []
    locs = list(rendered.get("locations") or [])
    usages = list(rendered.get("usages") or [])

    # Fix 1: 토지 용도 코드 (잡종지, 공장용지, 창고용지 등)는 소재지 셀이 아닌
    # 용도 셀에 들어간다. 현재 로직은 format_property 수정으로 area는 소재지에
    # 남고 용도는 infer_usage 에서 처리되므로, 여기서는 별도 조치 불필요.
    # (기존 heuristic이 잘못된 삽입을 했으므로 제거)

    # Fix 2: 근린시설 각사무소 - collapse 상세 with 사무소 notation.
    for i, block in enumerate(properties):
        if i >= len(locs):
            continue
        details = block.get("details") or []
        has_office = False
        floor_parts: list[tuple[str, str]] = []
        for d in details:
            m = re.match(r"^(\d+층|지하\d*층)\s*제[12]종근린생활시설\(사무소\)\s*(\d+(?:\.\d+)?)㎡", d.strip())
            if m:
                has_office = True
                floor_parts.append((m.group(1), m.group(2)))
        if has_office and floor_parts:
            # Build new location: address + floor-area pairs + "각사무소".
            address_part = locs[i]
            # Strip earlier label and floor summary that were assembled from the same details
            # We rebuild from address only (first token group before floor info).
            # Heuristic: the rendered line starts with address words; detect first floor-area token.
            # Simpler: replace existing "근린시설" label plus floor tokens with new sequence.
            addr_match = re.match(r"^(.+?)(\s+근린시설|\s+\d+층|\s+지하)", address_part)
            addr = addr_match.group(1) if addr_match else address_part
            floor_text = " ".join(f"{f}{a}㎡" for f, a in floor_parts)
            locs[i] = f"{addr} {floor_text} 각사무소".strip()

    # Fix 3: 다용도 층별 보존 (유흥주점, 계단실, 기계실 등)
    for i, block in enumerate(properties):
        if i >= len(locs):
            continue
        details = block.get("details") or []
        typed_floors: list[tuple[str, str, str]] = []  # (floor, area, type)
        for d in details:
            m = re.match(
                r"^(지하\d*층|\d+층)\s*:?\s*(\d+(?:\.\d+)?)㎡\s*[\(\[]([^)\]]+)[\)\]]\s*$",
                d.strip(),
            )
            if m:
                typed_floors.append((m.group(1), m.group(2), m.group(3)))
        if typed_floors:
            # Group consecutive same-area + same-type floors
            address_part = locs[i]
            # Preserve any trailing "제시외 ..." annotation appended by the upstream
            # attribution logic.
            trailing_jesi = ""
            m_jesi = re.search(r"(\s+제시외\s+.+)$", address_part)
            if m_jesi:
                trailing_jesi = m_jesi.group(1)
                address_part = address_part[: m_jesi.start()].rstrip()
            # Need address with full address prefix (not just first word)
            m2 = re.match(r"^(.*?)(\s+(?:지하\d*층|\d+층).*)$", address_part)
            addr = m2.group(1) if m2 else address_part.split()[0]
            parts = []
            j = 0
            prev_floor: str | None = None
            while j < len(typed_floors):
                floor, area, typ = typed_floors[j]
                # Try to group consecutive floors with same area/type
                if re.match(r"^\d+층$", floor):
                    start_n = int(floor[:-1])
                    end_n = start_n
                    k = j + 1
                    while (
                        k < len(typed_floors)
                        and re.match(r"^\d+층$", typed_floors[k][0])
                        and int(typed_floors[k][0][:-1]) == end_n + 1
                        and typed_floors[k][1] == area
                        and typed_floors[k][2] == typ
                    ):
                        end_n = int(typed_floors[k][0][:-1])
                        k += 1
                    if end_n > start_n:
                        parts.append(f"{start_n}∼{end_n}층{typ}각{area}㎡")
                        prev_floor = f"{end_n}층"
                    else:
                        # Same floor as previous? Drop floor prefix.
                        if prev_floor == floor:
                            parts.append(f"{typ}{area}㎡")
                        else:
                            parts.append(f"{floor}{typ}{area}㎡")
                        prev_floor = floor
                    j = k
                else:
                    if prev_floor == floor:
                        parts.append(f"{typ}{area}㎡")
                    else:
                        parts.append(f"{floor}{typ}{area}㎡")
                    prev_floor = floor
                    j += 1
            locs[i] = f"{addr} {' '.join(parts)}{trailing_jesi}".strip()

    # Fix 4: `상가,오피\n스텔등` → single line if usage cell only has one row.
    for idx, u in enumerate(usages):
        if len(locs) == 1 and "상가,오피\n스텔등" in u:
            usages[idx] = u.replace("상가,오피\n스텔등", "상가,오피스텔등")

    # Fix 5: 위락시설/숙박시설 + 근린생활시설 섞인 경우 용도 라벨 교정.
    for i, block in enumerate(properties):
        if i >= len(usages):
            continue
        details_joined = " ".join(block.get("details") or [])
        has_amusement = "위락시설" in details_joined
        has_lodging = "숙박시설" in details_joined
        has_store = "근린생활시설" in details_joined or "근린시설" in details_joined
        if has_amusement and (has_store or has_lodging):
            usages[i] = "위락시설,\n근린시설"

    # Fix 6: 공장 label 중복 제거 - location에 `공장` 라벨이 있고 usage도 `공장`이면 location에서 제거.
    for i in range(len(locs)):
        if i >= len(usages):
            continue
        if usages[i].strip() == "공장" and "공장" in locs[i]:
            # Only remove a lone 공장 between tokens (not 공장용지).
            locs[i] = re.sub(r"(?<!용)(?<![가-힣])공장\s+", "", locs[i])
            locs[i] = re.sub(r"\s+공장(?![가-힣])", "", locs[i])
            locs[i] = re.sub(r"\s+", " ", locs[i]).strip()

    # Fix 7: 단층 prefix 복원 - detail에 단층공장/단층주택 등 있는데 location에서 누락되었으면 복원.
    for i, block in enumerate(properties):
        if i >= len(locs):
            continue
        details_joined = " ".join(block.get("details") or [])
        dan_match = re.search(r"단층\s*(공장|주택|사무소|창고|농기계수리점|근린시설|[가-힣]+)", details_joined)
        if dan_match and "단층" not in locs[i]:
            # Place 단층 right before the first area token.
            m = re.search(r"(\d+(?:\.\d+)?㎡)", locs[i])
            if m:
                idx0 = m.start()
                locs[i] = locs[i][:idx0].rstrip() + " 단층" + locs[i][idx0:]
                locs[i] = re.sub(r"\s+", " ", locs[i]).strip()

    # Fix 8: `각사무소` 위치 - 소재지 끝에 '각사무소'를 두는 편집기준을 적용.
    # (근린시설 용도+상세내 '사무소' 있을 때)
    for i, block in enumerate(properties):
        if i >= len(locs):
            continue
        details = block.get("details") or []
        has_office = any("사무소" in d for d in details)
        if has_office and "사무소" not in locs[i]:
            locs[i] = locs[i].rstrip() + " 각사무소"

    # Fix 9: `단층농기계수리점` → `단층 농기계수리점` (편집기준: 용도+㎡는 붙여쓰기, 단층은 띄어쓰기)
    for i in range(len(locs)):
        locs[i] = re.sub(r"단층(농기계수리점)", r"단층 \1", locs[i])

    rendered["locations"] = locs
    rendered["usages"] = usages
    rendered["location"] = "\n".join(locs)
    rendered["usage"] = "\n".join(usages)
    return rendered


# ---------------------------------------------------------------------------
# LLM-based refinement (used only when explicitly enabled).
# ---------------------------------------------------------------------------


def _load_rules_snippet() -> str:
    if not RULES_PATH.exists():
        return ""
    text = RULES_PATH.read_text(encoding="utf-8")
    # Take the essential sections to keep prompt small.
    return text


def _load_examples(usage_hint: str, limit: int = 3, *, features: dict | None = None) -> list[str]:
    """Return up to `limit` example markdown blobs.

    Prefers embedding/TF-IDF retrieval when features dict is provided,
    falls back to keyword heuristic otherwise.
    """
    if not EXAMPLES_DIR.exists():
        return []
    # Attempt semantic retrieval first.
    if features is not None:
        try:
            from retrieval import top_k  # local import so refiner stays importable standalone
            hits = top_k(features, k=limit)
            out: list[str] = []
            for h in hits:
                p = ROOT / h["path"]
                try:
                    out.append(p.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    continue
            if out:
                return out
        except Exception:
            pass

    picked: list[str] = []
    files = sorted(EXAMPLES_DIR.glob("*.md"))
    hint = (usage_hint or "").replace("\n", "")
    ranked: list[tuple[int, Path]] = []
    for fp in files:
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        score = 0
        if hint and hint in content:
            score += 5
        score += min(3, len(content) // 500)
        ranked.append((score, fp))
    ranked.sort(key=lambda x: -x[0])
    for _, fp in ranked[:limit]:
        try:
            picked.append(fp.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
    return picked


def _load_lessons_snippet(limit_chars: int = 2000) -> str:
    try:
        from memory import lessons_snippet
        return lessons_snippet(limit_chars=limit_chars)
    except Exception:
        return ""


def _load_corrections_preamble(limit: int = 8) -> str:
    """Recent user corrections as 'avoid these mistakes' context."""
    try:
        from memory import load_corrections
        corrections = load_corrections()[-limit:]
    except Exception:
        return ""
    if not corrections:
        return ""
    rows = []
    for c in corrections:
        rows.append(
            f"- {c.get('cell_key','?')}: {c.get('before','')!r} → "
            f"{c.get('after','')!r}{(' (' + c['reason'] + ')') if c.get('reason') else ''}"
        )
    return "과거 수정 사례 (같은 실수를 반복하지 마라):\n" + "\n".join(rows)


_UA = os.environ.get("OLLAMA_UA", "curl/8.1.2 (court-auction-learner)")


def _call_ollama(prompt: str) -> str:
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(
            {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # WAF on ollama.hyphen.it.com blocks the default Python urllib UA.
            "User-Agent": _UA,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("response", "")).strip()


def _llm_refine(entry: dict, rendered: dict) -> dict:
    """Ask an LLM to correct the rendered locations/usages/note.

    Returns updated rendered dict, or the input unchanged on any failure.
    """
    cache = _load_cache()
    key_payload = {
        "case_numbers": entry.get("case_numbers"),
        "item_number": entry.get("item_number"),
        "usage": entry.get("usage"),
        "note_lines": entry.get("note_lines"),
        "properties": entry.get("properties"),
        "rendered": {
            "locations": rendered.get("locations"),
            "usages": rendered.get("usages"),
            "note": rendered.get("note"),
        },
        "model": OLLAMA_MODEL,
    }
    key = _fingerprint(key_payload)
    if key in cache:
        cached = cache[key]
        if isinstance(cached, dict) and cached.get("ok"):
            cached_locs = cached.get("locations") or rendered.get("locations") or []
            # 오래된 캐시에 LLM 프롬프트 토큰이 섞여있으면 정화. 심하게 오염된 경우 캐시 무효화.
            cleaned = []
            dirty = False
            for x in cached_locs:
                if not isinstance(x, str):
                    continue
                stripped = re.sub(r"^\s*(?:\d{4}타경\d+|아파트|단독주택|다세대|오피스텔|근린시설|상가|연립주택|빌라|대|전|답|임야|잡종지|도로|공장|item\d+)\s*(?:\|\s*(?:item\d+|\d{4}타경\d+|[가-힣]+)\s*)*\|\s*", "", x.strip()).strip()
                if "|" in stripped or stripped != x.strip():
                    dirty = True
                if stripped and not re.fullmatch(r"\d{4}타경\d+", stripped):
                    cleaned.append(stripped)
            # 원본 property 수보다 cleaned loc 가 훨씬 많으면 오염 의심 → 캐시 무효화.
            raw_count_c = len(entry.get("properties") or [])
            if raw_count_c and len(cleaned) > raw_count_c * 2:
                dirty = True
            if dirty:
                # 캐시 삭제, 재호출 유도
                cache.pop(key, None)
                _save_cache()
            else:
                out = dict(rendered)
                out["locations"] = cleaned or (rendered.get("locations") or [])
                out["usages"] = cached.get("usages") or rendered.get("usages")
                out["note"] = cached.get("note", rendered.get("note"))
                out["location"] = "\n".join(out["locations"] or [])
                out["usage"] = "\n".join(out["usages"] or [])
                return out
        elif isinstance(cached, dict) and cached.get("ok") is False:
            return rendered

    rules = _load_rules_snippet()[:6000]
    examples = _load_examples(entry.get("usage") or "", limit=2, features=entry)
    examples_block = "\n\n".join(examples)[:3000] if examples else ""
    lessons = _load_lessons_snippet(limit_chars=1500)
    corrections_pre = _load_corrections_preamble(limit=6)

    prompt = f"""당신은 한국 법원 경매 공고 편집자다. 아래 편집 규칙과 예시를 참고해서, 자동 파이프라인 출력(rendered)을 사람이 편집한 최종본과 일치하도록 수정하라. JSON 만 출력하고 설명을 붙이지 마라.

**중요 제약 (위반 시 결과 거부됨)**:
1. locations 의 각 주소는 반드시 [원본 입력]의 properties[].address 또는 usage 에 실제로 존재하는 주소여야 한다. 새 주소를 만들어 내거나, 예시에서 가져와서는 안 된다.
2. [예시] 섹션은 **포맷 참고용**이다. 예시의 주소·건물명·지번을 현재 케이스 출력에 절대로 복사하지 마라.
3. locations 라인 수는 원본 properties 수와 같거나 그보다 적어야 한다 (동소 축약 가능).
4. note 에는 [원본 입력]의 note_lines 에 있는 내용과 편집 규칙으로부터 도출되는 키워드만 허용된다.

[편집 규칙]
{rules}

[증류된 교훈]
{lessons}

[{corrections_pre}]

[예시 — 포맷 참고용, 주소 복사 금지]
{examples_block}

[원본 입력 — 주소의 유일한 정답 소스]
{json.dumps({
    'case_numbers': entry.get('case_numbers'),
    'item_number': entry.get('item_number'),
    'usage': entry.get('usage'),
    'note_lines': entry.get('note_lines'),
    'properties': entry.get('properties'),
}, ensure_ascii=False, indent=2)}

[현재 자동 출력]
locations = {json.dumps(rendered.get('locations'), ensure_ascii=False)}
usages = {json.dumps(rendered.get('usages'), ensure_ascii=False)}
note = {json.dumps(rendered.get('note'), ensure_ascii=False)}

편집 규칙에 따라 수정된 결과를 아래 JSON 스키마로만 반환:
{{"locations": [...], "usages": [...], "note": "..."}}
"""

    try:
        text = _call_ollama(prompt)
    except Exception as exc:
        # Log the failure to the runs log so we can track fallback rates.
        try:
            import logging
            logging.getLogger("llm_refiner").warning("LLM call failed: %s", exc)
        except Exception:
            pass
        cache[key] = {"ok": False, "at": time.time(), "err": str(exc)[:200]}
        _save_cache()
        return rendered

    # Extract JSON from text.
    parsed = None
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            parsed = None

    if not isinstance(parsed, dict):
        cache[key] = {"ok": False, "at": time.time(), "raw": text[:500]}
        _save_cache()
        return rendered

    loc = parsed.get("locations") or rendered.get("locations")
    usg = parsed.get("usages") or rendered.get("usages")
    nt = parsed.get("note", rendered.get("note"))
    if not isinstance(loc, list) or not isinstance(usg, list) or not isinstance(nt, str):
        cache[key] = {"ok": False, "at": time.time()}
        _save_cache()
        return rendered

    # LLM이 프롬프트 템플릿 토큰을 output에 섞어 반환하는 경우 정화.
    # 예: "2024타경7277|세종..." / "2024타경118030|item1|세종..." / "2024타경118917" 단독
    def _sanitize_loc(line: str) -> str:
        if not isinstance(line, str):
            return ""
        line = line.strip()
        # 제거: "YYYY타경NNNN|" 또는 "YYYY타경NNNN|itemN|" 앞부분
        line = re.sub(r"^\s*(?:\d{4}타경\d+|아파트|단독주택|다세대|오피스텔|근린시설|상가|연립주택|빌라|대|전|답|임야|잡종지|도로|공장|item\d+)\s*(?:\|\s*(?:item\d+|\d{4}타경\d+|[가-힣]+)\s*)*\|\s*", "", line)
        # 소재지 텍스트엔 '|' 가 나올 일이 없음 — 공백으로 치환.
        line = line.replace("|", " ")
        line = re.sub(r"\s+", " ", line)
        return line.strip()

    loc = [_sanitize_loc(x) for x in loc]
    loc = [x for x in loc if x and not re.fullmatch(r"\d{4}타경\d+", x)]
    if not loc:
        loc = rendered.get("locations") or []
    # LLM이 raw entry에 없는 추가 라인(비고 내용 등)을 소재지로 붙이는 것 방지.
    # 원본 property 수보다 많은 라인은 주소 패턴이 확실한 것만 남김.
    raw_count = len(entry.get("properties") or [])
    if raw_count and len(loc) > raw_count:
        addr_pat = re.compile(r"(?:시|군|구|읍|면|동|리|로|길|가)\s*\S*\s*\d")
        filtered = [x for x in loc if addr_pat.search(x) or re.search(r"\d+(?:\.\d+)?㎡", x)]
        if len(filtered) >= raw_count:
            loc = filtered[:raw_count * 2]  # 여유 2배까지만

    # Hallucination guard: LLM 출력 location 이 원본 entry 주소와 공통 토큰을
    # 충분히 공유하지 않으면 라인별로 drop. 모두 fail 하면 원본 heuristic 으로.
    raw_addresses = []
    for blk in (entry.get("properties") or []):
        addr = blk.get("address") if isinstance(blk, dict) else None
        if isinstance(addr, str) and addr:
            raw_addresses.append(addr)
    raw_usage = entry.get("usage") or ""
    if "㎡" in raw_usage or re.search(r"\d+평", raw_usage):
        raw_addresses.append(raw_usage)
    if raw_addresses:
        raw_norm = re.sub(r"\s+", "", " ".join(raw_addresses))

        def _shares_substring(a: str, b: str, min_len: int = 4) -> bool:
            for i in range(0, len(a) - min_len + 1):
                if a[i:i + min_len] in b:
                    return True
            return False

        filtered_loc = []
        for line in loc:
            ln = re.sub(r"\s+", "", line)
            if ln and _shares_substring(ln, raw_norm):
                filtered_loc.append(line)
            # else: drop this hallucinated line
        if filtered_loc:
            loc = filtered_loc
        else:
            # 모두 환각이면 heuristic fallback
            cache[key] = {"ok": False, "at": time.time(), "reason": "hallucination_guard_all"}
            _save_cache()
            return rendered
    # usage/loc 길이 맞춤
    if len(usg) > len(loc):
        usg = usg[: len(loc)]
    elif len(usg) < len(loc):
        usg = usg + [""] * (len(loc) - len(usg))

    # Sanity: location count shouldn't explode
    if len(loc) > max(1, len(rendered.get("locations") or [])) * 2 + 2:
        cache[key] = {"ok": False, "at": time.time()}
        _save_cache()
        return rendered

    cache[key] = {
        "ok": True,
        "locations": loc,
        "usages": usg,
        "note": nt,
        "at": time.time(),
    }
    _save_cache()

    out = dict(rendered)
    out["locations"] = loc
    out["usages"] = usg
    out["note"] = nt
    out["location"] = "\n".join(loc)
    out["usage"] = "\n".join(usg)
    return out


# ---------------------------------------------------------------------------
# Final deterministic normalizer — runs AFTER LLM refinement.
# LLM often reintroduces violations that format_entry already stripped (e.g.
# metro prefix). This pass re-applies the non-negotiable rules from rules.md.
# ---------------------------------------------------------------------------

_REGION_PREFIXES = (
    "서울특별시 ", "부산광역시 ", "대구광역시 ", "인천광역시 ", "광주광역시 ",
    "대전광역시 ", "울산광역시 ",
    "경기도 ", "강원특별자치도 ", "강원도 ",
    "충청북도 ", "충청남도 ",
    "전라북도 ", "전북특별자치도 ",
    "전라남도 ", "경상북도 ", "경상남도 ",
    "제주특별자치도 ", "제주도 ",
)


def _strip_region(line: str) -> str:
    for pref in _REGION_PREFIXES:
        if line.startswith(pref):
            return line[len(pref):]
    return line


def _final_normalize_location(line: str) -> str:
    if not isinstance(line, str):
        return ""
    s = line.strip()
    s = _strip_region(s)
    # 층과 호수 사이 공백: `9층913호` → `9층 913호` (단, `지하1층` 뒤는 건드리지 않음
    # because 지하층은 편집기준상 바로 면적이 붙는 경우가 더 많다)
    s = re.sub(r"(?<!지하)(\d+층)(\d+호)", r"\1 \2", s)
    # 쉼표 뒤 공백 제거
    s = re.sub(r",\s+", ",", s)
    # 다중 공백 → 단일 공백
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _final_normalize_note(note: str) -> str:
    if not isinstance(note, str):
        return ""
    s = note.strip()
    # `..` 제거
    s = re.sub(r"\.{2,}", ".", s)
    # 동일 키워드 반복 (`지분매각.지분매각` 같은)
    for kw in ("지분매각", "일괄매각", "제시외건물포함", "농지취득자격증명요",
               "분묘1기소재", "지상수목포함매각"):
        s = re.sub(rf"(?:\.?{kw}){{2,}}", f".{kw}" if s.startswith(".") or kw in s else kw, s)
        # safer: collapse adjacent duplicates
        s = re.sub(rf"({kw})(\.?)({kw})+", r"\1", s)
    # 쉼표 사용 금지 (비고는 마침표로만)
    # but careful: "일괄매각,지분매각" 를 "." 로 치환하는 건 편집기준과 맞음.
    s = s.replace(",", ".")
    # trailing punctuation 정리
    s = re.sub(r"\.+$", "", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = s.strip(".").strip()
    return s


def _final_normalize(rendered: dict) -> dict:
    locs = list(rendered.get("locations") or [])
    locs = [_final_normalize_location(x) for x in locs]
    locs = [x for x in locs if x]
    rendered["locations"] = locs
    rendered["location"] = "\n".join(locs)

    note = rendered.get("note")
    if isinstance(note, str) and note:
        rendered["note"] = _final_normalize_note(note)
    return rendered


def refine(entry: dict, rendered: dict) -> dict:
    """Public entry. Applies deterministic heuristics always; applies LLM only
    when `USE_LLM_REFINER` is enabled; then a final deterministic normalizer
    to enforce non-negotiable rules the LLM may reintroduce."""
    try:
        rendered = _heuristic_fix_locations(entry, rendered)
    except Exception:
        pass
    if is_enabled():
        try:
            rendered = _llm_refine(entry, rendered)
        except Exception:
            pass
    try:
        rendered = _final_normalize(rendered)
    except Exception:
        pass
    return rendered
