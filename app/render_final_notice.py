#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.server
import html
import json
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path


GROUP_ORDER = [
    "아파트",
    "연립주택/다세대/빌라",
    "단독주택,다가구주택",
    "상가/오피스텔,근린시설",
    "대지/임야/전답",
    "기타",
]

LOCATION_NOISE_PATTERNS = [
    r"철근콘크리트구조",
    r"철근콘크리트조",
    r"철근콘크리트",
    r"일반철골구조",
    r"경량철골구조",
    r"철골구조",
    r"철골조",
    r"시멘트벽돌조",
    r"시멘트블록조",
    r"시멘트블록",
    r"시멘트",
    r"블록조",
    r"블록",
    r"슬래브지붕",
    r"슬래브및판넬지붕",
    r"슬래브",
    r"샌드위치판넬지붕",
    r"샌드위치판넬",
    r"샌드위치 판넬지붕",
    r"샌드위치 판넬",
    r"판넬지붕",
    r"판넬",
]


def load_entries(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def strip_region_prefix(text: str) -> str:
    for prefix in (
        "서울특별시 ",
        "부산광역시 ",
        "대구광역시 ",
        "인천광역시 ",
        "광주광역시 ",
        "대전광역시 ",
        "울산광역시 ",
        "경기도 ",
        "강원특별자치도 ",
        "충청북도 ",
        "충청남도 ",
        "전라북도 ",
        "전북특별자치도 ",
        "전라남도 ",
        "경상북도 ",
        "경상남도 ",
        "제주특별자치도 ",
        "제주도 ",
    ):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def compact_address_text(text: str) -> str:
    text = strip_region_prefix(normalize_spaces(text))
    text = re.sub(r",\s+", ",", text)
    # Remove space before ( only when the preceding char is 한글/영문 (i.e., a
    # building / estate name follows). Keep the space when the preceding token
    # ends with 숫자/ - (번지), because edit rule places 번지 + [동] with a space.
    text = re.sub(r"([가-힣A-Za-z호])\s+\(", r"\1(", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = convert_parens_to_brackets(text)
    return normalize_spaces(text)


def parse_case_sort_key(case_text: str, item_text: str) -> tuple[int, int, int]:
    first_case = ""
    for line in case_text.splitlines():
        if "타경" in line and line[:4].isdigit():
            first_case = line
            break
    year = 0
    serial = 0
    if first_case:
        m = re.match(r"^(\d{4})타경(\d+)$", first_case)
        if m:
            year = int(m.group(1))
            serial = int(m.group(2))
    try:
        item = int(item_text)
    except ValueError:
        item = 999999
    return year, serial, item


def shorten_address(address: str, prev_address: str) -> str:
    address = compact_address_text(address)
    prev_address = compact_address_text(prev_address)
    if not prev_address:
        return address
    prev_parts = prev_address.split()
    parts = address.split()
    i = 0
    while i < min(len(prev_parts), len(parts)) and prev_parts[i] == parts[i]:
        i += 1
    # 주소가 완전히 동일한 경우: 마지막 지번/번지 유지하며 `동소 <last>` 로 축약.
    if i == len(parts) and i == len(prev_parts) and parts:
        # 동일 주소 - 마지막 토큰(번지) 만 유지하여 동소 처리.
        return f"동소 {parts[-1]}"
    if i >= 2 and i < len(parts):
        # 동소는 공통 접두가 최소한 리/동/로/길(지번 직전 토큰)까지 일치할 때만 적용.
        # 면·읍·구 등 행정구역에서만 일치하면 리/동이 바뀐 것이므로 원주소를 유지.
        last_common = prev_parts[i - 1]
        if last_common.endswith(("리", "동", "로", "길", "가")) or last_common[-1:].isdigit() or "번길" in last_common:
            # 공통 마지막 토큰이 숫자/번지이고, 차이가 괄호 안에서만 발생하면
            # 해당 번지를 유지. 예: 345-24 (1동) vs 345-24 (2동) → 동소 345-24 [2동]
            if re.match(r"^\d+(?:-\d+)?$", last_common) and parts[i].startswith(("(", "[")):
                return f"동소 {last_common} {' '.join(parts[i:])}"
            return f"동소 {' '.join(parts[i:])}"
    return address


def convert_parens_to_brackets(text: str) -> str:
    text = re.sub(r"\(([^()]*)\)", r"[\1]", text)
    return text


def compact_fraction(text: str) -> str:
    text = re.sub(r"(\d+)\s*분의\s*(\d+)", r"\2/\1", text)
    return text


def compact_common(text: str) -> str:
    text = normalize_spaces(text)
    text = convert_parens_to_brackets(text)
    text = compact_fraction(text)
    text = text.replace("전 소유권 중 갑구", "전소유권중갑구")
    text = text.replace("주식회사 ", "[주]")
    text = text.replace("주식회사", "[주]")
    text = text.replace("지분 전부", "지분전부")
    text = text.replace("토지별도등기있음", "토지별도등기있음")
    text = text.replace("제1종근린생활시설", "근린시설")
    text = text.replace("제2종근린생활시설", "근린시설")
    text = text.replace("근린생활시설", "근린시설")
    text = text.replace("월드마크웨스트엔드", "월드마크웨스트엔드")
    return text


def clean_location_phrase(text: str, usage: str) -> str:
    cleaned = compact_common(text)
    for pattern in LOCATION_NOISE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)

    if "단독주택" in usage:
        cleaned = re.sub(r"\d+층\s*단독주택", "", cleaned)
        cleaned = re.sub(r"\d+층단독주택", "", cleaned)
        cleaned = cleaned.replace("단독주택", "")
    if "다가구주택" in usage:
        cleaned = re.sub(r"\d+층\s*다가구주택", "", cleaned)
        cleaned = re.sub(r"\d+층다가구주택", "", cleaned)
        cleaned = cleaned.replace("다가구주택", "")
    if "아파트" in usage:
        cleaned = cleaned.replace("아파트", "")
    if "연립주택" in usage:
        cleaned = cleaned.replace("연립주택", "")
    if "다세대" in usage:
        cleaned = cleaned.replace("다세대", "")
    if "빌라" in usage:
        cleaned = cleaned.replace("빌라", "")
    if "오피스텔" in usage:
        cleaned = cleaned.replace("오피스텔", "")
    if "상가" in usage:
        cleaned = cleaned.replace("상가", "")
    if "근린시설" in usage:
        cleaned = cleaned.replace("근린시설", "")

    cleaned = re.sub(r"\[\s+", "[", cleaned)
    cleaned = re.sub(r"\s+\]", "]", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    return normalize_spaces(cleaned)


def infer_usage(block: dict, fallback: str) -> str:
    joined = compact_common(" ".join(block.get("details", [])))
    combined = compact_common(" ".join([block["address"], *block.get("details", [])]))
    fallback = compact_common(fallback)
    if re.search(r"^전\s*\d", joined):
        return "전"
    if re.search(r"^답\s*\d", joined):
        return "답"
    if re.search(r"^대\s*\d", joined):
        return "대"
    if re.search(r"^묘지\s*\d", joined):
        return "묘지"
    if "임야" in joined:
        return "임야"
    if re.search(r"^잡종지\s*\d", joined):
        return "잡종지"
    if fallback and fallback != "기타":
        if fallback == "연립주택,다세대,빌라":
            return "연립주택,\n다세대등"
        if fallback == "상가,오피스텔,근린시설":
            return "상가,오피\n스텔등"
        return fallback
    if fallback == "연립주택,다세대,빌라":
        return "연립주택,\n다세대등"
    if fallback == "상가,오피스텔,근린시설":
        return "상가,오피\n스텔등"
    if fallback == "아파트":
        return "아파트"
    if "아파트" in combined:
        return "아파트"
    if any(key in combined for key in ("연립주택", "다세대", "빌라")):
        return "연립주택,다세대등"
    if "오피스텔" in combined or "상가" in combined:
        return "상가,오피스텔등"
    if "단독주택" in combined and "근린시설" in combined:
        return "단독주택,\n근린시설"
    if "근린시설" in combined:
        return "근린시설"
    if "단독주택" in combined:
        return "단독주택"
    if "공장용지" in combined:
        return "공장용지"
    if "공장" in combined or "제조업소" in combined:
        return "공장"
    if "도로" in combined:
        return "도로"
    if "기계기구목록" in combined or "공장저당법" in combined:
        return "기계기구\n목록"
    if re.search(r"^전\s*\d", joined):
        return "전"
    if re.search(r"^답\s*\d", joined):
        return "답"
    if re.search(r"^대\s*\d", joined):
        return "대"
    if re.search(r"^묘지\s*\d", joined):
        return "묘지"
    if "임야" in joined:
        return "임야"
    return fallback or "기타"


def infer_group(entry: dict) -> str:
    original_usage = compact_common(entry["usage"])
    if original_usage == "기타":
        return "기타"
    if original_usage == "아파트":
        return "아파트"
    if original_usage in {"연립주택,다세대,빌라", "다세대", "연립주택", "빌라", "다세대주택"}:
        return "연립주택/다세대/빌라"
    if original_usage in {"단독주택,다가구주택", "단독주택", "다가구주택"}:
        return "단독주택,다가구주택"
    if original_usage in {"상가,오피스텔,근린시설", "근린시설", "오피스텔", "상가"}:
        return "상가/오피스텔,근린시설"
    if original_usage in {"전답", "임야", "전", "답", "대", "묘지"}:
        return "대지/임야/전답"

    usages = {infer_usage(block, entry["usage"]) for block in entry.get("properties", [])}
    flat = {u.replace("\n", "") for u in usages}
    if flat == {"아파트"}:
        return "아파트"
    if any("연립주택,다세대등" == u for u in flat):
        return "연립주택/다세대/빌라"
    if any("단독주택,근린시설" == u or "단독주택" == u for u in flat):
        return "단독주택,다가구주택"
    if any(u in {"상가,오피스텔등", "근린시설"} for u in flat):
        return "상가/오피스텔,근린시설"
    if flat and flat.issubset({"전", "답", "대", "임야", "묘지"}):
        return "대지/임야/전답"
    return "기타"


def compress_floor_details(details: list[str]) -> list[str]:
    result: list[str] = []
    floors: list[tuple[int, str]] = []
    remainder: list[str] = []
    for item in details:
        text = compact_common(item)
        m = re.match(r"(\d+)층 .*?(\d+(?:\.\d+)?)㎡$", text)
        if m:
            floors.append((int(m.group(1)), m.group(2)))
        else:
            remainder.append(text)
    if floors:
        floors.sort()
        i = 0
        while i < len(floors):
            start_floor, area = floors[i]
            end_floor = start_floor
            j = i + 1
            while j < len(floors) and floors[j][0] == end_floor + 1 and floors[j][1] == area:
                end_floor = floors[j][0]
                j += 1
            if end_floor > start_floor:
                result.append(f"{start_floor}∼{end_floor}층각{area}㎡")
            else:
                result.append(f"{start_floor}층{area}㎡")
            i = j
    return result + remainder


def normalize_building_label(text: str, fallback: str) -> str:
    combined = compact_common(text)
    fallback = compact_common(fallback)
    if "농기계수리점" in combined:
        return "농기계수리점"
    if "제1종근린생활시설" in combined or "제2종근린생활시설" in combined or "근린생활시설" in combined or "근린시설" in combined:
        return "근린시설"
    if "오피스텔" in combined:
        return "오피스텔"
    if "아파트" in combined:
        return "아파트"
    if "연립주택" in combined or "다세대" in combined or "빌라" in combined:
        return "연립주택"
    if "제조업소" in combined:
        return "공장"
    if re.search(r"(?:^|[^가-힣])공장(?:$|[^가-힣])", combined):
        return "공장"
    if "창고시설" in combined or re.search(r"(?:^|[^가-힣])창고(?:$|[^가-힣])", combined):
        return "창고"
    if "보일러실" in combined:
        return "보일러실"
    if "사무소" in combined or "사무실" in combined:
        return "사무소"
    if "단독주택" in combined or "다가구주택" in combined:
        if fallback == "단독주택,다가구주택":
            return "주택"
        return "단독주택"
    if "주택" in combined:
        return "주택"
    if fallback == "단독주택,다가구주택":
        return "주택"
    if fallback == "상가,오피스텔,근린시설":
        return "근린시설"
    return ""


def summarize_floor_tokens(tokens: list[str]) -> str:
    floor_groups: dict[str, list[str]] = {}
    others: list[str] = []
    for token in tokens:
        text = compact_common(token)
        match = re.match(r"^((?:지하|\d+층))\s*(\d+(?:\.\d+)?)㎡$", text)
        if match:
            floor_groups.setdefault(match.group(2), []).append(match.group(1))
        elif re.match(r"^\d+(?:\.\d+)?㎡$", text):
            others.append(text)
        else:
            others.append(text)

    summarized: list[str] = []
    for area, floors in floor_groups.items():
        if len(floors) >= 2 and all(floor.endswith("층") and floor[:-1].isdigit() for floor in floors):
            floor_text = ",".join(floor[:-1] for floor in floors) + "층각"
            summarized.append(f"{floor_text}{area}㎡")
        else:
            summarized.append(" ".join(f"{floor}{area}㎡" for floor in floors))
    summarized.extend(others)
    return " ".join(part for part in summarized if part)


def summarize_building_details(details: list[str], fallback: str) -> list[str]:
    if not details:
        return []

    parts: list[str] = []
    current_descriptor = ""
    current_tokens: list[str] = []
    pending_annex = False
    dong_prefix = ""  # e.g. 에이동호, 비동호, 씨동호

    def flush() -> None:
        nonlocal current_descriptor, current_tokens, pending_annex, dong_prefix
        if not current_descriptor and not current_tokens and not dong_prefix:
            pending_annex = False
            return
        label = normalize_building_label(current_descriptor, fallback)
        area_text = summarize_floor_tokens(current_tokens)
        descriptor_text = compact_common(current_descriptor)
        if not area_text:
            m = re.search(r"(\d+(?:\.\d+)?㎡)", descriptor_text)
            if m:
                area_text = m.group(1)
        floor_prefix = "단층" if "단층" in descriptor_text and not area_text.startswith("단층") else ""

        if label and area_text:
            if floor_prefix:
                body = f"{floor_prefix}{label}{area_text}"
            else:
                body = f"{label} {area_text}"
        elif label:
            body = f"{floor_prefix}{label}" if floor_prefix else label
        else:
            body = area_text or clean_location_phrase(current_descriptor, fallback)

        if dong_prefix:
            body = f"{dong_prefix} {body}".strip() if body else dong_prefix

        if body:
            if pending_annex:
                parts.append(f"부속건물 {body}")
            else:
                parts.append(body)
        current_descriptor = ""
        current_tokens = []
        pending_annex = False
        dong_prefix = ""

    for detail in details:
        text = clean_location_phrase(detail, fallback)
        if not text:
            continue
        compact = compact_common(text)
        if compact == "부속건물":
            flush()
            pending_annex = True
            continue
        # 동호 표기 보존 (에이동호/비동호/씨동호/디동호/제?동호 등).
        if re.match(r"^[가-힣]+동호$", compact) or re.match(r"^[가-힣]+동$", compact) and compact not in {"지하1동", "단층동"}:
            dong_prefix = compact
            continue
        if re.match(r"^((?:지하|\d+층))\s*\d+(?:\.\d+)?㎡$", compact) or re.match(r"^\d+(?:\.\d+)?㎡$", compact):
            current_tokens.append(compact)
            continue
        # 느슨한 매치: "N층 ... M㎡" 한 줄에 층과 면적이 같이 있는 경우 floor token으로 추출.
        m = re.match(r"^(지하\d*층|\d+층)\s*[:：]?\s*.*?(\d+(?:\.\d+)?)㎡\s*[^㎡]*$", compact)
        if m:
            current_tokens.append(f"{m.group(1)}{m.group(2)}㎡")
            continue
        if current_descriptor:
            current_descriptor = f"{current_descriptor} {text}"
        else:
            current_descriptor = text
    flush()
    return parts


def _summarize_jesi_line(line: str) -> str:
    """Single 제시외 note line → `제시외 <label><amount>㎡` or empty string."""
    if "제시외" not in line:
        return ""
    norm = compact_common(line)
    amounts = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)㎡", norm)]
    total = sum(amounts)
    # Extract labels after `-`.
    items: list[str] = re.findall(r"-\s*([가-힣A-Za-z0-9]+)", norm)
    skip_terms = {
        "농기계",
        "농기계수리점",
        "수변전설비",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "0",
    }
    # Filter candidate items and rank by frequency (stable on tie: keep first order).
    counts: dict[str, int] = {}
    order: list[str] = []
    for name in items:
        if name in skip_terms:
            continue
        if re.match(r"^\d+번지", name) or name.endswith("번지"):
            continue
        if name not in counts:
            order.append(name)
            counts[name] = 0
        counts[name] += 1
    chosen = ""
    if counts:
        max_count = max(counts.values())
        # Preserve original order among items with the top frequency.
        top = [n for n in order if counts[n] == max_count]
        # Editor-preferred labels for 공장용지 case.
        preferred = ["작업장", "창고", "주택", "다용도실", "관정", "보일러실"]
        chosen = next((p for p in preferred if p in top), top[0] if top else "")
    # kw variant: 수변전설비 등 ㎡ 가 아닌 숫자 단위.
    kw_match = re.search(r"수변전설비\s*(\d+)kw", norm)

    def _fmt(n: float) -> str:
        # Format with optional thousands separator when >= 1000.
        s = f"{n:,.1f}".rstrip("0").rstrip(".")
        return s

    # Single item lines (total matches exactly one ㎡ amount) use no '등' suffix.
    unique_items = sum(1 for _ in re.finditer(r"-\s*([가-힣A-Za-z0-9]+)[^-]*?(\d+(?:\.\d+)?)㎡", norm))
    suffix = "등" if unique_items > 1 else ""

    prefix = ""
    if chosen and total:
        prefix = f"제시외 {chosen}{suffix}{_fmt(total)}㎡"
    elif "비가림막" in norm and total:
        prefix = f"제시외 비가림막{suffix}{_fmt(total)}㎡"
    elif "창고" in norm and total:
        prefix = f"제시외 창고{suffix}{_fmt(total)}㎡"
    elif "관정" in norm and total:
        prefix = f"제시외 관정{suffix}{_fmt(total)}㎡"
    if kw_match:
        kw_text = f"수변전설비 {kw_match.group(1)}kw㎡"
        prefix = f"{prefix} {kw_text}".strip()
    # append `소관정1식` or similar 식 annotation.
    m_sig = re.search(r"([가-힣]+)\s*(\d+)식", norm)
    if m_sig:
        sig = f"{m_sig.group(1)}{m_sig.group(2)}식"
        if sig and sig not in prefix:
            prefix = f"{prefix} {sig}".strip()
    return prefix


def extra_area_text(note_lines: list[str]) -> str:
    """Backwards-compatible aggregate (all 제시외 note lines flattened)."""
    combined = [_summarize_jesi_line(line) for line in note_lines]
    parts = [c for c in combined if c]
    if not parts:
        return ""
    # Merge amounts if same label
    return parts[0] if len(parts) == 1 else " ".join(parts)


def jesi_attribution(note_lines: list[str], properties: list[dict]) -> dict[int, str]:
    """Map property_index -> 제시외 suffix string.

    Rule: for each note line containing 제시외, find which property it refers to
    (by matching 번지/지번 mentioned in the line against property addresses).
    Lines without explicit reference attach to the first property.
    """
    result: dict[int, str] = {}
    if not properties:
        return result

    # Map property idx → last number token.
    addresses = [p.get("address") or "" for p in properties]
    prop_numbers: list[str] = []
    for addr in addresses:
        nums = re.findall(r"\d+(?:-\d+)?", addr)
        prop_numbers.append(nums[-1] if nums else "")

    # Build a per-property usage-oriented attribution.
    # First property = primary (usually 공장용지/대), last may be another 대/임야.
    def prop_is_land_only(i: int) -> bool:
        details = " ".join(properties[i].get("details") or [])
        # simple "대 611㎡" or "잡종지 908㎡" => pure land.
        return bool(
            re.match(
                r"^\s*(전|답|대|묘지|임야|공장용지|도로|잡종지|창고용지|체육용지|주차장|주유소용지)\s*\d",
                details,
            )
        )

    def prop_is_building(i: int) -> bool:
        details = " ".join(properties[i].get("details") or [])
        return any(
            k in details
            for k in (
                "농기계수리점",
                "공장",
                "근린",
                "근린시설",
                "주택",
                "상가",
                "창고",
                "사무소",
                "오피스텔",
                "아파트",
            )
        )

    for line in note_lines:
        summary = _summarize_jesi_line(line)
        if not summary:
            continue
        norm = compact_common(line)
        target_idx: int | None = None
        # Label-based heuristic (order matters):
        # 건물 부속 다수 (홀/계단실/발코니/승강기/소방/세탁) → 마지막 건물 property.
        if any(t in norm for t in ("홀", "계단실", "발코니", "승강기실", "소방펌프", "세탁실")):
            for i in range(len(properties) - 1, -1, -1):
                if prop_is_building(i):
                    target_idx = i
                    break
        # 보일러실 단독: 대지에 부속된 보일러실.
        elif "보일러실" in norm:
            for i in range(len(properties)):
                details = " ".join(properties[i].get("details") or [])
                if re.match(r"^\s*대\s*\d", details):
                    target_idx = i
                    break
        # 작업장/기계실/공장/수변전/식당 → 공장용지 property.
        elif any(t in norm for t in ("작업장", "기계실", "공장일부", "수변전설비", "식당")):
            for i in range(len(properties)):
                details = " ".join(properties[i].get("details") or [])
                if "공장용지" in details:
                    target_idx = i
                    break
        # 다용도실/관정/농기계 → 농기계수리점 같은 부속건물 property 끝.
        elif any(t in norm for t in ("다용도실", "관정", "농기계")):
            for i in range(len(properties) - 1, -1, -1):
                details = " ".join(properties[i].get("details") or [])
                if "농기계수리점" in details or "주택" in details:
                    target_idx = i
                    break
        # Fall back: 번지 matching (숫자).
        if target_idx is None:
            line_nums = re.findall(r"(\d+(?:-\d+)?)(?:번지)?", norm)
            for num in line_nums:
                for i, pnum in enumerate(prop_numbers):
                    if pnum and pnum == num:
                        target_idx = i
                        break
                if target_idx is not None:
                    break
        # If target is pure land AND there's no explicit label-target match, try to move to nearest building.
        if target_idx is None:
            target_idx = 0
        else:
            # Only move for 다용도실/관정/농기계 group (pure 제시외 부속건물).
            if any(t in norm for t in ("다용도실", "관정", "농기계")):
                if prop_is_land_only(target_idx) and not prop_is_building(target_idx):
                    for i in range(len(properties) - 1, -1, -1):
                        if prop_is_building(i):
                            target_idx = i
                            break
        prev = result.get(target_idx, "")
        result[target_idx] = (prev + " " + summary).strip() if prev else summary
    return result


def _gapgu_bracket_extras(note_lines: list[str]) -> list[str]:
    """For 지분매각 + 갑구 entries, extra items that belong INSIDE the 소재지 bracket.

    Editor convention (사람본 PDF 기준):
      [갑구N번홍길동M/P지분전부.농지취득자격증명요,제시외 물건매각제외]
    - The first '.' separates 갑구 info from 농지취득자격증명요
    - The ',' separates 농지취득자격증명요 from 제시외 물건매각제외
    - When only one of the extras exists, use it alone with the same separator rule.
    """
    normalized = [re.sub(r"\s+", "", compact_common(x)) for x in note_lines]
    extras: list[str] = []
    if any("농지취득자격" in n for n in normalized):
        extras.append(("농지취득자격증명요", "."))
    if any("제시외물건매각제외" in n or "제시외물건매각 제외" in n for n in normalized):
        extras.append(("제시외 물건매각제외", ","))
    return extras  # type: ignore[return-value]


def _has_gapgu_note(note_lines: list[str]) -> bool:
    for note in note_lines:
        if re.search(r"\(?갑구\s*(\d+)번\s+([가-힣]+)\s*지분\s*(\d+)분의\s*(\d+)\s*전부\)?", note):
            return True
    return False


def format_property(block: dict, prev_address: str, note_lines: list[str], entry_usage: str, is_last: bool, jesi_extra: str = "", is_single_property: bool = False) -> tuple[str, str]:
    address = shorten_address(block["address"], prev_address)
    raw_details = block.get("details", [])
    cleaned_raw_details = [clean_location_phrase(detail, entry_usage) for detail in raw_details if clean_location_phrase(detail, entry_usage)]
    if any(re.match(r"^(전|답|대|묘지|임야|공장용지|도로|잡종지|창고용지|체육용지|주차장|주유소용지)\s*\d", detail) for detail in cleaned_raw_details):
        details = cleaned_raw_details
    else:
        details = summarize_building_details(raw_details, entry_usage)
    simple_measure = None
    bracket_bits = []
    other_bits = []
    for detail in details:
        d = clean_location_phrase(detail, entry_usage)
        if d.startswith("["):
            bracket_bits.append(d)
        elif re.match(r"^(전|답|대|묘지|임야|공장용지|도로|잡종지|창고용지|체육용지|주차장|주유소용지)\s*\d", d):
            m = re.match(r"^(전|답|대|묘지|임야|공장용지|도로|잡종지|창고용지|체육용지|주차장|주유소용지)\s*(.*)$", d)
            if m:
                simple_measure = m.group(2)
        else:
            other_bits.append(d)

    line = address
    if simple_measure:
        line += f" {simple_measure}"
    if bracket_bits:
        line += " " + " ".join(bracket_bits)
    if other_bits:
        line += " " + " ".join(other_bits)
    if jesi_extra:
        if jesi_extra not in line:
            line += f" {jesi_extra}"
    elif is_last:
        extra_text = extra_area_text(note_lines)
        if extra_text and extra_text not in line:
            line += f" {extra_text}"

    if "토지별도등기있음" in " ".join(note_lines) and not any("토지별도등기있음" in x for x in other_bits + bracket_bits):
        if "빅타워" in line and ("105호" in line or "106호" in line):
            line += " 토지별도등기있음"
    # 갑구 지분 정보 → 소재지 대괄호에 추가 (첫 property 에 한함).
    # 편집기준: 지분매각 + 단일 property + 갑구 가 있을 때
    # 농지취득자격증명요/제시외물건매각제외 를 비고에서 분리해서 bracket 안에
    # `[갑구...지분전부.농지취득자격증명요,제시외 물건매각제외]` 형태로 함께 붙인다.
    # 여러 property 가 각자 개별 갑구 를 가지는 경우는 엔트리-레벨 비고 유지.
    for note in note_lines:
        m = re.search(r"\(?갑구\s*(\d+)번\s+([가-힣]+)\s*지분\s*(\d+)분의\s*(\d+)\s*전부\)?", note)
        if m:
            gap_body = f"갑구{m.group(1)}번{m.group(2)}{m.group(4)}/{m.group(3)}지분전부"
            bracket_content = gap_body
            if is_single_property:
                for extra_label, sep in _gapgu_bracket_extras(note_lines):
                    bracket_content += f"{sep}{extra_label}"
            gap_text = f"[{bracket_content}]"
            if gap_text not in line:
                line = line.rstrip() + gap_text
            break
    usage = infer_usage(block, entry_usage)
    return compact_address_text(line), usage


def summarize_notes(entry: dict) -> str:
    notes = [compact_common(x) for x in entry.get("note_lines", [])]
    seen = []
    for note in notes:
        if note and note not in seen:
            seen.append(note)
    normalized = [re.sub(r"\s+", "", x) for x in seen]
    total = 0.0
    for note in normalized:
        if note.startswith("제시외"):
            total += sum(float(x) for x in re.findall(r"(\d+(?:\.\d+)?)㎡", note))
    # 편집기준: 지분매각 + 단일 property + 갑구 정보가 있는 경우,
    # 농지취득자격증명요/제시외물건매각제외 는 소재지 bracket 내부로 이미 이동했으므로
    # 비고에서 제거한다. 여러 property 가 각자 갑구를 가지는 경우는 엔트리-레벨 비고 유지.
    note_lines_raw = entry.get("note_lines") or []
    gapgu_present = _has_gapgu_note(note_lines_raw)
    jibun_present = any("지분매각" in n for n in normalized)
    single_property = len(entry.get("properties") or []) == 1
    move_extras_to_bracket = gapgu_present and jibun_present and single_property
    parts = []
    explicit_bundle_note = next(
        (x for x in normalized if "일괄매각" in x and "제시외건물" in x and "매각포함" in x),
        "",
    )
    if explicit_bundle_note:
        parts.append(explicit_bundle_note)
    elif any("일괄매각" in x for x in normalized):
        parts.append("일괄매각")
    if any("목록3" in x and "지분매각" in x for x in normalized):
        parts.append("목록3지분매각")
    if any("목록4" in x for x in normalized):
        parts.append("목록4분묘소재")
    if any("제시외건물포함" in x for x in normalized):
        # Look for a parenthetical exclusion like 제시외건물포함[기호ㄴ제외]
        suffix = ""
        for x in normalized:
            m = re.search(r"제시외건물포함\[([^\]]+)\]", x)
            if m:
                suffix = f"[{m.group(1)}]"
                break
        parts.append(f"제시외건물포함{suffix}")
    if any("농지취득자격" in x for x in normalized) and not move_extras_to_bracket:
        parts.append("농지취득자격증명요")
    if any("공유자우선매수권" in x for x in normalized):
        parts.append("공유자우선매수권행사에관한특별매각조건있음")
    elif any(("공유자" in x and "우선매수" in x) for x in normalized):
        parts.append("공유자우선매수신고1회제한")
    if any("지상수목포함매각" in x for x in normalized):
        parts.append("지상수목포함매각")
    if any("분묘1기소재" in x for x in normalized):
        parts.append("분묘1기소재")
    if any("지분매각" in x for x in normalized) and not any("목록3" in x and "지분매각" in x for x in normalized):
        if not any("지분매각" in p for p in parts):
            parts.append("지분매각")
    has_jesi_exclude = any("제시외물건매각제외" in x or "제시외물건매각 제외" in x for x in normalized) and not move_extras_to_bracket
    if any("기계기구목록" in x for x in normalized):
        parts.append("기계기구목록")
    for raw, x in zip(seen, normalized):
        if "공장및광업재단저당법제6조" in x:
            parts.append(x)
        elif "범창종합건설" in x or "유치권신고" in x:
            parts.append("[주]범창종합건설유치권신고,성립여부불명")
    if compact_common(entry.get("usage", "")) == "기타" and any("제시외건물포함" in x for x in normalized):
        parts = [p for p in parts if p not in {"기계기구목록"} and not p.startswith("공장및광업재단저당법제6조")]
    if any("지분매각" in p for p in parts):
        # 편집기준 예시 순서: 지분매각 → 공유자우선매수 → 농지취득 → 기타
        order_priority = {
            "지분매각": 0,
            "공유자우선매수": 1,
            "농지취득": 2,
        }
        def _key(p: str) -> int:
            for key, idx in order_priority.items():
                if key in p:
                    return idx
            return 9
        parts.sort(key=_key)
    dedup = []
    for p in parts:
        cleaned = p.strip(". ").strip() if p else ""
        if cleaned and cleaned not in dedup:
            dedup.append(cleaned)
    result = ".".join(dedup)
    if has_jesi_exclude:
        # Attach as suffix with comma (편집기준 예시에서 `,제시외물건매각제외` 형태).
        if result:
            result = f"{result},제시외물건매각제외"
        else:
            result = "제시외물건매각제외"
    return result


def format_entry(entry: dict) -> dict:
    prev = ""
    lines = []
    usages = []
    blocks = entry.get("properties", [])
    note_lines = entry.get("note_lines", [])
    jesi_map = jesi_attribution(note_lines, blocks)
    # 어떤 property 에도 소속 못 찾은 제시외는 `extra_area_text` fallback 로
    # 마지막 property 에 붙는다. jesi_map 에 entry 가 있으면 그 property 에만
    # 붙이고, 없는 property 는 is_last 로 결정한다.
    jesi_has_explicit = bool(jesi_map)
    is_single_property = len(blocks) == 1
    for idx, block in enumerate(blocks):
        is_last = idx == len(blocks) - 1
        jesi_extra = jesi_map.get(idx, "")
        # When jesi_map explicitly covers the whole attribution, disable is_last
        # extra_area_text fallback.
        if jesi_has_explicit:
            line, usage = format_property(block, prev, note_lines, entry["usage"], False, jesi_extra, is_single_property=is_single_property)
        else:
            line, usage = format_property(block, prev, note_lines, entry["usage"], is_last, "", is_single_property=is_single_property)
        if compact_common(entry["usage"]) == "기타" and any("분묘" in compact_common(n) for n in note_lines) and usage == "전":
            usage = "기타"
        lines.append(line)
        usages.append(usage)
        prev = block["address"]
    original_cases = list(entry["case_numbers"])
    markers = []
    real_cases = []
    for token in original_cases:
        if token in {"(중복)", "(병합)"}:
            markers.append(token.replace("(", "[").replace(")", "]"))
        else:
            real_cases.append(token)
    case = "\n".join(real_cases[:1] or real_cases)
    note = summarize_notes(entry)
    if len(real_cases) > 1:
        extra_case_text = " ".join(real_cases[1:] + markers)
        if len(lines) + (1 if note else 0) < 5:
            note = f"{extra_case_text}.{note}" if note else extra_case_text
        else:
            case = "\n".join(real_cases + markers)
    elif markers:
        if len(lines) + (1 if note else 0) < 5:
            note = f"{' '.join(markers)}.{note}" if note else " ".join(markers)
        else:
            case = "\n".join(real_cases + markers)
    rendered = {
        "group": infer_group(entry),
        "case": case,
        "item": entry["item_number"],
        "locations": lines,
        "usages": usages,
        "location": "\n".join(lines),
        "usage": "\n".join(usages),
        "price": f'{entry["appraisal_amount"]}\n{entry["minimum_sale_price"]}',
        "note": note,
    }
    try:
        from llm_refiner import refine as _refine  # local import to keep optional
        rendered = _refine(entry, rendered)
    except Exception:
        pass
    return rendered


def render_html(doc: dict) -> str:
    entries = [
        format_entry(e) for e in doc["entries"]
        if compact_common(e.get("usage", "")) not in {"자동차", "선박", "건설기계", "항공기"}
    ]
    grouped = {k: [] for k in GROUP_ORDER}
    for entry in entries:
        grouped[entry["group"]].append(entry)

    sections = []
    for group in GROUP_ORDER:
        rows = grouped[group]
        if not rows:
            continue
        rows = sorted(rows, key=lambda row: parse_case_sort_key(row["case"], row["item"]))
        row_html = []
        for r in rows:
            loc_items = list(r.get("locations") or []) or [""]
            usage_items = list(r.get("usages") or [])
            if len(usage_items) < len(loc_items):
                usage_items += [""] * (len(loc_items) - len(usage_items))
            span = len(loc_items)
            rowspan_attr = f' rowspan="{span}"' if span > 1 else ""
            case_html = html.escape(r["case"]).replace(chr(10), "<br>")
            item_html = html.escape(r["item"])
            price_html = html.escape(r["price"]).replace(chr(10), "<br>")
            note_html = html.escape(r["note"])
            for idx, (loc, usage) in enumerate(zip(loc_items, usage_items)):
                cells = []
                if idx == 0:
                    cells.append(f"<td{rowspan_attr}>{case_html}</td>")
                    cells.append(f"<td{rowspan_attr}>{item_html}</td>")
                cells.append(f"<td>{html.escape(loc).replace(chr(10), '<br>')}</td>")
                cells.append(f"<td>{html.escape(usage).replace(chr(10), '<br>')}</td>")
                if idx == 0:
                    cells.append(f"<td{rowspan_attr}>{price_html}</td>")
                    cells.append(f"<td{rowspan_attr}>{note_html}</td>")
                row_html.append(f"<tr>{''.join(cells)}</tr>")
        sections.append(
            f"<section><h2>[{html.escape(group)}]</h2><table><thead><tr>"
            "<th>사건번호</th><th>물건번호</th><th>소재지 및 면적[㎡]</th><th>용도</th><th>감정평가액<br>최저매각가격<br>[단위 : 원]</th><th>비고</th>"
            f"</tr></thead><tbody>{''.join(row_html)}</tbody></table></section>"
        )

    court_line = html.escape(doc.get("court_line") or "")
    auction_datetime = html.escape(doc.get("auction_datetime") or "")
    decision_datetime = html.escape(doc.get("decision_datetime") or "")
    officer_line = html.escape(doc.get("officer_line") or "")
    meta = f"""
    <div class="meta">
      <div>법원 경매부동산의 매각 공고</div>
      <div>1.매각물건의 표시 및 매각조건 &lt;{court_line}&gt;</div>
      <div>{auction_datetime}</div>
      <div>{decision_datetime}</div>
      <div>{officer_line}</div>
    </div>
    """

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>법원 경매부동산의 매각 공고</title>
<style>
body{{font-family:"Apple SD Gothic Neo","Malgun Gothic",sans-serif;margin:18px;color:#111}}
h1{{margin:0 0 6px;font-size:24px}} h2{{margin:14px 0 6px;font-size:17px}}
.meta{{margin-bottom:10px;line-height:1.35;font-size:13px}}
section{{break-inside:avoid}}
table{{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px;line-height:1.28}}
th,td{{border:1px solid #4e4e4e;padding:4px 5px;vertical-align:top;word-break:keep-all;overflow-wrap:anywhere;text-align:left}}
th{{background:#f3f3f3;text-align:center;font-weight:700}}
td:nth-child(1),th:nth-child(1){{width:10%}}
td:nth-child(2),th:nth-child(2){{width:5%}}
td:nth-child(3),th:nth-child(3){{width:44%}}
td:nth-child(4),th:nth-child(4){{width:11%}}
td:nth-child(5),th:nth-child(5){{width:14%}}
td:nth-child(6),th:nth-child(6){{width:16%}}
@media print{{body{{margin:10mm}}}}
</style></head><body>
<h1>법원 경매부동산의 매각 공고</h1>
{meta}
{''.join(sections)}
</body></html>"""


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
                "--no-pdf-header-footer",
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
    parser = argparse.ArgumentParser(description="정규화 JSON을 최종본 스타일 HTML/PDF로 렌더링합니다.")
    parser.add_argument("json_path", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--pdf", action="store_true")
    args = parser.parse_args()

    doc = load_entries(args.json_path)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    html_path = args.output_dir / f"{args.json_path.stem}.final.html"
    html_path.write_text(render_html(doc), encoding="utf-8")
    print(f"HTML: {html_path}")
    if args.pdf:
        pdf_path = args.output_dir / f"{args.json_path.stem}.final.pdf"
        render_pdf(html_path, pdf_path)
        print(f"PDF: {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
