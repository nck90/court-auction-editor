"""
법원경매공고 편집 기준 (2023.12.04) 기반 규칙 엔진.
기계적으로 적용 가능한 규칙을 Python 로직으로 구현.
판단이 필요한 영역(비고 축약, 최단 대표건물 선택 일부)은 LLM 보정으로 위임.
"""

import re
from typing import List, Dict

# --- 1. 도명 (광역시·도 접두어) ---
# '세종특별자치시'는 삭제 금지 (편집 기준 예외 조항)
CITY_PREFIXES_TO_STRIP = [
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
    "대전광역시", "울산광역시",
    "경기도", "강원도", "강원특별자치도",
    "충청북도", "충청남도",
    "전라북도", "전북특별자치도", "전라남도",
    "경상북도", "경상남도",
    "제주특별자치도", "제주도",
]


def strip_city_prefix(text: str) -> str:
    """'충청북도 제천시' → '제천시' 등으로 변환. 세종특별자치시는 유지."""
    for city in CITY_PREFIXES_TO_STRIP:
        text = re.sub(rf"{city}\s*", "", text)
    return text


# --- 2. 건축자재 키워드 (삭제 대상) ---
BUILDING_MATERIAL_PATTERNS = [
    r"\[철근\]\s*콘크리트",
    r"\(철근\)\s*콘크리트",
    r"철근\s*콘크리트",
    r"철근콘크리트구조",
    r"철근콘크리트조",
    r"철근\s*콘크리트조",
    r"철근\s*콘크리트구조",
    r"철근콘크리트 벽식구조",
    r"철근콘크리트평지붕",
    r"철근콘크리트평슬래브",
    r"\(철근\)콘크리트지붕",
    r"일반철골구조지붕",
    r"일반철골구조",
    r"경량철골구조",
    r"경량철골조",
    r"철골구조위 칼라강판지붕",
    r"철골구조",
    r"벽돌조 슬래브지붕",
    r"벽돌및\s*시멘트\s*벽돌조\s*슬래브지",
    r"시멘트\s*블럭조\s*슬래브지붕",
    r"시멘트블럭조\s*강판지붕",
    r"시멘트브럭조\s*슬래브지붕",
    r"시멘벽돌조 슬래브지붕",
    r"일반목구조\s*아스팔트슁글지붕",
    r"아스팔트슁글지붕",
    r"기타지붕\(패널\)",
    r"기타지붕\[패널\]",
    r"기타지붕\(아스팔트싱글\)",
    r"기타지붕",
    r"샌드위치패널지붕",
    r"샌드위치\s*판넬조",
    r"컬러강판지붕",
    r"칼라강판지붕",
    r"강파이프구조",
    r"연와조 평스라브지붕",
    r"평스라브지붕",
    r"슬래브지붕",
    r"판넬지붕",
    r"판넬조",
    r"조립식판넬지붕",
    r"조립식\s*판넬지붕",
    r"벽돌조",
]


def strip_building_materials(text: str) -> str:
    """건축자재 키워드를 텍스트에서 제거."""
    for pat in BUILDING_MATERIAL_PATTERNS:
        text = re.sub(pat, "", text)
    # 건축자재 제거 후 남은 연결 조사 정리
    text = re.sub(r"(?:철근)?콘크리트\s*(?:평\s*)?슬래브\s*및?", "", text)
    text = re.sub(r"철근\s*콘크리트\s*및?", "", text)
    text = re.sub(r"철골\s*콘크리트\s*및?", "", text)
    text = re.sub(r"및\s+및", "및", text)
    text = re.sub(r"^\s*및\s+", "", text)
    text = re.sub(r"\[\s*\]", "", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _merge_lines(text: str) -> str:
    """셀 안의 줄바꿈을 한 칸 공백으로 치환."""
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


# --- 3. 제2종 근린생활시설 → 근린시설 ---
def compact_keun_rin(text: str) -> str:
    text = re.sub(r"[12]\s*,\s*[12]\s*종\s*근린생활시설", "근린시설", text)
    text = re.sub(r"제?\s*[12]\s*종\s*근린생활시설", "근린시설", text)
    text = re.sub(r"근린생활시설", "근린시설", text)
    return text


# --- 4. 농지취득자격증명 축약 ---
def compact_nongji(note: str) -> str:
    """
    - 농지취득자격증명원제출요 / 농지취득자격증명 제출요 / 농지취득자격증명 제출 필요 → 농지취득자격증명요
    - (미제출시 보증금 미반환 …) / (매각결정기일까지 미제출시…) 등 괄호 블록 삭제
    - [농지법 제8조 제1항 제1호에 따라 농지취득자격증명원 필요하지 않음] → 농지취득증명원불요
    """
    note = re.sub(r"농지취득자격증명\s*원?\s*제출요", "농지취득자격증명요", note)
    note = re.sub(r"농지취득자격증명\s*제출\s*필요", "농지취득자격증명요", note)
    note = re.sub(r"농지취득자격증명\s*제출요", "농지취득자격증명요", note)
    note = re.sub(r"\(매각결정기일\s*까지\s*미제출시[^)]*\)", "", note)
    note = re.sub(r"\(미제출시[^)]*\)", "", note)
    note = re.sub(r"농지법\s*제\s*8조\s*제1항\s*제1호에\s*따라\s*농지취득자격증명원\s*필요하지\s*않음", "농지취득증명원불요", note)
    return note


# --- 5. 비고 붙여쓰기 (공백 제거) ---
def strip_spaces_in_note(note: str) -> str:
    """비고는 띄어쓰기를 모두 제거하여 붙여 씀. 단, 쉼표/마침표/괄호/~/물음표는 유지."""
    note = re.sub(r"\s+", "", note)
    return note


# --- 6. 마지막 문장 마침표 삭제 ---
def strip_trailing_period(note: str) -> str:
    return re.sub(r"\.+$", "", note)


# --- 7. 조사/관용 축약 ---
def compact_particles(text: str) -> str:
    text = re.sub(r"~바람\b", "요", text)
    text = re.sub(r"임\s*\.?$", "", text)  # 문장 끝 '임'
    text = text.replace("제출바람", "제출요")
    text = text.replace("조경용으로조성된연못", "조경용조성연못")
    return text


# --- 8. 동일 면적의 연속 층수 합치기 ---
_LAYER_RE = re.compile(r"(\d+)\s*층\s*(\d+(?:\.\d+)?)\s*㎡")


def merge_equal_area_floors(detail: str) -> str:
    """'1층 73.88㎡ 2층 73.88㎡' → '1,2층각73.88㎡'"""
    # 단순 휴리스틱: 연속된 'N층 SSS㎡' 토큰을 스캔해 동일 면적이면 병합
    tokens = _LAYER_RE.findall(detail)
    if len(tokens) < 2:
        return detail
    # 그룹화: 연속된 동일면적 층
    groups = []  # list of (floors, area)
    current_floors = [tokens[0][0]]
    current_area = tokens[0][1]
    for f, a in tokens[1:]:
        if a == current_area:
            current_floors.append(f)
        else:
            groups.append((current_floors, current_area))
            current_floors = [f]
            current_area = a
    groups.append((current_floors, current_area))

    def render_group(floors, area):
        if len(floors) == 1:
            return f"{floors[0]}층{area}㎡"
        # 연속 숫자면 ~로, 아니면 쉼표로
        nums = [int(x) for x in floors]
        if nums == list(range(min(nums), max(nums) + 1)) and len(nums) >= 3:
            return f"{min(nums)}~{max(nums)}층각{area}㎡"
        return f"{','.join(floors)}층각{area}㎡"

    # 원본에서 매칭된 모든 "N층 A㎡" 패턴을 찾아서 첫 그룹부터 순차 치환
    replaced = detail
    idx = 0
    new_parts = []
    matches = list(_LAYER_RE.finditer(detail))
    last_end = 0
    group_idx = 0
    # 그룹 크기 만큼 매치를 소진
    cursor = 0
    out = []
    for floors, area in groups:
        n = len(floors)
        block_matches = matches[cursor:cursor + n]
        if not block_matches:
            break
        start = block_matches[0].start()
        end = block_matches[-1].end()
        out.append(detail[last_end:start])
        out.append(render_group(floors, area))
        last_end = end
        cursor += n
    out.append(detail[last_end:])
    return "".join(out)


# --- 9. 지분 표기 변환 ---
_SHARE_PATTERNS = [
    # '(갑구 16번 공유자 이인숙 지분 6597분의 2975 전부)'
    (re.compile(r"\(?\s*(갑구\s*\d+(?:-\d+)?번)\s*공유자\s+([가-힣A-Za-z0-9㈜\(\)\s]+?)\s+지분\s+(\d+(?:\.\d+)?)\s*분의\s*(\d+(?:\.\d+)?)\s+전부\s*\)?"),
     lambda m: f"{m.group(1).replace(' ', '')}공유자{m.group(2).strip().replace(' ', '')}{m.group(4)}/{m.group(3)}지분전부"),
    # '(소유자 박철진 지분 중 일부(443분의 218))' → '박철진218/443지분일부'
    (re.compile(r"\(?\s*소유자\s+([가-힣A-Za-z㈜\(\)\s]+?)\s+지분\s+중?\s*일부\s*\(?\s*(\d+)\s*분의\s*(\d+)\s*\)?\s*\)?"),
     lambda m: f"{m.group(1).strip()}{m.group(3)}/{m.group(2)}지분일부"),
    (re.compile(r"\(?\s*소유자\s+([가-힣A-Za-z㈜\(\)\s]+?)\s+지분\s+일부\s+(\d+)\s*분의\s*(\d+)\s*\)?"),
     lambda m: f"{m.group(1).strip()}{m.group(3)}/{m.group(2)}지분일부"),
    # '(공유자 주식회사 지우 지분 167분의 3 전부)' → '주식회사지우3/167지분전부'
    (re.compile(r"\(?\s*공유자\s+([가-힣A-Za-z㈜\(\)\s]+?)\s+지분\s+(\d+)\s*분의\s*(\d+)\s+전부\s*\)?"),
     lambda m: f"{m.group(1).strip().replace(' ', '')}{m.group(3)}/{m.group(2)}지분전부"),
    # '(고우열 지분 전부 131435분의 16520)'
    (re.compile(r"\(?\s*([가-힣A-Za-z㈜\(\)\s]+?)\s+지분\s+전부\s+(\d+)\s*분의\s*(\d+)\s*\)?"),
     lambda m: f"{m.group(1).strip().replace(' ', '')}{m.group(3)}/{m.group(2)}지분전부"),
    # '(채무자 김형래 지분 10분의1 전부)'
    (re.compile(r"\(?\s*채무자\s+([가-힣A-Za-z㈜\(\)\s]+?)\s+지분\s+(\d+)\s*분의\s*(\d+)\s+전부\s*\)?"),
     lambda m: f"{m.group(1).strip().replace(' ', '')}{m.group(3)}/{m.group(2)}지분전부"),
]


def convert_share_notation(text: str) -> str:
    for pat, repl in _SHARE_PATTERNS:
        text = pat.sub(repl, text)
    return text


# --- 10. 제시외 면적 합산 (최단 대표건물 선정) ---
_JESIWAE_AREA_RE = re.compile(r"제시외(?:건물)?[^\n㎡]*?([가-힣]+)\s*[^㎡]*?(\d+(?:\.\d+)?)㎡")


def sum_jesiwae(detail: str) -> str:
    """
    상세내역의 제시외 건물/수목 면적을 합산하고 대표 건물을 최단 명칭으로 요약.
    매우 복잡한 입력에는 불완전할 수 있음 — 완전치 않을 때 원본 유지.
    """
    matches = _JESIWAE_AREA_RE.findall(detail)
    if len(matches) < 2:
        return detail
    # 이름 중에 글자수 가장 짧은 것 선택
    names = [m[0] for m in matches]
    areas = [float(m[1]) for m in matches]
    shortest_name = sorted(names, key=len)[0]
    total = sum(areas)
    # 원본 제시외 블록을 통째로 대체
    new_repr = f"제시외 {shortest_name}등{total:g}㎡"
    # 가장 먼저 '제시외'가 나오는 지점부터 끝까지가 제시외 블록이라고 가정하는 건 너무 난폭.
    # 보수적으로: 원본을 그대로 두되, 뒤에 요약을 append 하지 않음 (리스크 방지).
    return detail  # 실제 합산은 LLM 보정 단계에서 더 정확하게 처리


# --- 11. 사건번호 (중복)/(병합) 비고 이동 ---
_DUP_RE = re.compile(r"(\d+타경\d+)\s*\((중복|병합)\)", re.MULTILINE)


def extract_duplicate_tag(case_no_block: str):
    """'2025타경5330\n2025타경181\n[중복]' 같은 블록에서 [중복]/(중복) 태그를 찾아 비고 이동용으로 분리."""
    tag = None
    m = re.search(r"\[?(중복|병합)\]?", case_no_block)
    if m:
        tag = m.group(1)
        # 2번째 사건번호 추출
        nums = re.findall(r"\d+타경\d+", case_no_block)
        if len(nums) >= 2:
            return nums[0], f"{nums[1]}[{tag}]"
    return case_no_block.strip(), None


# --- 12. 동소 처리 (연속 주소에서 지역 동일하면 2번째부터 '동소') ---
def convert_same_address(locations: List[str]) -> List[str]:
    """연속된 소재지에서 '시/군 + 읍면동/길' 수준이 동일하면 2번째부터 '동소 번지' 형태로."""
    if len(locations) <= 1:
        return locations
    out = [locations[0]]
    prev_prefix = _address_prefix(locations[0])
    for loc in locations[1:]:
        cur_prefix = _address_prefix(loc)
        if cur_prefix and cur_prefix == prev_prefix:
            rest = loc[len(cur_prefix):].strip()
            out.append(f"동소 {rest}")
        else:
            out.append(loc)
            prev_prefix = cur_prefix
    return out


def _address_prefix(addr: str) -> str:
    """주소에서 '시/군 + 읍면동/도로명' 수준의 공통 접두어 추출."""
    m = re.match(r"^\s*(\S+시|\S+군)\s+(\S+[읍면동리]|\S+[로길])\s+", addr)
    if m:
        return m.group(0).rstrip()
    m = re.match(r"^\s*(세종특별자치시)\s+(\S+[읍면동리]|\S+[로길])\s+", addr)
    if m:
        return m.group(0).rstrip()
    return ""


# --- 13. 최종 orchestrator: 레코드 단위 적용 ---
def apply_rules_to_record(rec: Dict) -> Dict:
    """
    레코드 형식:
    {
      'case_no': '2025타경142',
      'item_no': '1',
      'yongdo': '아파트',
      'locations': [{'addr': '...', 'detail': '...'}],
      'price': '330,000,000',
      'min_price': '330,000,000',
      'note': '...'
    }
    """
    new_loc_addrs = []
    new_loc_details = []
    new_uses = []  # 소재지별 용도 (상세내역 용도 중복 제거 후 추출)

    for loc in rec.get("locations", []):
        addr = _merge_lines(loc.get("addr", ""))
        detail = _merge_lines(loc.get("detail", ""))

        # 도명 삭제 (세종특별자치시는 유지)
        addr = strip_city_prefix(addr)
        detail = strip_city_prefix(detail)

        # 건축자재 삭제
        detail = strip_building_materials(detail)
        # 근린시설 축약
        detail = compact_keun_rin(detail)
        # 지분표기
        detail = convert_share_notation(detail)
        addr = convert_share_notation(addr)
        # 동일면적 층 병합
        detail = merge_equal_area_floors(detail)

        # 상세 첫 토큰에서 용도 추출 (예: '대 264.6㎡' → 용도 '대', 나머지 '264.6㎡')
        use_token, remain = _split_use_token(detail)
        new_uses.append(use_token)

        # 주소 + 면적 결합
        merged = (addr.strip() + " " + remain.strip()).strip()
        new_loc_addrs.append(addr.strip())
        new_loc_details.append(merged)

    # 동소 처리
    new_loc_details = convert_same_address(new_loc_details)

    # 비고
    note = rec.get("note", "")
    note = compact_nongji(note)
    note = compact_particles(note)
    note = strip_spaces_in_note(note)
    note = strip_trailing_period(note)

    return {
        "case_no": rec["case_no"],
        "item_no": rec.get("item_no", "1"),
        "yongdo": rec.get("yongdo", ""),
        "locations": new_loc_details,
        "uses": new_uses,
        "price": rec.get("price", ""),
        "min_price": rec.get("min_price", ""),
        "note": note,
    }


_USE_HEAD_RE = re.compile(r"^(대|답|전|임야|도로|주유소용지|공장용지|체육용지|창고용지|기타)\s*(.*)$")


def _split_use_token(detail: str) -> tuple:
    """상세 텍스트 첫 부분이 지목 용도로 시작하면 분리. 예) '대 264.6㎡' → ('대', '264.6㎡')"""
    detail = detail.strip()
    m = _USE_HEAD_RE.match(detail)
    if m:
        return m.group(1), m.group(2)
    # '4층 다가구주택(6가구)' 같은 건물 용도도 감지
    m2 = re.match(r"^(\d+층\s*)?(아파트|오피스텔|연립주택|다가구주택|단독주택|근린시설|공장|창고|주택)", detail)
    if m2:
        return m2.group(2), detail
    return "", detail


# --- 14. 용도 정규화 (그룹핑용) ---
def normalize_yongdo(y: str) -> str:
    y = y.strip()
    if y in ("아파트",):
        return "아파트"
    if y in ("오피스텔",):
        return "오피스텔"
    if y in ("연립주택", "다세대주택"):
        return "연립주택"
    if y in ("다가구주택", "단독주택,다가구주택", "단독주택", "단독주택,\n다가구주택"):
        return "단독주택,다가구주택"
    if y in ("근린시설",):
        return "근린시설"
    if y in ("임야", "전답", "대지,임야,전답", "대지/임야/전답", "대지,임야,\n전답"):
        return "대지/임야/전답"
    return "기타"


GROUP_ORDER = ["아파트", "오피스텔", "연립주택", "단독주택,다가구주택", "근린시설", "대지/임야/전답", "기타"]
