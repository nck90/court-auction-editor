#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET
import zipfile

import olefile

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"

CASE_PATTERN = re.compile(r"^\d{4}타경\d+$")
AMOUNT_PATTERN = re.compile(r"^\d[\d,]*$")
ADDRESS_PATTERN = re.compile(
    r"^(소재지)?("
    r"서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|세종특별자치시|"
    r"경기도|강원특별자치도|충청북도|충청남도|전라북도|전북특별자치도|전라남도|경상북도|경상남도|"
    r"제주특별자치도|제주도)"
)

NOTE_KEYWORDS = (
    "일괄매각",
    "지분매각",
    "목록",
    "매각에서 제외",
    "제시외",
    "분묘",
    "공유자 우선매수권",
    "특별매각조건",
    "우선매수권",
    "농지취득자격",
    "토지별도등기",
    "보증금 몰수",
    "유치권",
    "수목",
    "지상",
    "포함",
    "제외",
    "성립여부",
    "기계기구목록",
    "공장 및 광업재단 저당법",
    "공부상",
    "주거나지",
)

DETAIL_KEYWORDS = (
    "㎡",
    "구조",
    "지층",
    "지하",
    "1층",
    "2층",
    "3층",
    "4층",
    "5층",
    "단층",
    "대 ",
    "전 ",
    "답 ",
    "임야",
    "묘지",
    "공장용지",
    "도로",
    "사무실",
    "노래연습장",
    "휴게음식점",
    "일반음식점",
    "소매점",
    "게임제공업소",
    "근린생활시설",
    "제1종근린생활시설",
    "제2종근린생활시설",
    "단독주택",
    "다가구주택",
    "오피스텔",
    "아파트",
    "제조업소",
    "부속건물",
    "현관",
    "테라스",
    "창고",
    "다용도실",
    "공장 ",
)

LOCATION_NOISE_PATTERNS = (
    "철근콘크리트구조",
    "철근콘크리트조",
    "철근콘크리트",
    "일반철골구조",
    "경량철골구조",
    "철골구조",
    "철골조",
    "시멘트벽돌조",
    "시멘트블록조",
    "시멘트블록",
    "시멘트",
    "블록조",
    "블록",
    "슬래브지붕",
    "슬래브및판넬지붕",
    "슬래브",
    "샌드위치판넬지붕",
    "샌드위치판넬",
    "샌드위치 판넬지붕",
    "샌드위치 판넬",
    "판넬지붕",
    "판넬",
)

IGNORE_EXACT = {
    "(단위: 원)",
    "사건번호",
    "매 각 물 건",
    "물건",
    "번호",
    "용 도",
    "소 재 지",
    "상 세 내 역",
    "(구조 및 면적)",
    "감정평가액",
    "최저매각가격",
    "비 고",
}


@dataclass
class PropertyBlock:
    address: str
    details: list[str] = field(default_factory=list)

    def combined_text(self) -> str:
        return " ".join([self.address, *self.details]).strip()


@dataclass
class AuctionEntry:
    case_numbers: list[str]
    item_number: str
    usage: str
    appraisal_amount: str
    minimum_sale_price: str
    note_lines: list[str] = field(default_factory=list)
    properties: list[PropertyBlock] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple[int, str, int]:
        first_case = next((case for case in self.case_numbers if CASE_PATTERN.match(case)), "")
        year, serial = 0, 0
        if first_case:
            m = re.match(r"^(\d{4})타경(\d+)$", first_case)
            if m:
                year = int(m.group(1))
                serial = int(m.group(2))
        try:
            item_no = int(self.item_number)
        except ValueError:
            item_no = sys.maxsize
        return year, first_case, serial * 1000 + item_no

    def merged_location_area_lines(self) -> list[str]:
        lines: list[str] = []
        previous_address = ""
        for idx, block in enumerate(self.properties):
            address = shorten_address(block.address, previous_address)
            details = " ".join(clean_location_detail(detail, self.usage) for detail in block.details).strip()
            if details:
                line = compact_address_text(f"{address} {details}")
            else:
                line = compact_address_text(address)
            if idx == len(self.properties) - 1:
                extra_text = extra_area_text(self.note_lines)
                if extra_text and extra_text not in line:
                    line = compact_address_text(f"{line} {extra_text}")
            lines.append(line)
            previous_address = block.address
        return lines

    def case_number_text(self) -> str:
        return " ".join(self.case_numbers)

    def usage_lines(self) -> list[str]:
        inferred = [infer_property_usage(block, self.usage) for block in self.properties]
        return inferred or [normalize_usage_label(self.usage)]

    def note_summary_lines(self) -> list[str]:
        return summarize_note_lines(self.note_lines)


@dataclass
class AuctionDocument:
    court_line: str = ""
    auction_datetime: str = ""
    decision_datetime: str = ""
    officer_line: str = ""
    entries: list[AuctionEntry] = field(default_factory=list)


def normalize_text(value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def strip_region_prefix(value: str) -> str:
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
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def compact_address_text(value: str) -> str:
    value = strip_region_prefix(normalize_text(value))
    value = re.sub(r",\s+", ",", value)
    value = re.sub(r"\s+\(", "(", value)
    value = re.sub(r"\(\s+", "(", value)
    value = re.sub(r"\s+\)", ")", value)
    return normalize_text(value)


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def looks_human_readable(token: str) -> bool:
    if not token:
        return False
    if token in IGNORE_EXACT:
        return True
    if "경매" in token or "타경" in token:
        return True
    if re.search(r"[가-힣A-Za-z0-9]", token) is None:
        return False
    return True


def stream_names(ole: olefile.OleFileIO) -> Iterable[str]:
    for parts in ole.listdir():
        yield "/".join(parts)


def read_hwp_binary_text_tokens(path: Path) -> list[str]:
    ole = olefile.OleFileIO(str(path))
    compressed = False
    if ole.exists("FileHeader"):
        header = ole.openstream("FileHeader").read()
        flags = int.from_bytes(header[36:40], "little")
        compressed = bool(flags & 0x01)

    tokens: list[str] = []
    for stream_name in sorted(name for name in stream_names(ole) if name.startswith("BodyText/Section")):
        raw = ole.openstream(stream_name).read()
        data = zlib.decompress(raw, -15) if compressed else raw
        pos = 0
        while pos + 4 <= len(data):
            header = int.from_bytes(data[pos : pos + 4], "little")
            tag = header & 0x3FF
            size = (header >> 20) & 0xFFF
            pos += 4
            if size == 0xFFF:
                size = int.from_bytes(data[pos : pos + 4], "little")
                pos += 4
            payload = data[pos : pos + size]
            pos += size
            if tag != 67:
                continue
            text = normalize_text(payload.decode("utf-16le", errors="ignore"))
            if looks_human_readable(text):
                tokens.append(text)
    return tokens


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def read_hwpx_xml_tokens(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    tokens: list[str] = []
    paragraph_tags = {"p", "paragraph", "para"}

    for element in root.iter():
        if local_name(element.tag).lower() not in paragraph_tags:
            continue
        text = normalize_text("".join(element.itertext()))
        if looks_human_readable(text):
            tokens.append(text)

    if tokens:
        return tokens

    for element in root.iter():
        if local_name(element.tag).lower() not in {"t", "text"}:
            continue
        text = normalize_text("".join(element.itertext()))
        if looks_human_readable(text):
            tokens.append(text)
    return tokens


def read_hwpx_text_tokens(path: Path) -> list[str]:
    xml_names: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                lowered = name.lower()
                if not lowered.endswith(".xml"):
                    continue
                if lowered.startswith("contents/") or "section" in lowered or lowered.endswith("header.xml"):
                    xml_names.append(name)
            if not xml_names:
                xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")]

            tokens: list[str] = []
            for name in sorted(xml_names):
                try:
                    tokens.extend(read_hwpx_xml_tokens(archive.read(name)))
                except ET.ParseError:
                    continue
    except zipfile.BadZipFile as exc:
        raise ValueError("`.hwpx` 파일 구조를 읽지 못했습니다. 실제 HWPX 파일인지 확인해 주세요.") from exc

    if not tokens:
        raise ValueError("`.hwpx` 파일에서 본문 텍스트를 찾지 못했습니다. 다른 형식으로 저장된 문서일 수 있습니다.")
    return tokens


def detect_input_format(path: Path) -> str:
    with path.open("rb") as handle:
        magic = handle.read(8)

    if magic.startswith(OLE_MAGIC):
        return "hwp"
    if magic.startswith(ZIP_MAGIC):
        return "hwpx"

    suffix = path.suffix.lower()
    if suffix == ".hwp":
        return "hwp"
    if suffix == ".hwpx":
        return "hwpx"
    raise ValueError(f"지원하지 않는 파일 형식입니다: {path.suffix}")


def read_text_tokens(path: Path) -> list[str]:
    detected = detect_input_format(path)
    if detected == "hwp":
        return read_hwp_binary_text_tokens(path)
    if detected == "hwpx":
        return read_hwpx_text_tokens(path)
    raise ValueError(f"지원하지 않는 파일 형식입니다: {path.suffix}")


def is_address(token: str) -> bool:
    return bool(ADDRESS_PATTERN.match(token))


def is_note(token: str) -> bool:
    return any(keyword in token for keyword in NOTE_KEYWORDS)


def is_detail(token: str) -> bool:
    return any(keyword in token for keyword in DETAIL_KEYWORDS)


def normalize_usage_label(value: str) -> str:
    return value.replace(" ", "")


def shorten_address(address: str, previous_address: str) -> str:
    address = compact_address_text(address)
    previous_address = compact_address_text(previous_address)
    if not previous_address:
        return address
    prev_parts = previous_address.split()
    parts = address.split()
    i = 0
    while i < min(len(prev_parts), len(parts)) and prev_parts[i] == parts[i]:
        i += 1
    if i >= 2 and i < len(parts):
        return f"동소 {' '.join(parts[i:])}"
    return address


def clean_location_detail(detail: str, usage: str) -> str:
    cleaned = normalize_text(detail)
    for token in LOCATION_NOISE_PATTERNS:
        cleaned = cleaned.replace(token, "")

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
    if "근린시설" in usage or "근린생활시설" in usage:
        cleaned = cleaned.replace("근린생활시설", "")
        cleaned = cleaned.replace("근린시설", "")

    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r"\[\s+", "[", cleaned)
    cleaned = re.sub(r"\s+\]", "]", cleaned)
    return compact_address_text(cleaned)


def infer_property_usage(block: PropertyBlock, fallback: str) -> str:
    text = normalize_usage_label(block.combined_text())
    detail_text = normalize_usage_label(" ".join(block.details))
    fallback = normalize_usage_label(fallback)

    if re.search(r"^전\d", detail_text):
        return "전"
    if re.search(r"^답\d", detail_text):
        return "답"
    if re.search(r"^대\d", detail_text):
        return "대"
    if re.search(r"^묘지\d", detail_text):
        return "묘지"
    if "임야" in detail_text:
        return "임야"
    if "공장용지" in detail_text:
        return "공장용지"
    if "도로" in detail_text:
        return "도로"

    if fallback and fallback not in {"기타", "근린시설"}:
        return fallback

    if "단독주택" in text and ("근린생활시설" in text or "근린시설" in text):
        return "단독주택,근린시설"

    explicit_patterns = [
        ("아파트", ("아파트",)),
        ("연립주택,다세대,빌라", ("연립주택", "다세대", "빌라")),
        ("상가,오피스텔,근린시설", ("오피스텔", "상가")),
        ("근린시설", ("근린생활시설", "근린시설", "노래연습장", "게임제공업소", "휴게음식점", "일반음식점", "소매점")),
        ("단독주택,다가구주택", ("단독주택", "다가구주택")),
        ("공장용지", ("공장용지",)),
        ("공장", ("공장", "제조업소")),
        ("도로", ("도로",)),
        ("묘지", ("묘지",)),
        ("임야", ("임야",)),
    ]
    for label, patterns in explicit_patterns:
        if any(pattern in text for pattern in patterns):
            return label

    if fallback == "기타":
        return "기타"
    return fallback or "기타"


def extra_area_label(note_lines: list[str]) -> str:
    normalized = " ".join(normalize_text(line) for line in note_lines)
    if "비가림막" in normalized:
        return "비가림막등"
    if "창고" in normalized:
        return "창고등"
    if "관정" in normalized:
        return "관정등"
    return "건물등"


def extra_area_text(note_lines: list[str]) -> str:
    total = 0.0
    for line in note_lines:
        if "제시외" in line:
            total += sum(float(x) for x in re.findall(r"(\d+(?:\.\d+)?)㎡", line))
    if total:
        area_text = f"{total:.1f}".rstrip("0").rstrip(".")
        return f"제시외 {extra_area_label(note_lines)}{area_text}㎡"
    return ""


def summarize_note_lines(note_lines: list[str]) -> list[str]:
    lines = [normalize_text(line) for line in note_lines if normalize_text(line)]
    if not lines:
        return []

    summarized: list[str] = []
    seen: set[str] = set()
    def add_unique(value: str) -> None:
        key = re.sub(r"\s+", "", value)
        if key not in seen:
            summarized.append(value)
            seen.add(key)

    for line in lines:
        if line.startswith("제시외"):
            continue
        compact = re.sub(r"\s+", "", line)
        compact = compact.replace("목록3.", "목록3").replace("목록4.", "목록4").replace("목록 12. 15.", "목록12,15")
        compact = compact.replace("분묘 소재함", "분묘소재").replace("분묘 1기 소재", "분묘1기소재")
        compact = compact.replace("지상 수목포함 매각", "지상수목포함매각")
        compact = compact.replace("토지별도등기있음", "토지별도등기있음")
        compact = compact.replace("공유자 우선매수권 있음(공유자 우선매수권 행사를 제한하는 특별매각조건 있음)", "공유자우선매수권행사에관한특별매각조건있음")
        compact = compact.replace("농지취득자격 증명 제출 요(미제출시 보증금 몰수)", "농지취득자격증명요")
        compact = compact.replace("종류기계기구목록", "기계기구목록")
        compact = compact.replace("공장 및 광업재단 저당법 제6조", "공장및광업재단저당법제6조")
        add_unique(compact)

    return summarized


def infer_group_label(entry: AuctionEntry) -> str:
    usage_lines = {normalize_usage_label(line) for line in entry.usage_lines()}
    if usage_lines & {"공장용지", "공장", "도로", "기타"}:
        return "기타"
    if usage_lines <= {"아파트"}:
        return "아파트"
    if usage_lines & {"연립주택,다세대,빌라"}:
        return "연립주택,다세대,빌라"
    if usage_lines & {"단독주택,다가구주택", "단독주택,근린시설"}:
        return "단독주택,다가구주택"
    if usage_lines & {"상가,오피스텔,근린시설", "근린시설"}:
        return "상가,오피스텔,근린시설"
    if usage_lines <= {"전", "답", "대", "임야", "묘지"} or usage_lines & {"전", "답", "대", "임야", "묘지"}:
        return "대지,임야,전답"
    return "기타"


def is_usage_candidate(token: str) -> bool:
    if not token or AMOUNT_PATTERN.match(token) or CASE_PATTERN.match(token):
        return False
    if is_address(token) or token.startswith("매각") or ":" in token:
        return False
    return bool(re.search(r"[가-힣]", token))


def looks_like_item_boundary(tokens: list[str], index: int) -> bool:
    if not re.fullmatch(r"\d+", tokens[index]):
        return False
    if index + 2 >= len(tokens):
        return False
    usage = tokens[index + 1]
    probe = tokens[index + 2]
    return is_usage_candidate(usage) and (is_address(probe) or is_detail(probe))


def split_entries(tokens: list[str]) -> AuctionDocument:
    doc = AuctionDocument()
    for idx, token in enumerate(tokens):
        if "경매" in token and "계" in token and not doc.court_line:
            doc.court_line = token
        elif token.startswith("매각일시") and not doc.auction_datetime:
            doc.auction_datetime = token
        elif token.startswith("매각결정일시") and not doc.decision_datetime:
            doc.decision_datetime = token
        elif token.startswith("보좌관") and not doc.officer_line:
            doc.officer_line = token

    try:
        data_start = next(idx for idx, token in enumerate(tokens) if compact_text(token) == "비고") + 1
    except StopIteration:
        # 헤더 행이 없는 문서: 첫 사건번호(YYYY타경NNNN) 위치부터 데이터 시작.
        data_start = next(
            (idx for idx, token in enumerate(tokens) if CASE_PATTERN.match(token)),
            -1,
        )
        if data_start < 0:
            raise ValueError("표 헤더를 찾지 못했습니다. 문서 형식이 예상과 다릅니다.")

    i = data_start
    while i < len(tokens):
        if not CASE_PATTERN.match(tokens[i]):
            i += 1
            continue

        case_numbers = [tokens[i]]
        i += 1
        while i < len(tokens) and (CASE_PATTERN.match(tokens[i]) or tokens[i] == "(중복)"):
            case_numbers.append(tokens[i])
            i += 1

        while i < len(tokens) and not CASE_PATTERN.match(tokens[i]):
            item_number = tokens[i]
            if not re.fullmatch(r"\d+", item_number) or i + 1 >= len(tokens):
                i += 1
                continue
            usage = tokens[i + 1]
            i += 2

            body: list[str] = []
            while i < len(tokens) and not CASE_PATTERN.match(tokens[i]):
                if looks_like_item_boundary(tokens, i):
                    break
                body.append(tokens[i])
                i += 1

            entry = classify_entry(case_numbers, item_number, usage, body)
            doc.entries.append(entry)

    return doc


def classify_entry(case_numbers: list[str], item_number: str, usage: str, body: list[str]) -> AuctionEntry:
    prices: list[str] = []
    remainder: list[str] = []
    for token in body:
        if AMOUNT_PATTERN.match(token) and len(prices) < 2:
            prices.append(token)
        else:
            remainder.append(token)

    properties: list[PropertyBlock] = []
    notes: list[str] = []
    current: PropertyBlock | None = None

    for token in remainder:
        if is_address(token):
            current = PropertyBlock(address=token)
            properties.append(current)
            continue

        if is_note(token):
            notes.append(token)
            continue

        if current is None:
            notes.append(token)
            continue

        if is_detail(token) or not current.details:
            current.details.append(token)
        else:
            notes.append(token)

    appraisal_amount = prices[0] if prices else ""
    minimum_sale_price = prices[1] if len(prices) > 1 else appraisal_amount

    # Recovery: `usage` 칸에 실제 주소가 오고 용도 라벨이 note_lines로 밀린
    # 표 파싱 케이스 (예: 지방법원 지원 HWP 일부). 주소가 비어 있고 usage 에 ㎡
    # 가 있으면 usage 를 주소로 승격하고 note 에서 용도 후보를 꺼낸다.
    if not properties and (("㎡" in usage) or re.search(r"\d+평", usage)) and not CASE_PATTERN.match(usage):
        usage_candidates = (
            "아파트", "오피스텔", "다세대", "연립주택", "빌라",
            "단독주택", "다가구주택", "근린생활시설", "근린시설",
            "상가", "공장", "창고", "대", "전", "답", "임야", "잡종지",
            "묘지", "도로", "공장용지", "창고용지", "주차장", "체육용지",
            "주유소용지", "주택",
        )
        real_usage = ""
        leftover_notes: list[str] = []
        for tok in notes:
            t = tok.strip()
            if not real_usage and t in usage_candidates:
                real_usage = t
                continue
            leftover_notes.append(tok)
        properties = [PropertyBlock(address=usage)]
        notes = leftover_notes
        if real_usage:
            usage = real_usage

    return AuctionEntry(
        case_numbers=case_numbers,
        item_number=item_number,
        usage=usage,
        appraisal_amount=appraisal_amount,
        minimum_sale_price=minimum_sale_price,
        note_lines=notes,
        properties=properties,
    )


def usage_groups(entries: list[AuctionEntry]) -> list[tuple[str, list[AuctionEntry]]]:
    groups: dict[str, list[AuctionEntry]] = {}
    preferred_order = [
        "아파트",
        "연립주택,다세대,빌라",
        "단독주택,다가구주택",
        "대지,임야,전답",
        "상가,오피스텔,근린시설",
        "기타",
    ]
    for entry in entries:
        group_label = infer_group_label(entry)
        groups.setdefault(group_label, []).append(entry)
    present_order = [label for label in preferred_order if label in groups]
    return [(usage, sorted(groups[usage], key=lambda item: item.sort_key)) for usage in present_order]


def render_html(doc: AuctionDocument, source_path: Path) -> str:
    sections: list[str] = []
    for usage, entries in usage_groups(doc.entries):
        rows = []
        for entry in entries:
            location_lines = "<br>".join(html.escape(line) for line in entry.merged_location_area_lines()) or "&nbsp;"
            usage_lines = "<br>".join(html.escape(line) for line in entry.usage_lines()) or "&nbsp;"
            notes = "<br>".join(html.escape(line) for line in entry.note_summary_lines()) or "&nbsp;"
            rows.append(
                f"""
                <tr>
                  <td>{html.escape(entry.case_number_text())}</td>
                  <td>{html.escape(entry.item_number)}</td>
                  <td>{location_lines}</td>
                  <td>{usage_lines}</td>
                  <td>{html.escape(entry.appraisal_amount)}<br>{html.escape(entry.minimum_sale_price)}</td>
                  <td>{notes}</td>
                </tr>
                """
            )

        sections.append(
            f"""
            <section class="usage-section">
              <h2>[{html.escape(usage)}]</h2>
              <table>
                <thead>
                  <tr>
                    <th>사건번호</th>
                    <th>매각물건</th>
                    <th>소재지 및 면적 [㎡]</th>
                    <th>용도</th>
                    <th>감정평가액<br>최저매각가격<br>[단위:원]</th>
                    <th>비고</th>
                  </tr>
                </thead>
                <tbody>
                  {''.join(rows)}
                </tbody>
              </table>
            </section>
            """
        )

    meta_lines = [line for line in [doc.court_line, doc.auction_datetime, doc.officer_line, doc.decision_datetime] if line]
    meta_html = "<br>".join(html.escape(line) for line in meta_lines)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>법원경매공고 편집 원고</title>
  <style>
    :root {{
      --ink: #1f2328;
      --grid: #767676;
      --paper: #ffffff;
      --accent: #17324d;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: #eef1f4;
      font-family: "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
      line-height: 1.45;
    }}
    .page {{
      width: min(1180px, calc(100vw - 32px));
      margin: 24px auto;
      background: var(--paper);
      padding: 32px;
      box-shadow: 0 12px 40px rgba(0, 0, 0, 0.08);
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 28px;
      color: var(--accent);
    }}
    .meta {{
      margin-bottom: 24px;
      font-size: 15px;
    }}
    .source {{
      margin: 0 0 24px;
      color: #59636e;
      font-size: 13px;
    }}
    .usage-section {{
      margin-top: 28px;
    }}
    h2 {{
      margin: 0 0 10px;
      font-size: 20px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }}
    th, td {{
      border: 1px solid var(--grid);
      vertical-align: top;
      padding: 8px 10px;
      word-break: break-word;
    }}
    th {{
      text-align: center;
      background: #f5f7f9;
      font-weight: 700;
    }}
    td:nth-child(1) {{ width: 14%; }}
    td:nth-child(2), th:nth-child(2) {{ width: 8%; text-align: center; }}
    td:nth-child(3), th:nth-child(3) {{ width: 37%; }}
    td:nth-child(4), th:nth-child(4) {{ width: 11%; text-align: center; }}
    td:nth-child(5), th:nth-child(5) {{ width: 14%; text-align: right; }}
    td:nth-child(6), th:nth-child(6) {{ width: 16%; }}
    @media print {{
      body {{ background: white; }}
      .page {{
        width: auto;
        margin: 0;
        padding: 0;
        box-shadow: none;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <h1>법원경매공고 편집 원고</h1>
    <div class="meta">{meta_html}</div>
    <p class="source">원본 파일: {html.escape(str(source_path))}</p>
    {''.join(sections)}
  </main>
</body>
</html>
"""


def build_document(input_path: Path, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    tokens = read_text_tokens(input_path)
    document = split_entries(tokens)
    html_path = output_dir / f"{input_path.stem}.edited.html"
    json_path = output_dir / f"{input_path.stem}.normalized.json"
    html_path.write_text(render_html(document, input_path), encoding="utf-8")
    json_path.write_text(json.dumps(asdict(document), ensure_ascii=False, indent=2), encoding="utf-8")
    return html_path, json_path


def find_chrome_binary() -> str | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Arc.app/Contents/MacOS/Arc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium")


def html_to_pdf(html_path: Path, pdf_path: Path) -> None:
    chrome = find_chrome_binary()
    if not chrome:
        raise RuntimeError("PDF 렌더링용 Chrome 계열 브라우저를 찾지 못했습니다.")

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path.resolve()}",
        str(html_path.resolve().as_uri()),
    ]
    env = os.environ.copy()
    env.setdefault("HOME", str(Path.home()))
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Chrome PDF 변환 실패: {result.stderr.strip() or result.stdout.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="법원경매공고 HWP/HWPX를 편집 기준에 맞는 HTML 원고로 변환합니다.")
    parser.add_argument("input", type=Path, help="입력 HWP/HWPX 파일 경로")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"), help="결과물 저장 디렉터리")
    parser.add_argument("--pdf", action="store_true", help="생성된 HTML을 PDF로도 저장합니다.")
    args = parser.parse_args()

    try:
        html_path, json_path = build_document(args.input, args.output_dir)
        pdf_path = None
        if args.pdf:
            pdf_path = args.output_dir / f"{args.input.stem}.edited.pdf"
            html_to_pdf(html_path, pdf_path)
    except Exception as exc:  # pragma: no cover
        print(f"실패: {exc}", file=sys.stderr)
        return 1

    print(f"HTML: {html_path}")
    print(f"JSON: {json_path}")
    if args.pdf and pdf_path:
        print(f"PDF: {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
