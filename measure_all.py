"""
완료 폴더 전체에 대한 정확도 측정.

전략:
  - "-완료" 표시가 있는 폴더만 처리
  - 폴더 안에서 원고(hwp/hwpx)와 최종본(수정* PDF) 자동 매칭
  - 원고를 qwen_pipeline에 입력해 결과 JSON 저장
  - 최종본을 truth_extract로 정답 추출
  - 비교 → 정확도 + 누락/오분류 패턴
"""

import json
import re
import sys
import time
import traceback
from pathlib import Path

from orchestrator import extract_raw_text
from qwen_pipeline import run_pipeline
from truth_extract import extract_groups, GROUPS

ROOT = Path("0320 대구지방법원 서부지원 경매4계-완료")
OUT = Path("measurement_all")
OUT.mkdir(exist_ok=True)


def find_completed_folders():
    folders = []
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir():
            continue
        if "-완료" in d.name:
            folders.append(d)
    return folders


def find_raw_and_final(folder: Path):
    """폴더 안에서 원고 hwp와 최종본 pdf를 매칭.
    원고/ 또는 최종/ 서브폴더가 있으면 그 안에서 우선 검색.
    """
    name = folder.name.replace("-완료", "").strip()
    date_prefix = name.split()[0] if name else ""

    # 검색 위치 우선순위: 원고/ 서브폴더 → 폴더 직접
    raw_dirs = [folder / "원고", folder] if (folder / "원고").is_dir() else [folder]
    final_dirs = [folder / "최종", folder] if (folder / "최종").is_dir() else [folder]

    raw = None
    for d in raw_dirs:
        hwps = list(d.glob("*.hwp")) + list(d.glob("*.hwpx"))
        if not hwps:
            continue
        by_prefix = [f for f in hwps if f.stem.startswith(date_prefix)]
        by_cs = [f for f in hwps if f.stem.startswith("CS_")]
        if by_prefix:
            raw = sorted(by_prefix, key=lambda f: f.stat().st_size, reverse=True)[0]
        elif by_cs:
            raw = sorted(by_cs, key=lambda f: f.stat().st_size, reverse=True)[0]
        else:
            raw = sorted(hwps, key=lambda f: f.stat().st_size, reverse=True)[0]
        break

    final = None
    for d in final_dirs:
        pdfs = list(d.glob("*.pdf"))
        if not pdfs:
            continue
        by_susu = [f for f in pdfs if f.name.startswith("수정")]
        # 폴더 prefix와 일치하는 PDF 우선
        by_match = [f for f in pdfs if f.stem.startswith(date_prefix) or any(part in f.stem for part in name.split() if len(part) > 2)]
        if by_susu:
            final = by_susu[0]
        elif by_match:
            final = by_match[0]
        else:
            final = pdfs[0]
        break

    return raw, final


def safe_label(folder_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9가-힣()]+", "_", folder_name)[:80]


def measure_one(folder: Path) -> dict:
    label = folder.name.replace("-완료", "").strip()
    raw, final_pdf = find_raw_and_final(folder)

    print(f"\n{'='*70}\n  [{label}]\n{'='*70}", flush=True)
    if not raw:
        print(f"  ✗ 원고 hwp 없음", flush=True)
        return {"label": label, "error": "원고 없음"}
    if not final_pdf:
        print(f"  ✗ 최종본 pdf 없음", flush=True)
        return {"label": label, "error": "최종본 없음"}

    print(f"  원고: {raw.name}", flush=True)
    print(f"  최종: {final_pdf.name}", flush=True)

    # 정답 추출
    try:
        truth = extract_groups(final_pdf)
        truth_total = sum(len(v) for v in truth.values())
        print(f"  정답 사건: {truth_total}건", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 정답 추출 실패: {e}", flush=True)
        return {"label": label, "error": f"정답추출: {e}"}

    # 우리 시스템 처리
    try:
        text = extract_raw_text(str(raw))
        print(f"  hwp 추출 {len(text)}자", flush=True)
        t0 = time.time()
        result = run_pipeline(text, on_progress=lambda m: print(f"    · {m}", flush=True))
        elapsed = round(time.time() - t0, 1)
        print(f"  편집 완료 {elapsed}s, 사건 {len(result.get('records') or [])}건", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 편집 실패: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return {"label": label, "error": f"편집: {e}"}

    # 결과 저장
    safe = safe_label(label)
    (OUT / f"{safe}_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / f"{safe}_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 그룹별 비교
    our_by_g = {}
    for r in result.get("records") or []:
        g = r.get("group", "기타")
        our_by_g.setdefault(g, []).append(r.get("case_no", "?"))

    excl = our_by_g.get("게재제외", [])
    matched = sum(len(set(our_by_g.get(g, [])) & set(truth[g])) for g in GROUPS)
    rate = matched / truth_total * 100 if truth_total else 0

    print(f"  그룹 일치율: {matched}/{truth_total} = {rate:.1f}%", flush=True)
    if excl:
        print(f"  📌 게재제외: {excl}", flush=True)

    return {
        "label": label,
        "raw": raw.name,
        "final": final_pdf.name,
        "truth_total": truth_total,
        "our_total_pub": sum(len(our_by_g.get(g, [])) for g in GROUPS),
        "exclude": excl,
        "matched": matched,
        "rate": round(rate, 1),
        "elapsed_sec": elapsed,
        "our_by_group": {g: sorted(set(v)) for g, v in our_by_g.items()},
        "truth_by_group": truth,
    }


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    folders = find_completed_folders()
    if only:
        folders = [f for f in folders if only in f.name]
    print(f"측정 대상: {len(folders)} 폴더\n", flush=True)

    summary = []
    for folder in folders:
        try:
            s = measure_one(folder)
            summary.append(s)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 예외: {type(e).__name__}: {e}", flush=True)
            summary.append({"label": folder.name, "error": str(e)})

        # 진행 상황 누적 저장
        (OUT / "_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 종합
    print(f"\n\n{'='*70}\n  종합 ({len(summary)}건)\n{'='*70}", flush=True)
    print(f"\n{'폴더':<50} {'정답':>5} {'우리':>5} {'제외':>5} {'일치':>5} {'정확도':>7}", flush=True)
    print("-" * 84, flush=True)
    total_truth = total_match = total_excl = 0
    failed = 0
    for s in summary:
        if "error" in s:
            print(f"  ✗ {s['label']:<48}  {s['error']}", flush=True)
            failed += 1
            continue
        print(
            f"{s['label'][:48]:<50} {s['truth_total']:>5} {s['our_total_pub']:>5} {len(s['exclude']):>5} {s['matched']:>5} {s['rate']:>6.1f}%",
            flush=True,
        )
        total_truth += s["truth_total"]
        total_match += s["matched"]
        total_excl += len(s["exclude"])
    print("-" * 84, flush=True)
    if total_truth:
        print(
            f"{'전체':<50} {total_truth:>5} {'':>5} {total_excl:>5} {total_match:>5} {total_match/total_truth*100:>6.1f}%",
            flush=True,
        )
    if failed:
        print(f"실패 {failed}건", flush=True)


if __name__ == "__main__":
    main()
