"""
Qwen API 단발 호출 클라이언트.

editing_standards.md를 시스템 프롬프트로 주고, 사용자가 붙여넣은 법원원고를
편집 기준에 맞춰 1회 호출로 편집한 결과를 반환한다. (루프 없음)

두 가지 진입점:
  - edit_text(raw_text)        : 자유 텍스트 편집 → 텍스트 반환
  - process_document(raw_text) : 원고 전체 → 구조화 JSON {header, records}
"""

import json
import os
import re
from pathlib import Path
import requests

from ai_errors import AIServiceError, normalize_ai_exception

BASE = Path(__file__).resolve().parent

QWEN_URL = os.environ.get(
    "QWEN_URL", "https://qwen.hyphen.it.com/v1/chat/completions"
)
QWEN_MODEL = os.environ.get("QWEN_MODEL", "mlx-community/Qwen3.6-35B-A3B-4bit")
QWEN_TOKEN = os.environ.get("QWEN_TOKEN", "")
QWEN_MAX_TOKENS = int(os.environ.get("QWEN_MAX_TOKENS", "32768"))
QWEN_TEMPERATURE = float(os.environ.get("QWEN_TEMPERATURE", "0.15"))


def _load_standards() -> str:
    p = BASE / "editing_standards.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


EDITING_STANDARDS = _load_standards()


SYSTEM_PROMPT_TEXT = (
    "너는 법원경매공고 편집 전문가다. 사용자가 입력한 법원 원고(원본 텍스트)를 "
    "아래 <편집 기준>에 따라 편집해서 결과 텍스트만 반환한다. "
    "설명·머리말·코드펜스·해설 절대 금지. 오직 편집된 결과 텍스트만 출력한다.\n\n"
    "<편집 기준>\n"
    + EDITING_STANDARDS
    + "\n</편집 기준>\n"
)


SYSTEM_PROMPT_JSON = (
    "너는 법원경매공고 편집 전문가다. 사용자가 입력한 법원 원고(원본 텍스트 전체)를 "
    "아래 <편집 기준>에 따라 편집한 뒤, 정해진 스키마의 JSON 한 개로 반환한다.\n\n"
    "<편집 기준>\n"
    + EDITING_STANDARDS
    + "\n</편집 기준>\n\n"
    "<출력 스키마>\n"
    "{\n"
    '  "header": {\n'
    '    "damdang": "경매N계 (담당자이름 있으면 포함)",\n'
    '    "sale_date": "YYYY. M. D.[요일] HH:MM",\n'
    '    "decision_date": "YYYY. M. D.[요일] HH:MM",\n'
    '    "location": "법원 및 지원 이름 + 층/호 등"\n'
    "  },\n"
    '  "records": [\n'
    "    {\n"
    '      "case_no": "2024타경XXX",\n'
    '      "dup_tag": "2024타경YYY[중복] 혹은 빈 문자열",\n'
    '      "item_no": "1",\n'
    '      "group": "아파트|오피스텔|연립주택/다세대/빌라|단독주택,다가구주택|상가/오피스텔,근린시설|근린시설|대지/임야/전답|기타",\n'
    '      "locations": [\n'
    '        {"address": "편집기준 적용된 주소+면적 (소재지 주소는 동·층·호 띄어쓰기 유지, 층과 ㎡는 붙여쓰기)", "use": "해당 행의 용도"}\n'
    "      ],\n"
    '      "price": "감정평가액 (숫자·콤마만)",\n'
    '      "min_price": "최저매각가격 (숫자·콤마만)",\n'
    '      "note": "편집기준 적용된 비고 (공백제거·붙여쓰기·문장끝 마침표 삭제)"\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "절대 규칙:\n"
    "- 오직 JSON 한 개만 반환. 코드펜스/설명/주석 금지.\n"
    "- 모든 사건·물건을 records 배열에 빠짐없이 담는다.\n"
    "- group은 위 8개 중 하나로 정확히. 모르면 \"기타\".\n"
    "- 편집 기준의 모든 조항을 records[].note 와 records[].locations[].address 에 적용한다.\n"
)


def _post_qwen(
    messages: list,
    *,
    timeout: int = 600,
    max_tokens: int = QWEN_MAX_TOKENS,
    temperature: float = QWEN_TEMPERATURE,
) -> dict:
    """Qwen API 호출. choices[0].message + finish_reason 포함 dict 반환."""
    payload = {
        "model": QWEN_MODEL,
        "messages": messages,
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {QWEN_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(QWEN_URL, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        raise AIServiceError(normalize_ai_exception(e, endpoint=QWEN_URL)) from e
    choices = data.get("choices") or []
    if not choices:
        return {"content": "", "finish_reason": ""}
    msg = choices[0].get("message") or {}
    return {
        "content": (msg.get("content") or "").strip(),
        "finish_reason": choices[0].get("finish_reason") or "",
    }


def edit_text(raw_text: str, *, timeout: int = 600) -> str:
    """원고 → 편집본. Qwen API를 1회만 호출한다."""
    if not raw_text.strip():
        return ""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_TEXT},
        {"role": "user", "content": raw_text},
    ]
    return _post_qwen(messages, timeout=timeout)["content"]


def _extract_json_block(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def process_document(raw_text: str, *, timeout: int = 1200) -> dict:
    """
    원고 전체 → {header, records} JSON.
    1차 호출 → 응답이 토큰 한도에 걸려 잘렸으면(finish_reason='length' 또는 JSON 파싱 실패),
    동일 컨텍스트에 assistant 응답을 누적해 'continue' 메시지로 한 번에 한해 이어쓰기 요청.
    """
    if not raw_text.strip():
        return {"header": {}, "records": []}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_JSON},
        {"role": "user", "content": raw_text},
    ]
    first = _post_qwen(messages, timeout=timeout)
    accumulated = first["content"]
    finish_reason = first["finish_reason"]

    def _try_parse(text: str):
        block = _extract_json_block(text)
        try:
            return json.loads(block), None
        except json.JSONDecodeError as e:
            return None, e

    parsed, err = _try_parse(accumulated)
    if parsed is None:
        # 잘림 의심 → 이어쓰기 1회 시도
        cont_messages = messages + [
            {"role": "assistant", "content": accumulated},
            {
                "role": "user",
                "content": (
                    "직전 출력이 토큰 한도로 잘렸다. 같은 JSON을 처음부터 다시 만들지 말고, "
                    "마지막 글자에서 곧바로 이어서 출력해 완전한 JSON을 완성해라. "
                    "코드펜스/설명 금지. JSON만 출력."
                ),
            },
        ]
        second = _post_qwen(cont_messages, timeout=timeout)
        cont = second["content"]
        # 모델이 ```json 으로 다시 시작하는 경우 제거
        cont = re.sub(r"^```(?:json)?\s*", "", cont).strip()
        cont = re.sub(r"```\s*$", "", cont).strip()
        merged = accumulated + cont
        parsed, err2 = _try_parse(merged)
        if parsed is None:
            raise RuntimeError(
                f"Qwen JSON 파싱 실패 (이어쓰기 후에도): {err2.msg if err2 else err.msg}. "
                f"finish_reason={finish_reason!r}. 누적 길이={len(merged)}자. "
                f"끝부분: {merged[-300:]!r}"
            )

    if not isinstance(parsed, dict):
        raise RuntimeError("Qwen 응답이 dict 형태가 아닙니다.")
    parsed.setdefault("header", {})
    parsed.setdefault("records", [])
    return parsed
