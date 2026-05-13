"""
에디터 에이전트 + 리뷰어 에이전트.

- EditorAgent: 원본 원고 텍스트(+직전 리뷰 피드백) → 편집 기준을 적용한 JSON 편집본
- ReviewerAgent: 편집본 JSON → 편집 기준 위반 사항 리스트. 위반 없으면 pass=true

모든 판단은 LLM이 수행한다. Python은 JSON 파싱·재시도만 담당한다.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from ai_errors import AIServiceError, normalize_ai_exception

BASE = Path(__file__).resolve().parent

OLLAMA_URL = os.environ.get("OLLAMA_URL", "https://ollama.hyphen.it.com/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-coder:30b-a3b-q4_K_M")
CF_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "77813d1cfde422b9a2e2dfff9bf199cd.access")
CF_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "135041cca9bdbf4858e8f0f4213035c132b5034252487655a57c3f1d3c9f4d0f")

HEADERS = {
    "Content-Type": "application/json",
    "CF-Access-Client-Id": CF_CLIENT_ID,
    "CF-Access-Client-Secret": CF_CLIENT_SECRET,
}


def _load_standards() -> str:
    p = BASE / "editing_standards.txt"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


EDITING_STANDARDS = _load_standards()


# ---------- Ollama low-level call ----------

def _generate_once(prompt: str, *, temperature: float, num_predict: int, timeout: int) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": True,
        "keep_alive": "30m",  # 모델을 메모리에 유지해 다음 호출 TTFT 단축
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
            "num_ctx": 32768,
            "top_p": 0.9,
        },
    }
    chunks: List[str] = []
    with requests.post(OLLAMA_URL, headers=HEADERS, json=payload, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                stripped = line.strip()
                if stripped.startswith("data:"):
                    stripped = stripped[5:].strip()
                try:
                    obj = json.loads(stripped)
                except Exception:  # noqa: BLE001
                    continue
            if obj.get("response"):
                chunks.append(obj["response"])
            if obj.get("done"):
                break
    return "".join(chunks).strip()


def _generate(prompt: str, *, temperature: float = 0.2, num_predict: int = -1, timeout: int = 1800) -> str:
    """
    스트리밍 Ollama 호출. CF 524 / 연결 오류 시 최대 5회 재시도 (지수 백오프).
    재시도 사이 짧은 ping으로 모델을 워밍업 유지.
    """
    import time
    max_attempts = 5
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _generate_once(prompt, temperature=temperature, num_predict=num_predict, timeout=timeout)
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            last_exc = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            # 524 (CF 타임아웃), 502/503 (게이트웨이/서비스), 연결 오류는 재시도 가치 있음
            retryable = status in (502, 503, 504, 524) or not isinstance(e, requests.exceptions.HTTPError)
            if not retryable:
                raise AIServiceError(normalize_ai_exception(e, endpoint=OLLAMA_URL)) from e
            if attempt == max_attempts:
                break
            wait = min(2 ** attempt, 30)
            print(f"[agent] _generate 재시도 {attempt}/{max_attempts} (에러 {status or type(e).__name__}, {wait}s 대기)")
            time.sleep(wait)
            # 재시도 전에 작은 ping으로 워밍업 트리거
            try:
                requests.post(
                    OLLAMA_URL,
                    headers=HEADERS,
                    json={"model": OLLAMA_MODEL, "prompt": "ok", "stream": False, "keep_alive": "30m", "options": {"num_predict": 4}},
                    timeout=60,
                )
            except Exception:  # noqa: BLE001
                pass
        except ValueError as e:
            raise AIServiceError(normalize_ai_exception(e, endpoint=OLLAMA_URL)) from e
    if last_exc:
        raise AIServiceError(normalize_ai_exception(last_exc, endpoint=OLLAMA_URL)) from last_exc
    raise RuntimeError("LLM 호출 실패 (알 수 없는 원인)")


# ---------- JSON utilities ----------

def _extract_json(text: str) -> str:
    """모델 응답에서 JSON 블록 추출."""
    text = text.strip()
    # ```json ... ``` 또는 ``` ... ``` 블록 제거
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 첫 { 또는 [ 부터 마지막 } 또는 ] 까지
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        return text
    # 스택 매칭
    stack = []
    end = -1
    for i, ch in enumerate(text[start:], start=start):
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                end = i
                break
    if end > start:
        return text[start : end + 1]
    return text[start:]


def _parse_json(text: str):
    clean = _extract_json(text)
    return json.loads(clean)


def _generate_json(prompt: str, *, temperature: float = 0.15, max_retries: int = 3) -> dict:
    """
    JSON 응답 전용: 파싱 실패 시 모델에 '완전한 JSON으로 다시 출력하라'고 재요청.
    """
    last_err: Optional[Exception] = None
    cur_prompt = prompt
    for attempt in range(1, max_retries + 1):
        response = _generate(cur_prompt, temperature=temperature)
        try:
            return _parse_json(response)
        except json.JSONDecodeError as e:
            last_err = e
            # 잘린 응답을 힌트와 함께 다시 요청
            snippet = response[-800:] if len(response) > 800 else response
            cur_prompt = (
                prompt
                + "\n\n[직전 출력이 JSON 파싱에 실패했다. 파싱 오류: "
                + f"{e.msg} (line {e.lineno}, col {e.colno}). "
                + "반드시 '{' 로 시작해 '}' 로 끝나는 완전한 JSON 한 개만 출력하라. "
                + "잘라먹지 말고 끝까지 작성하라. "
                + "설명/주석/코드펜스 절대 금지. 오직 JSON.]"
            )
    raise last_err if last_err else RuntimeError("알 수 없는 JSON 파싱 실패")


# ---------- 헤더 추출 에이전트 (한 번 호출) ----------

HEADER_SYSTEM = f"""너는 법원경매공고 편집 전문가다. 아래 편집 기준에 따라 공고 원고의 헤더(담당계, 매각일시, 매각결정일시, 매각장소)만 추출한다.

=== 편집 기준 (2023.12.04) ===
{EDITING_STANDARDS}
=== 편집 기준 끝 ===

<출력 스키마>
{{
  "damdang": "경매N계 (담당자이름 있으면 포함)",
  "sale_date": "YYYY. M. D.[요일] HH:MM 형태 (편집 기준의 '매각기일' 표기와 일치)",
  "decision_date": "YYYY. M. D.[요일] HH:MM",
  "location": "법원 및 지원 이름 + 층/호 등"
}}

오직 JSON 한 개만 반환. 설명·코드펜스·주석 금지.
"""


def header_run(raw_text: str) -> dict:
    """원본 원고 텍스트에서 헤더 정보 한 번만 추출."""
    prompt = (
        HEADER_SYSTEM
        + "\n\n=== 원본 원고 (헤더 영역만 살펴봐도 됨) ===\n"
        + raw_text[:4000]
        + "\n\n=== 헤더 JSON ===\n"
    )
    return _generate_json(prompt, temperature=0.1)


# ---------- 사건 단위 에디터 ----------

EDITOR_RECORD_SYSTEM = f"""너는 법원경매공고 편집 전문가다. 아래 편집 기준에 따라 제공된 "단일 사건"의 원고를 구조화된 JSON 레코드(들)로 편집한다. 편집 기준의 모든 조항을 엄격히 적용한다.

=== 편집 기준 (2023.12.04) ===
{EDITING_STANDARDS}
=== 편집 기준 끝 ===

작업 절차:
1. 전달받은 원고 블록은 "하나의 사건번호"에 해당한다. 물건번호가 여러 개일 수 있으므로 각 물건을 별개 레코드로 분리해 출력한다.
2. 편집 기준의 모든 규칙을 적용한다.
3. group 필드는 다음 중 하나로 정확히: "아파트" | "오피스텔" | "연립주택/다세대/빌라" | "단독주택,다가구주택" | "상가/오피스텔,근린시설" | "근린시설" | "대지/임야/전답" | "기타".

<출력 스키마>
{{
  "records": [
    {{
      "case_no": "2024타경XXX",
      "dup_tag": "2024타경YYY[중복] 혹은 빈 문자열",
      "item_no": "1",
      "group": "아파트|오피스텔|연립주택/다세대/빌라|단독주택,다가구주택|상가/오피스텔,근린시설|근린시설|대지/임야/전답|기타",
      "locations": [
        {{"address": "편집기준 적용된 주소+면적 (소재지 주소는 동·층·호 띄어쓰기 유지, 층과 ㎡는 붙여쓰기)", "use": "해당 행의 용도"}}
      ],
      "price": "감정평가액 (숫자·콤마만)",
      "min_price": "최저매각가격 (숫자·콤마만)",
      "note": "편집기준 적용된 비고 (공백 제거·붙여쓰기·문장 끝 마침표 삭제)"
    }}
  ]
}}

오직 JSON 한 개만. 설명·코드펜스·주석 금지. records 배열 안에 물건번호 수만큼 객체가 있다.
"""


def editor_run_record(block_text: str, feedback: str = "") -> dict:
    """단일 사건 원고 블록 → {records: [...]} JSON."""
    parts = [EDITOR_RECORD_SYSTEM]
    if feedback.strip():
        parts.append(
            "\n\n=== 직전 리뷰어 피드백 (반드시 반영해서 이 사건을 재편집) ===\n" + feedback.strip()
        )
    parts.append("\n\n=== 사건 원고 블록 ===\n" + block_text.strip())
    parts.append("\n\n=== 편집본 JSON ===\n")
    return _generate_json("".join(parts), temperature=0.15)


# ---------- 사건 단위 리뷰어 ----------

REVIEWER_RECORD_SYSTEM = f"""너는 법원경매공고 편집 기준 준수 여부를 검증하는 엄격한 편집 검토자다. 제공된 "한 사건의 편집본 JSON"이 편집 기준의 모든 조항을 완벽히 따르는지 검증한다.

=== 편집 기준 (2023.12.04) ===
{EDITING_STANDARDS}
=== 편집 기준 끝 ===

<출력 스키마>
{{
  "pass": true | false,
  "summary": "한줄 요약",
  "issues": [
    {{
      "case_no": "2024타경XXX",
      "field": "records[N].locations[M].address | records[N].note | ...",
      "rule": "위반된 편집 기준 조항 요약",
      "current": "현재 잘못된 값",
      "expected": "편집 기준대로 고쳐야 하는 값",
      "severity": "critical|major|minor"
    }}
  ]
}}

- 위반이 전혀 없으면 pass:true, issues:[]
- 동일한 current/expected 처럼 실질 차이 없는 이슈는 지적하지 말 것
- 오직 JSON 한 개만. 설명 금지.
"""


def reviewer_run_record(record_json: dict, original_block: str = "") -> dict:
    """한 사건의 편집본 JSON → 검증 결과 JSON."""
    parts = [REVIEWER_RECORD_SYSTEM]
    if original_block.strip():
        parts.append("\n\n=== 대조용 원본 원고 ===\n" + original_block.strip())
    parts.append(
        "\n\n=== 편집본 JSON ===\n"
        + json.dumps(record_json, ensure_ascii=False, indent=2)
        + "\n\n=== 검증 결과 JSON ===\n"
    )
    return _generate_json("".join(parts), temperature=0.1)
