"""Ollama API client for rule refinement tasks (judgment-heavy edits)."""

import json
import os
import requests
from typing import Dict, List

from ai_errors import AIServiceError, normalize_ai_exception

OLLAMA_URL = os.environ.get("OLLAMA_URL", "https://ollama.hyphen.it.com/api/generate")
# 양자화 Qwen3 Coder 30B MoE (3B active) Q4_K_M 기본 사용.
# 필요 시 환경변수로 교체: OLLAMA_MODEL=qwen2.5-coder:32b 등
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-coder:30b-a3b-q4_K_M")
CF_CLIENT_ID = os.environ.get(
    "CF_ACCESS_CLIENT_ID",
    "77813d1cfde422b9a2e2dfff9bf199cd.access",
)
CF_CLIENT_SECRET = os.environ.get(
    "CF_ACCESS_CLIENT_SECRET",
    "135041cca9bdbf4858e8f0f4213035c132b5034252487655a57c3f1d3c9f4d0f",
)

HEADERS = {
    "Content-Type": "application/json",
    "CF-Access-Client-Id": CF_CLIENT_ID,
    "CF-Access-Client-Secret": CF_CLIENT_SECRET,
}

EDIT_RULES_SYSTEM = """너는 법원경매공고 편집 기준(2023.12.04)에 따라 법원원고의 비고·상세내역을 다듬는 편집자다.
반드시 아래 규칙만 적용해 답한다. 설명 금지. 오직 편집된 텍스트만 반환하라.

<핵심 규칙>
1. 비고는 모든 공백을 제거하여 붙여 쓴다. 문장 사이는 마침표로 구분한다. 마지막 마침표는 제거한다.
2. 농지취득자격증명 제출요(미제출시 ...) 계열 문구는 '농지취득자격증명요'로 축약하고 괄호블록은 삭제한다.
3. '주식회사XXX' → '[주]XXX'. '㈜' 유지.
4. 일괄매각·지분매각은 항상 맨 앞. [중복]/[병합] 사건번호는 그보다도 앞.
5. 상세 날짜(2024.2.8.)·상호명·특정인 이름은 삭제한다. 지분자 이름은 유지.
6. '~바람' → '요'. '~함' 문장 끝은 제거. '하였으나' → ','. '불분명함' → '불명'.
7. 제시외 건물 면적들은 모두 더해서 대표건물(글자수 가장 짧은 것)+등+합계㎡로 요약한다.
8. 건축자재(철골조·철근콘크리트구조·일반철골구조·슬래브지붕·판넬조·샌드위치패널 등)는 삭제한다.
9. 제1종·제2종 근린생활시설 → 근린시설.
10. 지분: '전 소유권 중 갑구N번 XXX Y분의 Z 지분전부/일부' → '[전소유권중갑구N번XXXZ/Y지분전부]'. 대괄호·붙여쓰기.
11. 연속 동일 면적 층수: '1층73.88㎡ 2층73.88㎡' → '1,2층각73.88㎡'.

<예시>
입력: 일괄매각. 목록4 분묘소재. 농지취득자격증명 제출요(미제출시 보증금 미반환). 공유자우선매수권 행사에 관한 특별매각조건 있음.
출력: 일괄매각.목록4분묘소재.농지취득자격증명요.공유자우선매수권행사에관한특별매각조건있음

입력: 일괄매각, 목록 12, 15 지분매각. 주식회사 범창종합건설이 2024.2.8. 유치권 신고를 하였으나 그 성립여부는 불분명함.
출력: 일괄매각.목록12,15지분매각.[주]범창종합건설유치권신고,성립여부불명

입력: 일괄매각. 제시외 건물 포함. 매각물건명세서, 부동산현황조사보고서, 감정평가서 등을 통하여 철저한 사전 조사 후 입찰자 본인의 책임하에 입찰 요망.
출력: 일괄매각.제시외건물포함.매각물건명세서등참고후입찰요망

입력(지분): (전 소유권 중 갑구 1-1번 박철호 43분의 10 지분전부)
출력(지분): [전소유권중갑구1-1번박철호10/43지분전부]

입력(지분): (공유자 주식회사 대운주택 지분 107분의 67 전부)
출력(지분): [전소유권중갑구1번[주]대운주택67/107지분전부]

입력(제시외): 제시외 -다용도실 단층 10㎡ -창고 단층 13.8㎡ -창고 단층 18㎡ -창고 단층 11.5㎡
출력(제시외): 제시외 창고등53.3㎡
"""


def ask_ollama(prompt: str, timeout: int = 120) -> str:
    """Ollama /api/generate 호출. 응답 문자열(response 필드)만 반환."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 512,
        },
    }
    try:
        r = requests.post(OLLAMA_URL, headers=HEADERS, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        raise AIServiceError(normalize_ai_exception(e, endpoint=OLLAMA_URL)) from e
    return (data.get("response") or "").strip()


def refine_note(raw_note: str, rec_summary: str = "") -> str:
    """비고 하나를 규칙대로 다듬어 반환. 실패 시 원문을 그대로 돌려준다."""
    if not raw_note.strip():
        return ""
    prompt = EDIT_RULES_SYSTEM + "\n\n<사건 요약>\n" + rec_summary + "\n\n<원본 비고>\n" + raw_note + "\n\n<편집 후 비고>\n"
    try:
        out = ask_ollama(prompt)
        # 모델이 코드블록으로 감싸면 제거
        out = out.strip().strip("`").strip()
        # 여러 줄이면 첫 줄만 쓸지? 실무상 비고는 하나의 문자열이어야 하므로 \n → '.'로 치환
        out = out.replace("\n", ".")
        out = out.replace("  ", "")
        return out or raw_note
    except Exception as e:  # noqa: BLE001
        print(f"[llm] refine_note 실패: {e}")
        return raw_note


def refine_detail(raw_detail: str, yongdo: str = "") -> str:
    """상세내역(소재지 + 면적) 하나를 편집 기준대로 다듬는다."""
    if not raw_detail.strip():
        return ""
    prompt = EDIT_RULES_SYSTEM + "\n\n<사건 용도>\n" + yongdo + "\n\n<원본 상세내역>\n" + raw_detail + "\n\n<편집 후 상세내역>\n"
    try:
        out = ask_ollama(prompt)
        out = out.strip().strip("`").strip()
        return out or raw_detail
    except Exception as e:  # noqa: BLE001
        print(f"[llm] refine_detail 실패: {e}")
        return raw_detail
