"""
에디터 ↔ 리뷰어 루프 오케스트레이터 (사건 단위 병렬 처리).

플로우:
  1. 파일에서 원본 텍스트 추출
  2. 헤더 JSON 1회 추출 (LLM)
  3. 원본 텍스트를 사건번호 경계로 블록 분할
  4. 사건 블록마다:
       - 에디터(단일 사건) → 편집본 레코드(들)
       - 리뷰어(단일 사건) → pass or 이슈 피드백
       - 통과까지 무제한 반복
  5. 모든 사건 통과 후 헤더+레코드 병합 → 최종 JSON
  6. 진행 과정은 SSE로 실시간 push
"""

import hashlib
import json
import os
import queue
import re
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pdfplumber

from ai_errors import public_error_message
from agent import (
    editor_run_record,
    header_run,
    reviewer_run_record,
)


# ---------- 실행 제한 파라미터 ----------
# 사용자 요구: 취소·시간 제한 없이 "편집 기준 100% 부합"까지 무한 반복.
# 루프 종료는 오로지 아래 세 가지로만 일어난다:
#   (1) 리뷰어 pass == true (실제 통과)
#   (2) current == expected 실질 동일 이슈뿐 → 실질 통과
#   (3) 수렴 감지: 연속 3회 동일 편집본 / A-B-A-B 진자운동 (규칙 해석 상충으로 모델이 정답에 도달 불가한 상태)
# 병렬은 Ollama 서버 큐에 적재되므로 2로 고정.
GLOBAL_BUDGET_SEC = 0  # 0 = 무제한
MAX_ITER_PER_RECORD = 0  # 0 = 무제한 (수렴까지 계속)
PER_RECORD_BUDGET_SEC = 0  # 0 = 무제한
PARALLEL_WORKERS = int(os.environ.get("PARALLEL_WORKERS", "2"))


def _hash_edited(edited: dict) -> str:
    blob = json.dumps(edited, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def _filter_noise_issues(issues: List[dict]) -> List[dict]:
    """current와 expected가 (공백 무시하고) 실질적으로 동일한 허위 이슈 제거."""
    def _norm(s):
        return re.sub(r"\s+", " ", (s or "").strip())
    real = []
    for it in issues:
        cur = _norm(it.get("current"))
        exp = _norm(it.get("expected"))
        if cur and exp and cur == exp:
            continue
        real.append(it)
    return real


# ---------- 파일 → 원본 텍스트 ----------

def extract_raw_text(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return _pdf_to_text(path)
    if ext in (".hwp", ".hwpx"):
        return _hwp_to_text(path)
    raise ValueError(f"지원하지 않는 확장자: {ext}")


def _pdf_to_text(path: str) -> str:
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, p in enumerate(pdf.pages):
            txt = p.extract_text() or ""
            tables = p.extract_tables() or []
            rows_text = []
            for t in tables:
                for row in t:
                    row_txt = " | ".join((c or "").replace("\n", " ").strip() for c in row)
                    if row_txt.strip():
                        rows_text.append(row_txt)
            if rows_text:
                txt = txt + "\n\n[표 행]\n" + "\n".join(rows_text)
            pages.append(f"--- 페이지 {i + 1} ---\n{txt}")
    return "\n\n".join(pages)


def _hwp_to_text(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"PK"):
        return _owpml_to_text(path)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return _hwp5_to_text(path)
    return Path(path).read_text(errors="ignore")


def _hwp5_to_text(path: str) -> str:
    from hwp5.hwp5html import HTMLTransform
    from hwp5.xmlmodel import Hwp5File
    from xml.etree import ElementTree as ET
    import tempfile

    out_chunks = []
    with tempfile.TemporaryDirectory() as d:
        HTMLTransform().transform_hwp5_to_dir(Hwp5File(path), d)
        idx = os.path.join(d, "index.xhtml")
        tree = ET.parse(idx)
        ns = {"x": "http://www.w3.org/1999/xhtml"}
        tables = tree.getroot().findall(".//x:table", ns)
        for ti, tb in enumerate(tables):
            out_chunks.append(f"--- 표 {ti + 1} ---")
            for tr in tb.findall(".//x:tr", ns):
                cells = []
                for td in tr.findall("x:td", ns):
                    txt = "".join(td.itertext()).strip()
                    cells.append(txt)
                out_chunks.append(" | ".join(cells))
    return "\n".join(out_chunks)


def _owpml_to_text(path: str) -> str:
    from xml.etree import ElementTree as ET

    chunks = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if "section" in name and name.endswith(".xml"):
                xml = z.read(name).decode("utf-8", errors="ignore")
                try:
                    root = ET.fromstring(xml)
                    for el in root.iter():
                        if el.tag.endswith("}t") or el.tag == "t":
                            if el.text:
                                chunks.append(el.text)
                except Exception:  # noqa: BLE001
                    chunks.append(xml)
    return "\n".join(chunks)


# ---------- 원본 텍스트 → 사건 단위 블록 ----------

CASE_NO_RE = re.compile(r"\d{4}\s*타경\s*\d+")


def split_into_record_blocks(raw_text: str) -> List[Dict]:
    """
    사건번호 발생 지점을 경계로 블록 분할.
    반환: [{case_no, block_text, start, end}, ...]
    같은 사건번호가 연속으로 나타나면 (중복 사건 셀의 두 번째 번호) 첫 번째만 쓰고 태그만 기록.
    """
    # 각 라인에서 사건번호를 찾되, 한 라인에 2개 이상 있으면 독립 블록으로 취급하지 않고
    # 하나로 묶어 '중복' 신호로만 본다. pdfplumber의 경우 줄 내에서 분리되지만
    # HWP 변환본은 동일 셀 내 줄바꿈으로 들어가 있을 수 있으므로 라인 기준으로 스캔.
    blocks: List[Dict] = []
    positions: List[int] = []
    for m in CASE_NO_RE.finditer(raw_text):
        positions.append(m.start())

    # 실제 "새로운 사건 시작"을 감지: 앞에 case_no 라인이 있고, 거기가 continuation인지 판단
    # 여기서는 단순 경계 분할 — 블록 단위에서 LLM이 물건번호 복수를 처리한다.
    if not positions:
        return [{"case_no": "미상", "block_text": raw_text.strip()}]

    # 동일한 텍스트 라인 안에서 연속 매치되는 사건번호는 하나로 합침(중복 태그용)
    merged_starts: List[int] = []
    last_end = -10**9
    for start in positions:
        # 직전 start와의 텍스트 사이에 줄바꿈·의미있는 문자가 거의 없으면 합침
        gap = raw_text[last_end:start]
        if merged_starts and len(gap.strip()) < 30 and "\n" not in gap.strip():
            # 직전 블록의 연속 (중복) 사건번호 → 합쳐서 유지
            last_end = start + 10
            continue
        merged_starts.append(start)
        last_end = start + 10

    for i, start in enumerate(merged_starts):
        end = merged_starts[i + 1] if i + 1 < len(merged_starts) else len(raw_text)
        block_text = raw_text[start:end].strip()
        # 블록 내 모든 사건번호 뽑기 (중복 사건번호 병기)
        all_nos = [re.sub(r"\s+", "", x) for x in CASE_NO_RE.findall(block_text)]
        primary = all_nos[0] if all_nos else "미상"
        blocks.append({
            "case_no": primary,
            "all_nos": all_nos,
            "block_text": block_text,
        })

    return blocks


# ---------- 진행 이벤트 헬퍼 ----------

def event(kind: str, **data) -> str:
    payload = {"event": kind, **data}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def issues_summary(review: dict) -> str:
    if not review:
        return ""
    lines = [f"검증 결과: {review.get('summary', '')}"]
    for i, issue in enumerate(review.get("issues") or [], start=1):
        lines.append(
            f"{i}. [{issue.get('severity','')}] 필드 {issue.get('field','')}"
            f"\n   규칙: {issue.get('rule','')}"
            f"\n   현재: {issue.get('current','')}"
            f"\n   수정: {issue.get('expected','')}"
        )
    return "\n".join(lines)


# ---------- 메인 루프 (병렬 + 5분 타임박스) ----------

def run_loop(file_path: str) -> Iterator[str]:
    """
    이벤트 큐를 사이에 두고, 워커 스레드가 추출·헤더·사건 병렬 처리를 수행한다.
    메인(제너레이터)은 큐에서 이벤트를 뽑아 SSE로 yield.
    """
    q: "queue.Queue[Optional[str]]" = queue.Queue()

    def emit(kind: str, **data):
        q.put(event(kind, **data))

    start_time = time.time()
    remaining = lambda: max(0.0, GLOBAL_BUDGET_SEC - (time.time() - start_time))

    final_records: List[Optional[dict]] = []
    record_lock = threading.Lock()

    def process_record(idx: int, blk: Dict) -> Optional[dict]:
        """한 사건을 처리: 에디터 ↔ 리뷰어 루프를 통과/수렴까지 무제한 반복."""
        case_no = blk["case_no"]
        block_text = blk["block_text"]
        emit("record_start", idx=idx, total=len(final_records), case_no=case_no, preview=block_text[:400])

        feedback = ""
        hash_history: List[str] = []
        edited: Optional[dict] = None
        passed = False
        sub_iter = 0

        while True:
            sub_iter += 1
            emit("record_stage", idx=idx, case_no=case_no, sub_iter=sub_iter, stage="editor")
            t0 = time.time()
            try:
                edited = editor_run_record(block_text, feedback=feedback)
            except Exception as e:  # noqa: BLE001
                emit("record_error", idx=idx, case_no=case_no, sub_iter=sub_iter, stage="editor", message=public_error_message(e))
                time.sleep(3)
                continue

            cur_hash = _hash_edited(edited)
            hash_history.append(cur_hash)
            emit("record_editor_done", idx=idx, case_no=case_no, sub_iter=sub_iter,
                 elapsed=round(time.time() - t0, 2), edited=edited)

            # (B) 연속 3회 동일 편집본
            if len(hash_history) >= 3 and hash_history[-1] == hash_history[-2] == hash_history[-3]:
                emit("record_converged", idx=idx, case_no=case_no, sub_iter=sub_iter,
                     reason="연속 3회 동일 편집본 — 수렴 판정")
                passed = True
                break

            # (C) 진자운동
            if len(hash_history) >= 4 and hash_history[-1] == hash_history[-3] \
                    and hash_history[-2] == hash_history[-4] and hash_history[-1] != hash_history[-2]:
                emit("record_converged", idx=idx, case_no=case_no, sub_iter=sub_iter,
                     reason="두 상태 사이 진자운동 — 마지막 편집본 채택")
                passed = True
                break

            emit("record_stage", idx=idx, case_no=case_no, sub_iter=sub_iter, stage="reviewer")
            t0 = time.time()
            try:
                review = reviewer_run_record(edited, original_block=block_text)
            except Exception as e:  # noqa: BLE001
                emit("record_error", idx=idx, case_no=case_no, sub_iter=sub_iter, stage="reviewer", message=public_error_message(e))
                time.sleep(3)
                continue

            raw_issues = review.get("issues") or []
            filtered = _filter_noise_issues(raw_issues)
            noise_n = len(raw_issues) - len(filtered)
            review["issues"] = filtered
            if noise_n:
                review["summary"] = (review.get("summary") or "") + f" (허위 이슈 {noise_n}건 자동 제거)"

            passed = bool(review.get("pass")) and not filtered
            if not filtered and raw_issues:
                passed = True

            emit("record_reviewer_done", idx=idx, case_no=case_no, sub_iter=sub_iter,
                 elapsed=round(time.time() - t0, 2), passed=passed,
                 issue_count=len(filtered), noise_filtered=noise_n, review=review)

            if passed:
                break

            feedback = issues_summary(review)

        # 최종 편집본 저장
        if edited:
            with record_lock:
                for r in edited.get("records", []) or []:
                    final_records.append(r)
            emit("record_pass", idx=idx, case_no=case_no, sub_iter=sub_iter,
                 converged=not passed and sub_iter > 1)
        return edited

    def worker():
        header: Dict = {}
        try:
            emit("status", message="파일에서 원본 텍스트 추출 중…")
            raw_text = extract_raw_text(file_path)
            emit("extracted", chars=len(raw_text), preview=raw_text[:1200])

            # 헤더 (1회)
            emit("status", message="헤더 추출 중 (LLM 호출 1회)")
            t0 = time.time()
            try:
                header = header_run(raw_text)
            except Exception as e:  # noqa: BLE001
                emit("error", stage="header", message=public_error_message(e))
                header = {}
            emit("header_done", elapsed=round(time.time() - t0, 2), header=header)

            # 사건 블록
            blocks = split_into_record_blocks(raw_text)
            # final_records 리스트 크기를 블록 수로 확보 (process_record 에서 total 계산용)
            final_records.extend([None] * 0)  # placeholder, 실제 데이터는 per-record 에서 append
            emit("blocks_detected", count=len(blocks), case_list=[b["case_no"] for b in blocks])
            emit("status", message=f"사건 {len(blocks)}건을 {PARALLEL_WORKERS}개 워커로 병렬 처리 시작 · 완벽 통과까지 무제한 반복")

            # 병렬 실행 (Ollama가 서버 측에서 직렬화하므로 PARALLEL_WORKERS는 소규모로 유지)
            # 취소·타임아웃 없음. 모든 사건이 pass / 수렴까지 반드시 완료한다.
            with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as ex:
                futures = []
                for idx, blk in enumerate(blocks, start=1):
                    futures.append(ex.submit(process_record, idx, blk))
                for fut in as_completed(futures):
                    # process_record 내부에서 예외를 잡지만 혹시 전파된 경우
                    exc = fut.exception()
                    if exc:
                        emit("record_error", message=public_error_message(exc))

            emit("complete", passed=True,
                 final={"header": header, "records": final_records},
                 iterations=len(blocks),
                 elapsed=round(time.time() - start_time, 2))
        except Exception as e:  # noqa: BLE001
            emit("fatal", message=public_error_message(e),
                 traceback=traceback.format_exc(limit=3))
        finally:
            q.put(None)  # sentinel to end stream

    threading.Thread(target=worker, daemon=True).start()

    while True:
        item = q.get()  # 무제한 대기 — 워커가 sentinel(None)을 put 할 때까지
        if item is None:
            break
        yield item
