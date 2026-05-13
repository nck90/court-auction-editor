"""AI provider errors -> user-facing messages."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import requests


class AIServiceError(RuntimeError):
    """User-facing AI service failure."""


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _is_local_url(url: str) -> bool:
    host = _host(url)
    return host in {"", "localhost", "127.0.0.1", "::1"}


def _extract_message_from_json(data) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            for key in ("message", "detail", "error"):
                val = err.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        for key in ("message", "detail", "error"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _response_message(response: requests.Response | None) -> str:
    if response is None:
        return ""
    try:
        data = response.json()
    except ValueError:
        data = None
    msg = _extract_message_from_json(data)
    if msg:
        return msg
    text = (response.text or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:400]
    return _extract_message_from_json(parsed) or text[:400]


def user_message_from_http_error(response: requests.Response | None, *, endpoint: str) -> str:
    status = getattr(response, "status_code", None)
    raw = _response_message(response).lower()
    is_local = _is_local_url(endpoint)

    if status == 402 or "more credits" in raw or "insufficient credits" in raw or "quota" in raw:
        if is_local:
            return "로컬 AI 서버의 사용 한도 설정 때문에 요청을 처리하지 못했습니다. 서버 설정을 확인해 주세요."
        return "AI 편집 서버 사용량 한도에 걸렸습니다. 잠시 후 다시 시도해 주세요. 계속되면 서버 관리자에게 확인이 필요합니다."
    if status in (401, 403):
        return "AI 편집 서버 인증에 실패했습니다. 서버 설정을 확인해 주세요."
    if status == 404:
        return "AI 모델 엔드포인트를 찾지 못했습니다. 서버 주소 또는 모델 설정을 확인해 주세요."
    if status == 429:
        return "AI 편집 서버 요청이 몰려 있습니다. 잠시 후 다시 시도해 주세요."
    if status in (502, 503, 504, 524):
        return "AI 편집 서버가 일시적으로 불안정합니다. 잠시 후 다시 시도해 주세요."
    if status and status >= 500:
        return "AI 편집 서버 내부 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    if status and status >= 400:
        return "AI 편집 요청을 처리하지 못했습니다. 서버 설정을 확인해 주세요."
    return "AI 편집 서버 호출에 실패했습니다."


def normalize_ai_exception(exc: Exception, *, endpoint: str) -> str:
    if isinstance(exc, requests.exceptions.HTTPError):
        return user_message_from_http_error(getattr(exc, "response", None), endpoint=endpoint)
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return "AI 응답 시간이 너무 길어 요청이 중단됐습니다. 잠시 후 다시 시도해 주세요."
    if isinstance(exc, requests.exceptions.ConnectionError):
        if _is_local_url(endpoint):
            return "로컬 AI 서버에 연결하지 못했습니다. 서버가 실행 중인지 확인해 주세요."
        return "AI 편집 서버에 연결하지 못했습니다. 네트워크 또는 서버 상태를 확인해 주세요."
    if isinstance(exc, requests.exceptions.Timeout):
        return "AI 요청 시간이 초과됐습니다. 잠시 후 다시 시도해 주세요."
    return str(exc).strip() or "AI 처리 중 오류가 발생했습니다."


def public_error_message(exc: Exception) -> str:
    msg = str(exc).strip()
    return msg or "처리 중 오류가 발생했습니다."
