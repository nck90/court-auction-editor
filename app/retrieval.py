#!/usr/bin/env python3
"""임베딩 기반 (+ TF-IDF 폴백) 지식베이스 검색.

지원:
  - Ollama 임베딩 모델이 있으면 사용 (nomic-embed-text 계열)
  - 없으면 TF-IDF 코사인 유사도로 폴백 (numpy/sklearn 없이 수동 구현)
  - 인덱스 파일: knowledge/cache/embeddings.json
  - examples/ 가 수정되면 자동 재인덱싱 (mtime 비교)

사용:
    >>> retrieval.top_k(features, k=3)
    [{"path": "...", "score": 0.83, "preview": "..."}, ...]

features 는 dict (entry 기반) 혹은 미리 만든 문자열.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
KB_DIR = ROOT / "knowledge"
EXAMPLES_DIR = KB_DIR / "examples"
CACHE_DIR = KB_DIR / "cache"
INDEX_PATH = CACHE_DIR / "embeddings.json"
QUERY_CACHE_PATH = CACHE_DIR / "retrieval_query.json"

OLLAMA_BASE = os.environ.get(
    "OLLAMA_BASE", "https://ollama.hyphen.it.com"
).rstrip("/")
OLLAMA_EMBED_URL = f"{OLLAMA_BASE}/api/embeddings"
OLLAMA_TAGS_URL = f"{OLLAMA_BASE}/api/tags"
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "30"))
_UA = os.environ.get("OLLAMA_UA", "curl/8.1.2 (court-auction-learner)")


def _open_url(url: str, *, data: bytes | None = None, method: str = "GET", timeout: int | None = None):
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _UA,
        },
    )
    return urllib.request.urlopen(req, timeout=timeout or OLLAMA_TIMEOUT)

# ---------------------------------------------------------------------------
# Tokenization (한글-친화 캐릭터 n-gram + 공백 토큰)
# ---------------------------------------------------------------------------


_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]+")


def _tokenize(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    # Word-level tokens
    words = [w for w in _NON_WORD.split(text) if w]
    # Character bigrams for 한글 짧은 키워드 매칭
    bigrams: list[str] = []
    for w in words:
        if len(w) >= 2:
            for i in range(len(w) - 1):
                bigrams.append(w[i : i + 2])
    return words + [f"#{b}" for b in bigrams]


# ---------------------------------------------------------------------------
# Embedding provider detection
# ---------------------------------------------------------------------------


def _detect_embedding_model() -> str:
    """Return an available embedding model name, or '' for TF-IDF fallback."""
    if OLLAMA_EMBED_MODEL:
        return OLLAMA_EMBED_MODEL
    try:
        with _open_url(OLLAMA_TAGS_URL, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return ""
    models = [m.get("name", "") for m in data.get("models", [])]
    # Prefer known embedding model names.
    for pattern in (
        "nomic-embed-text",
        "mxbai-embed",
        "all-minilm",
        "bge-",
        "snowflake-arctic-embed",
    ):
        for m in models:
            if pattern in m:
                return m
    return ""


def _call_embed(text: str, model: str) -> list[float] | None:
    try:
        with _open_url(
            OLLAMA_EMBED_URL,
            data=json.dumps({"model": model, "prompt": text}).encode("utf-8"),
            method="POST",
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vec = data.get("embedding")
        if isinstance(vec, list) and vec:
            return [float(x) for x in vec]
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Feature extraction from entry
# ---------------------------------------------------------------------------


def features_to_text(features: Any) -> str:
    """Turn a dict-like feature set into a searchable string."""
    if isinstance(features, str):
        return features
    if not isinstance(features, dict):
        return str(features)
    parts: list[str] = []
    usage = features.get("usage") or ""
    if usage:
        parts.append(f"usage:{usage}")
    props = features.get("properties") or []
    parts.append(f"props:{len(props)}")
    addrs = []
    details_all = []
    for p in props:
        if isinstance(p, dict):
            addrs.append(str(p.get("address", ""))[:80])
            for d in (p.get("details") or []):
                details_all.append(str(d)[:120])
    for a in addrs[:6]:
        parts.append(a)
    for d in details_all[:8]:
        parts.append(d)
    for ln in features.get("note_lines") or []:
        parts.append(str(ln)[:140])
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Index build / load
# ---------------------------------------------------------------------------


def _examples_fingerprint() -> str:
    """Hash of example filenames + mtimes so we re-index on change."""
    if not EXAMPLES_DIR.exists():
        return "empty"
    entries = []
    for fp in sorted(EXAMPLES_DIR.glob("*.md")):
        entries.append(f"{fp.name}:{int(fp.stat().st_mtime)}")
    return hashlib.sha256(("\n".join(entries)).encode("utf-8")).hexdigest()[:16]


def _build_tfidf_index(docs: list[dict]) -> dict:
    """Build a simple TF-IDF index. docs: [{'path':..., 'text':...}]"""
    N = max(1, len(docs))
    df: Counter = Counter()
    tokenized = []
    for d in docs:
        toks = _tokenize(d["text"])
        uniq = set(toks)
        tokenized.append(toks)
        for t in uniq:
            df[t] += 1
    idf: dict[str, float] = {
        t: math.log((N + 1) / (c + 1)) + 1.0 for t, c in df.items()
    }
    vectors: list[dict[str, float]] = []
    for toks in tokenized:
        tf: Counter = Counter(toks)
        vec: dict[str, float] = {}
        for t, f in tf.items():
            vec[t] = (f / max(1, len(toks))) * idf.get(t, 1.0)
        # L2 norm
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        for k in vec:
            vec[k] /= norm
        vectors.append(vec)
    return {
        "mode": "tfidf",
        "idf": idf,
        "docs": docs,
        "vectors": vectors,
    }


def _build_embedding_index(docs: list[dict], model: str) -> dict | None:
    """Query Ollama for each doc and build a dense-vector index."""
    vectors: list[list[float]] = []
    for d in docs:
        v = _call_embed(d["text"], model)
        if v is None:
            return None
        # L2 norm
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        vectors.append([x / norm for x in v])
    return {
        "mode": "embed",
        "model": model,
        "docs": docs,
        "vectors": vectors,
    }


_INDEX: dict | None = None
_INDEX_FP: str = ""


def _collect_docs() -> list[dict]:
    docs: list[dict] = []
    if not EXAMPLES_DIR.exists():
        return docs
    for fp in sorted(EXAMPLES_DIR.glob("*.md")):
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # Use first 4KB for embedding, keep full text as preview source.
        docs.append({
            "path": str(fp.relative_to(ROOT)),
            "name": fp.stem,
            "text": text[:4000],
            "full_len": len(text),
        })
    return docs


def _save_index(index: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Convert sparse vectors (dict) — JSON-safe already. Embedding vectors are lists.
    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )


def _load_index_from_disk() -> dict | None:
    if not INDEX_PATH.exists():
        return None
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ensure_index(force: bool = False) -> dict:
    global _INDEX, _INDEX_FP
    fp = _examples_fingerprint()
    if not force and _INDEX is not None and _INDEX_FP == fp:
        return _INDEX
    # Try load from disk first
    on_disk = _load_index_from_disk()
    if (
        not force
        and on_disk
        and on_disk.get("fingerprint") == fp
        and on_disk.get("mode") in {"tfidf", "embed"}
    ):
        _INDEX = on_disk
        _INDEX_FP = fp
        return _INDEX
    # Rebuild
    docs = _collect_docs()
    if not docs:
        _INDEX = {"mode": "empty", "fingerprint": fp, "docs": [], "vectors": []}
        _INDEX_FP = fp
        _save_index(_INDEX)
        return _INDEX
    model = _detect_embedding_model()
    index: dict | None = None
    if model:
        index = _build_embedding_index(docs, model)
    if index is None:
        index = _build_tfidf_index(docs)
    index["fingerprint"] = fp
    _INDEX = index
    _INDEX_FP = fp
    _save_index(index)
    return index


def reindex() -> dict:
    return _ensure_index(force=True)


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _cosine_sparse(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def _cosine_dense(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


def _tfidf_vectorize(text: str, idf: dict[str, float]) -> dict[str, float]:
    toks = _tokenize(text)
    tf: Counter = Counter(toks)
    vec: dict[str, float] = {}
    for t, f in tf.items():
        vec[t] = (f / max(1, len(toks))) * idf.get(t, 1.0)
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items()}


def _load_query_cache() -> dict:
    if not QUERY_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(QUERY_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_query_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    QUERY_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8"
    )


def top_k(features: Any, k: int = 3) -> list[dict]:
    """Return top-k matching examples.

    Each entry: {'path': str, 'score': float, 'preview': str, 'mode': str}
    """
    index = _ensure_index()
    if index.get("mode") == "empty":
        return []
    text = features_to_text(features)
    if not text:
        return []

    qkey = hashlib.sha256(
        f"{index.get('mode')}:{index.get('fingerprint')}:{text}".encode("utf-8")
    ).hexdigest()[:20]
    qcache = _load_query_cache()
    if qkey in qcache:
        cached = qcache[qkey]
        # Refresh preview in case examples content changed but fingerprint equal.
        return cached[:k]

    results: list[dict] = []
    if index["mode"] == "tfidf":
        qvec = _tfidf_vectorize(text, index["idf"])
        for doc, dvec in zip(index["docs"], index["vectors"]):
            score = _cosine_sparse(qvec, dvec)
            results.append({
                "path": doc["path"],
                "score": score,
                "preview": doc["text"][:600],
                "mode": "tfidf",
            })
    elif index["mode"] == "embed":
        qv = _call_embed(text, index["model"])
        if qv is None:
            # Fallback: rebuild TF-IDF on the fly (one-shot)
            fallback = _build_tfidf_index(index["docs"])
            qvec = _tfidf_vectorize(text, fallback["idf"])
            for doc, dvec in zip(fallback["docs"], fallback["vectors"]):
                score = _cosine_sparse(qvec, dvec)
                results.append({
                    "path": doc["path"],
                    "score": score,
                    "preview": doc["text"][:600],
                    "mode": "tfidf(fallback)",
                })
        else:
            norm = math.sqrt(sum(x * x for x in qv)) or 1.0
            qn = [x / norm for x in qv]
            for doc, dvec in zip(index["docs"], index["vectors"]):
                score = _cosine_dense(qn, dvec)
                results.append({
                    "path": doc["path"],
                    "score": score,
                    "preview": doc["text"][:600],
                    "mode": "embed",
                })
    results.sort(key=lambda r: -r["score"])
    top = results[: max(k * 2, k)]
    qcache[qkey] = top
    _save_query_cache(qcache)
    return top[:k]


def mode() -> str:
    idx = _ensure_index()
    return idx.get("mode", "empty")
