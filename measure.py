"""
정확도 측정 스크립트.

각 원고(.hwp/.hwpx)를 qwen_pipeline에 입력해 결과 JSON을 저장하고,
짝이 되는 최종본 PDF에서 그룹별 사건번호를 자동 추출해 정답으로 사용한다.
"""

import json
import re
import sys
import time
from pathlib import Path

import pdfplumber

from orchestrator import extract_raw_text
from qwen_pipeline import run_pipeline


BASE = Path("0320 대구지방법원 서부지원 경매4계-완료") / "0320 대구지방법원 서부지원 경매4계-완료" / "분류"
RAW_DIR = BASE / "원고"
FINAL_DIR = BASE / "최종"

PAIRS = [
    ("수정 의정부지방법원 경매3계 0320(5단).pdf.hwp", "수정 의정부지방법원 경매3계 0320(5단).pdf", "의정부 경매3계"),
    ("대전지방법원 공주지원 경매2계 0320(5단).pdf.hwpx", "대전지방법원 공주지원 경매2계 0320(5단).pdf", "대전 공주지원 경매2계"),
    ("춘천지방법원 원주지원 경매3계 0321(6단).hwp", "춘천지방법원 원주지원 경매3계 0321(6단).pdf", "춘천 원주지원 경매3계"),
    ("대구지방법원 서부지원 경매4계 0320(6단).hwp", "대구지방법원 서부지원 경매4계 0320(6단).pdf", "대구 서부지원 경매4계"),
    ("서울북부지방법원 경매9계 0321(5단).hwp", "서울북부지방법원 경매9계 0321(5단).pdf", "서울북부 경매9계"),
]

GROUPS = ["아파트", "연립주택/다세대/빌라", "단독주택,다가구주택", "상가/오피스텔,근린시설", "대지/임야/전답", "기타"]

OUT = Path("measurement_results")
OUT.mkdir(exist_ok=True)


def extract_truth_from_pdf(pdf_path: Path) -> dict:
    """최종본 PDF → 그룹별 사건번호 리스트."""
    with pdfplumber.open(str(pdf_path)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    pattern = re.compile(r"\[(" + "|".join(re.escape(g) for g in GROUPS) + r")\]")
    matches = list(pattern.finditer(text))
    result = {g: [] for g in GROUPS}
    for i, m in enumerate(matches):
        g = m.group(1)
        body = text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        seen = set()
        for cn in re.findall(r"\d{4}\s*타경\s*\d+", body):
            c = re.sub(r"\s+", "", cn)
            if c not in seen:
                seen.add(c)
                result[g].append(c)
    return result


def measure_one(raw_name: str, final_name: str, label: str) -> dict:
    raw_path = RAW_DIR / raw_name
    final_path = FINAL_DIR / final_name

    print(f"\n=== [{label}] ===", flush=True)
    print(f"  원고: {raw_name}", flush=True)
    print(f"  최종: {final_name}", flush=True)

    # 정답 추출 (PDF)
    truth = extract_truth_from_pdf(final_path)
    truth_total = sum(len(v) for v in truth.values())
    print(f"  최종본에서 사건번호 {truth_total}건 추출", flush=True)
    for g, cs in truth.items():
        if cs:
            print(f"    [{g}] {len(cs)}건: {cs}", flush=True)

    # 우리 시스템 처리
    t0 = time.time()
    text = extract_raw_text(str(raw_path))
    print(f"  hwp 추출 {len(text)}자, {round(time.time()-t0,1)}s", flush=True)

    t0 = time.time()
    result = run_pipeline(text, on_progress=lambda m: print(f"    · {m}", flush=True))
    elapsed = round(time.time() - t0, 1)
    print(f"  편집 완료 {elapsed}s, 사건 {len(result.get('records') or [])}건", flush=True)

    # 결과 저장
    safe = re.sub(r"[^A-Za-z0-9가-힣]+", "_", label)
    out_json = OUT / f"{safe}.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    out_truth = OUT / f"{safe}_truth.json"
    out_truth.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")

    # 그룹 비교
    our_by_group = {g: [] for g in GROUPS}
    for r in result.get("records") or []:
        g = r.get("group", "기타")
        if g not in our_by_group:
            g = "기타"
        our_by_group[g].append(r.get("case_no", "?"))

    diff_lines = []
    for g in GROUPS:
        our_set = set(our_by_group[g])
        truth_set = set(truth[g])
        missing = sorted(truth_set - our_set)  # 정답엔 있는데 우리 결과엔 없음
        extra = sorted(our_set - truth_set)  # 우리만 분류
        if missing or extra:
            diff_lines.append(f"  [{g}]")
            if missing:
                diff_lines.append(f"    누락(정답엔 있음): {missing}")
            if extra:
                diff_lines.append(f"    추가(우리만): {extra}")

    if diff_lines:
        print("  그룹 분류 차이:", flush=True)
        for line in diff_lines:
            print(line, flush=True)
    else:
        print("  ✓ 그룹 분류 100% 일치", flush=True)

    return {
        "label": label,
        "elapsed_sec": elapsed,
        "our_record_count": len(result.get("records") or []),
        "truth_total": truth_total,
        "our_by_group": {g: sorted(set(v)) for g, v in our_by_group.items()},
        "truth_by_group": truth,
    }


def main():
    only = None
    if len(sys.argv) > 1:
        only = sys.argv[1]

    summary = []
    for raw, final, label in PAIRS:
        if only and only not in label:
            continue
        try:
            s = measure_one(raw, final, label)
            summary.append(s)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 실패: {type(e).__name__}: {e}", flush=True)
            summary.append({"label": label, "error": str(e)})

    # 종합 리포트
    print("\n\n=== 종합 ===", flush=True)
    for s in summary:
        if "error" in s:
            print(f"  ✗ {s['label']}: 오류 {s['error']}", flush=True)
            continue
        ours = sum(len(v) for v in s["our_by_group"].values())
        truth = s["truth_total"]
        match = sum(
            len(set(s["our_by_group"][g]) & set(s["truth_by_group"][g])) for g in GROUPS
        )
        print(
            f"  {s['label']}: 우리 {ours}건 / 정답 {truth}건 / "
            f"그룹일치 {match}건 ({(match/truth*100 if truth else 0):.1f}%) / {s['elapsed_sec']}s",
            flush=True,
        )

    (OUT / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n결과 폴더: {OUT.resolve()}", flush=True)


if __name__ == "__main__":
    main()
