"""
법원 경매공고 원고 파일에서 구조화된 레코드를 추출.
지원 포맷:
  - .pdf  (pdfplumber 테이블 추출)
  - .hwpx (실제 XML+ZIP 포맷)
  - .hwp  (HWP 5.x 바이너리, pyhwp로 텍스트 추출)
  - 확장자 .hwpx 이지만 내용이 HWP 5.x 바이너리인 경우(OLE 복합문서)도 .hwp로 처리.
"""

import re
import os
import zipfile
import pdfplumber
from pathlib import Path
from typing import Dict, List, Tuple

# ---- 공통 유틸 ----

CASE_NO_RE = re.compile(r"(\d{4}타경\d+)")
PRICE_RE = re.compile(r"[\d,]{4,}")


def _clean(s):
    if s is None:
        return ""
    s = str(s).replace("　", " ").strip()
    s = re.sub(r"\s+\n", "\n", s)
    return s


# ---- PDF 추출 ----

def extract_from_pdf(path: str) -> Tuple[Dict, List[Dict]]:
    """
    PDF에서 헤더(담당계·매각일시·매각결정일시)와 레코드 리스트를 반환.
    pdfplumber로 페이지별 테이블을 뽑아 합치고, 사건번호·물건번호가 같은 블록을 묶어 하나의 레코드로 만든다.
    """
    header = {}
    all_rows: List[List[str]] = []
    joined_text = ""

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            joined_text += "\n" + txt
            for table in page.extract_tables() or []:
                for row in table:
                    all_rows.append([_clean(c) for c in row])

    # --- 헤더 파싱 ---
    m = re.search(r"담\s*당\s*계\s*[:：]?\s*(경매\s*\d+\s*계(?:\s*\S+)?)", joined_text)
    if m:
        header["damdang"] = re.sub(r"\s+", " ", m.group(1).strip())
    m = re.search(r"매각일시\s*[:：]?\s*([^\n]+)", joined_text)
    if m:
        header["sale_date"] = m.group(1).strip()
    m = re.search(r"매각결정일시\s*[:：]?\s*([^\n]+)", joined_text)
    if m:
        header["decision_date"] = m.group(1).strip()

    # --- 레코드 조립 ---
    records = _rows_to_records(all_rows)
    return header, records


def _is_header_row(r: List[str]) -> bool:
    """헤더/서브헤더 행 감지."""
    joined = " ".join((c or "") for c in r)
    if "사건번호" in joined and ("매 각 물 건" in joined or "매각물건" in joined):
        return True
    if "물건\n번호" in joined or ("용 도" in joined and "소 재 지" in joined):
        return True
    if "감정평가액" in joined and "최저매각가격" in joined and "비" in joined:
        return True
    return False


def _clean_cell(c) -> str:
    if c is None:
        return ""
    return str(c).replace(" ", " ").strip()


def _flat(c) -> str:
    """셀의 줄바꿈·공백을 모두 제거해 한 덩어리로."""
    return re.sub(r"\s+", "", _clean_cell(c))


def _rows_to_records(rows: List[List[str]]) -> List[Dict]:
    """
    pdfplumber 추출 결과를 법원공고 레코드 구조로 정리.
    각 행은 [사건번호, 물건번호, 용도, 소재지, 상세내역, 감정평가액, 비고] 로 가정.
    사건번호 셀에 줄바꿈이 껴 있을 수 있으므로 공백을 제거한 형태로 매칭한다.
    """
    records: List[Dict] = []
    cur: Dict = None

    for r in rows:
        if _is_header_row(r):
            continue
        padded = list(r) + [""] * max(0, 7 - len(r))
        case_no_c, item_no_c, yongdo_c, addr_c, detail_c, price_c, note_c = [
            _clean_cell(x) for x in padded[:7]
        ]

        # 공백·줄바꿈이 있어도 개별 사건번호로 독립 인식 (탐욕 매칭 버그 방지)
        all_nos_raw = re.findall(r"\d{4}\s*타경\s*\d+", case_no_c)
        all_nos = [re.sub(r"\s+", "", x) for x in all_nos_raw]
        if all_nos:
            if cur:
                records.append(cur)
            # (중복)/(병합) 태그 감지
            dup_tag = ""
            m_dup = re.search(r"(중복|병합)", case_no_c)
            primary = all_nos[0]
            if m_dup and len(all_nos) >= 2:
                dup_tag = f"{all_nos[1]}[{m_dup.group(1)}]"

            prices = [x.strip() for x in re.split(r"\n|\r", price_c) if x.strip()]
            cur = {
                "case_no": primary,
                "dup_tag": dup_tag,
                "item_no": item_no_c or "1",
                "yongdo": yongdo_c,
                "locations": [{"addr": addr_c, "detail": detail_c}],
                "price": prices[0] if prices else "",
                "min_price": prices[1] if len(prices) >= 2 else (prices[0] if prices else ""),
                "note": note_c,
            }
        else:
            if cur is None:
                continue
            # 같은 사건번호 + 다른 물건번호 = 새 레코드 (pdfplumber가 case_no 셀을 합쳐놓을 때 대비)
            if item_no_c and item_no_c.isdigit() and item_no_c != cur["item_no"]:
                records.append(cur)
                prices = [x.strip() for x in re.split(r"\n|\r", price_c) if x.strip()]
                cur = {
                    "case_no": cur["case_no"],
                    "dup_tag": cur.get("dup_tag", ""),
                    "item_no": item_no_c,
                    "yongdo": yongdo_c or cur["yongdo"],
                    "locations": [{"addr": addr_c, "detail": detail_c}] if (addr_c or detail_c) else [],
                    "price": prices[0] if prices else "",
                    "min_price": prices[1] if len(prices) >= 2 else (prices[0] if prices else ""),
                    "note": note_c,
                }
                continue
            if addr_c or detail_c:
                cur["locations"].append({"addr": addr_c, "detail": detail_c})
            if note_c:
                cur["note"] = (cur["note"] + "\n" + note_c).strip()
            if price_c and not cur["price"]:
                prices = [x.strip() for x in re.split(r"\n|\r", price_c) if x.strip()]
                cur["price"] = prices[0] if prices else ""
                cur["min_price"] = prices[1] if len(prices) >= 2 else (prices[0] if prices else "")

    if cur:
        records.append(cur)
    return records


# ---- HWP / HWPX 추출 ----

def extract_from_hwp(path: str) -> Tuple[Dict, List[Dict]]:
    """HWP 5.x 바이너리 → pyhwp HTMLTransform으로 XHTML 변환 → XHTML 테이블 파싱."""
    try:
        from hwp5.hwp5html import HTMLTransform
        from hwp5.xmlmodel import Hwp5File
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"pyhwp(hwp5) 모듈 로드 실패: {e}")

    import tempfile
    from xml.etree import ElementTree as ET

    rows: List[List[str]] = []
    header_text = ""
    with tempfile.TemporaryDirectory() as d:
        HTMLTransform().transform_hwp5_to_dir(Hwp5File(path), d)
        idx = os.path.join(d, "index.xhtml")
        tree = ET.parse(idx)
        ns = {"x": "http://www.w3.org/1999/xhtml"}
        tables = tree.getroot().findall(".//x:table", ns)

        # 첫번째 표는 헤더(담당계·매각일시·매각결정일시)인 경우가 많음
        for tb in tables[:1]:
            for tr in tb.findall(".//x:tr", ns):
                for td in tr.findall("x:td", ns):
                    header_text += "\n" + _cell_text(td)

        # 두번째(또는 그 이후) 표가 본문 레코드 표
        for tb in tables[1:]:
            for tr in tb.findall(".//x:tr", ns):
                cells = [_cell_text(td) for td in tr.findall("x:td", ns)]
                rows.append(cells)

    header = _parse_header_text(header_text)
    # HWP xhtml의 행 구조는 PDF와 약간 다르므로 보정
    records = _rows_to_records(_hwp_normalize_rows(rows))
    return header, records


def _cell_text(td) -> str:
    """td 내부의 텍스트를 공백 보존·줄바꿈으로 정리."""
    parts = []
    for s in td.itertext():
        parts.append(s)
    return "".join(parts).strip()


def _hwp_normalize_rows(rows: List[List[str]]) -> List[List[str]]:
    """
    HWP XHTML 파서 결과를 pdfplumber와 유사한 7열 구조로 정규화.
    - 7셀 행: 그대로 (사건번호, 물건번호, 용도, 소재지, 상세내역, 감정평가액셀, 비고)
    - 2셀 행: 연속 소재지로 간주해 [None, None, None, addr, detail, None, None] 로 확장
    - 1셀 / 기타: 스킵 (단 (단위: 원) 포함)
    - 감정평가액 셀이 '3,850,0003,850,000' 처럼 붙어 있으면 절반으로 쪼개 줄바꿈 추가
    """
    out: List[List[str]] = []
    for r in rows:
        if len(r) == 7:
            r = list(r)
            r[5] = _split_price_cell(r[5])
            out.append(r)
        elif len(r) == 6:
            # 헤더성 6셀 행은 스킵
            continue
        elif len(r) == 2:
            addr, detail = r
            out.append(["", "", "", addr, detail, "", ""])
        else:
            # 기타 행은 스킵
            continue
    return out


def _split_price_cell(cell: str) -> str:
    """'3,850,0003,850,000' 형태를 '3,850,000\n3,850,000'로 분리."""
    if not cell:
        return cell
    if "\n" in cell:
        return cell
    s = cell.replace(" ", "")
    # 길이 짝수에다 두 조각이 동일한 패턴
    for i in range(len(s) // 2, len(s)):
        a, b = s[:i], s[i:]
        if a == b and re.fullmatch(r"[\d,]+", a):
            return a + "\n" + b
    return cell


def _parse_header_text(text: str) -> Dict:
    header: Dict = {}
    m = re.search(r"담\s*당\s*계\s*[:：]?\s*(경매\s*\d+\s*계(?:\s*\S+)?)", text)
    if m:
        header["damdang"] = re.sub(r"\s+", " ", m.group(1).strip())
    m = re.search(r"매각일시\s*[:：]?\s*([^\n]+)", text)
    if m:
        header["sale_date"] = m.group(1).strip()
    m = re.search(r"매각결정일시\s*[:：]?\s*([^\n]+)", text)
    if m:
        header["decision_date"] = m.group(1).strip()
    return header


def extract_from_hwpx(path: str) -> Tuple[Dict, List[Dict]]:
    """실제 OWPML HWPX(ZIP+XML) 인지 확인 후 XML 파싱. 아니면 HWP로 처리."""
    # .hwpx 확장자지만 OLE 복합문서인 경우가 많음
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"PK"):
        return _parse_owpml_hwpx(path)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return extract_from_hwp(path)
    # 그 외는 텍스트로 시도
    return _parse_plain_text(Path(path).read_text(errors="ignore"))


def _parse_owpml_hwpx(path: str) -> Tuple[Dict, List[Dict]]:
    """진짜 HWPX(ZIP+XML)에서 body text 추출."""
    from xml.etree import ElementTree as ET

    text_chunks: List[str] = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if "section" in name and name.endswith(".xml"):
                xml = z.read(name).decode("utf-8", errors="ignore")
                try:
                    root = ET.fromstring(xml)
                    for el in root.iter():
                        if el.tag.endswith("}t") or el.tag == "t":
                            if el.text:
                                text_chunks.append(el.text)
                except Exception:  # noqa: BLE001
                    text_chunks.append(xml)
    return _parse_plain_text("\n".join(text_chunks))


def _parse_plain_text(text: str) -> Tuple[Dict, List[Dict]]:
    """텍스트(비정형) 기반 휴리스틱 파싱. 사건번호/가격/비고를 정규식으로 뽑아낸다."""
    header: Dict = {}
    m = re.search(r"담\s*당\s*계\s*[:：]?\s*(경매\s*\d+\s*계(?:\s*\S+)?)", text)
    if m:
        header["damdang"] = re.sub(r"\s+", " ", m.group(1).strip())
    m = re.search(r"매각일시\s*[:：]?\s*([^\n]+)", text)
    if m:
        header["sale_date"] = m.group(1).strip()
    m = re.search(r"매각결정일시\s*[:：]?\s*([^\n]+)", text)
    if m:
        header["decision_date"] = m.group(1).strip()

    # 사건번호 단위로 블록 분할
    indexes = [m.start() for m in CASE_NO_RE.finditer(text)]
    if not indexes:
        return header, []
    blocks = []
    for i, start in enumerate(indexes):
        end = indexes[i + 1] if i + 1 < len(indexes) else len(text)
        blocks.append(text[start:end])

    records = []
    for block in blocks:
        m_case = CASE_NO_RE.search(block)
        case_no = m_case.group(1)
        # 금액: 콤마 포함 4자리 이상 숫자 중 큰 값 두 개
        prices = [x for x in PRICE_RE.findall(block) if "," in x]
        price = prices[0] if prices else ""
        min_price = prices[1] if len(prices) >= 2 else price
        # 용도: '아파트', '단독주택,다가구주택', '기타' 등
        yongdo = ""
        m_yongdo = re.search(
            r"(아파트|오피스텔|연립주택|다세대주택|단독주택[,\s]*다가구주택|다가구주택|단독주택|근린시설|임야|전답|대지[,\s]*임야[,\s]*전답|공장|기타)",
            block,
        )
        if m_yongdo:
            yongdo = m_yongdo.group(1)
        # 소재지: 첫번째 광역 접두 이후 덩어리 추출 — 매우 러프
        locations = []
        addr_pattern = re.compile(
            r"((?:서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|세종특별자치시|경기도|강원도|강원특별자치도|충청북도|충청남도|전라북도|전북특별자치도|전라남도|경상북도|경상남도|제주특별자치도)[^\n]+)"
        )
        for am in addr_pattern.finditer(block):
            locations.append({"addr": am.group(1).strip(), "detail": ""})
        if not locations:
            locations = [{"addr": "", "detail": block.strip()}]
        records.append({
            "case_no": case_no,
            "item_no": "1",
            "yongdo": yongdo,
            "locations": locations,
            "price": price,
            "min_price": min_price,
            "note": "",
        })
    return header, records


# ---- dispatcher ----

def extract_file(path: str) -> Tuple[Dict, List[Dict]]:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return extract_from_pdf(path)
    if ext == ".hwpx":
        return extract_from_hwpx(path)
    if ext == ".hwp":
        return extract_from_hwp(path)
    raise ValueError(f"지원하지 않는 확장자: {ext}")
