"""
신문 게재본 PDF에서 그룹별 사건번호를 신뢰성 있게 추출.

전략:
  1) 모든 사건번호 후보를 좌표와 함께 수집 (단어 단위 + 표 단위)
  2) 같은 페이지의 그룹 헤더 좌표와 매칭 (같은 컬럼 우선 → 위쪽 헤더 → 직전 그룹)
  3) 단어 단위에서 발견된 사건번호만 신뢰 — 본문 정규식 false positive 제거
  4) 자체 검증: missing(누락) / extra(노이즈) 보고
"""

import re
from pathlib import Path

import pdfplumber

GROUPS = [
    "아파트",
    "연립주택/다세대/빌라",
    "단독주택,다가구주택",
    "상가/오피스텔,근린시설",
    "대지/임야/전답",
    "기타",
]

GROUP_PAT = re.compile(r"\[(" + "|".join(re.escape(g) for g in GROUPS) + r")\]")
CASE_INLINE = re.compile(r"^(\d{4})\s*타\s*경\s*(\d+)$")
CASE_PREFIX = re.compile(r"^(\d{4})\s*타\s*경$")
CASE_TEXT = re.compile(r"(\d{4})\s*타\s*경\s*(\d+)")


def _is_group_header_word(text: str):
    m = GROUP_PAT.fullmatch(text.strip())
    return m.group(1) if m else None


def _extract_case_positions(words):
    """
    단어 단위로 사건번호 위치 추출.
    반환: [(top, x0, case_no, source)]

    - 일체형: '2024타경5113'
    - 접두형: '2024타경' + 같은 컬럼 바로 아래 줄 숫자.
      물건번호("1")가 prefix 같은 줄 우측에 있는 신문 표 구조 때문에 같은-줄 매칭은 사용 안 함.
    """
    cases = []
    used = set()
    for i, w in enumerate(words):
        if i in used:
            continue
        text = w["text"]
        m = CASE_INLINE.match(text)
        if m:
            cases.append((w["top"], w["x0"], f"{m.group(1)}타경{m.group(2)}", "inline"))
            used.add(i)
            continue
        m = CASE_PREFIX.match(text)
        if not m:
            continue
        # 같은 컬럼(x ±60) 가장 가까운 아래 줄 숫자
        candidates = []
        for j in range(i + 1, len(words)):
            if j in used:
                continue
            nw = words[j]
            if not re.match(r"^\d+$", nw["text"]):
                continue
            dy = nw["top"] - w["top"]
            if dy <= 0:
                continue
            if dy > 30:
                break  # 충분히 아래 줄까지 다 봤음
            if abs(nw["x0"] - w["x0"]) > 60:
                continue
            candidates.append((dy, j, nw))
        if candidates:
            candidates.sort()
            _, j, nw = candidates[0]
            cases.append(
                (w["top"], w["x0"], f"{m.group(1)}타경{nw['text']}", "prefix")
            )
            used.add(i)
            used.add(j)
    return cases


def _extract_case_from_tables(page):
    """
    표 단위로 사건번호 추출. 표의 첫 셀에서 패턴 매칭.
    반환: [(top, x0, case_no, source)]
    """
    cases = []
    for tbl in page.find_tables():
        rows = tbl.extract() or []
        first_cell = (rows[0][0] if rows and rows[0] else "") or ""
        if any(k in first_cell for k in ("사건번호", "매각물건", "물건번호")):
            continue
        x0, top, _x1, _bottom = tbl.bbox
        for row in rows:
            if not row:
                continue
            cell = row[0] or ""
            m = CASE_TEXT.search(cell)
            if not m:
                continue
            cn = f"{m.group(1)}타경{m.group(2)}"
            cases.append((top, x0, cn, "table"))
    return cases


def _assign_group(top, x0, group_positions, last_group):
    cand_col = []
    cand_any = []
    for gtop, gx0, gname in group_positions:
        if gtop > top:
            continue
        cand_any.append((top - gtop, gname))
        if abs(gx0 - x0) <= 100:
            cand_col.append((top - gtop, gname))
    if cand_col:
        return min(cand_col)[1]
    if cand_any:
        return min(cand_any)[1]
    return last_group or "기타"


def extract_groups(pdf_path) -> dict:
    truth = {g: [] for g in GROUPS}
    seen = {g: set() for g in GROUPS}
    last_group = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            # 단어 추출
            words = page.extract_words(use_text_flow=False)

            # 그룹 헤더 위치
            group_positions = []
            for w in words:
                gname = _is_group_header_word(w["text"])
                if gname:
                    group_positions.append((w["top"], w["x0"], gname))

            # 사건번호 후보 수집 (단어 + 표)
            candidates = []
            candidates.extend(_extract_case_positions(words))
            candidates.extend(_extract_case_from_tables(page))

            # 같은 사건번호가 여러 후보에서 나오면 가장 위쪽 위치 사용
            best = {}
            for top, x0, cn, src in candidates:
                if cn not in best or top < best[cn][0]:
                    best[cn] = (top, x0, src)

            # 좌표 순서대로 그룹 매칭
            sorted_cases = sorted(best.items(), key=lambda x: (x[1][0], x[1][1]))
            for cn, (top, x0, _src) in sorted_cases:
                g = _assign_group(top, x0, group_positions, last_group)
                last_group = g
                if cn not in seen[g]:
                    seen[g].add(cn)
                    truth[g].append(cn)

    return truth


def extract_with_validation(pdf_path) -> dict:
    """추출 + 자체 검증."""
    groups = extract_groups(pdf_path)
    extracted = set()
    for v in groups.values():
        extracted.update(v)

    # 본문/표 텍스트에서 모든 사건번호 패턴 (참고용)
    text_cases = set()
    with pdfplumber.open(str(pdf_path)) as pdf:
        for p in pdf.pages:
            text = p.extract_text() or ""
            for m in CASE_TEXT.finditer(text):
                text_cases.add(f"{m.group(1)}타경{m.group(2)}")
            for tbl in p.extract_tables() or []:
                for row in tbl:
                    for c in row:
                        if c:
                            for m in CASE_TEXT.finditer(c):
                                text_cases.add(f"{m.group(1)}타경{m.group(2)}")

    # 본문에 있으나 우리가 못 잡은 것 → 누락 의심
    missing = sorted(text_cases - extracted)
    # 우리가 잡았으나 본문에 없는 것 → 알고리즘 오류 (있으면 안 됨)
    extra_unknown = sorted(extracted - text_cases)

    return {
        "groups": groups,
        "missing_from_groups": missing,
        "extra_not_in_text": extra_unknown,
        "all_in_text": sorted(text_cases),
    }


def main():
    import sys

    paths = sys.argv[1:]
    if not paths:
        base = (
            Path("0320 대구지방법원 서부지원 경매4계-완료")
            / "0320 대구지방법원 서부지원 경매4계-완료"
            / "분류"
            / "최종"
        )
        paths = sorted(str(p) for p in base.glob("*.pdf"))

    all_ok = True
    for p in paths:
        print(f"\n=== {Path(p).name} ===")
        v = extract_with_validation(p)
        groups = v["groups"]
        total = sum(len(x) for x in groups.values())
        print(f"  추출 {total}건 / 본문 패턴 {len(v['all_in_text'])}건")
        for g in GROUPS:
            if groups[g]:
                print(f"  [{g}] {len(groups[g])}건: {groups[g]}")
        if v["missing_from_groups"]:
            print(f"  ⚠ 본문엔 있으나 그룹 매핑에서 빠진 사건: {v['missing_from_groups']}")
            all_ok = False
        if v["extra_not_in_text"]:
            print(f"  ⚠ 본문엔 없는데 그룹에 든 사건: {v['extra_not_in_text']}")
            all_ok = False
        if not v["missing_from_groups"] and not v["extra_not_in_text"]:
            print("  ✓ 추출·검증 통과")

    print("\n" + ("=" * 50))
    print("전체 검증:", "✓ PASS" if all_ok else "⚠ FAIL")


if __name__ == "__main__":
    main()
