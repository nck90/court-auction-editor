# 학습 루프 아키텍처 (context engineering loop)

> **중요:** 여기 구현된 것은 강화학습(RL)이 아니다. 모델 파라미터 업데이트가 없고
> gradient도 흐르지 않는다. 아래 요소들을 조합해서 "사용할수록 정확도가 올라가는"
> 효과를 만든다:
>
> 1. **지식베이스 성장** — 고득점 케이스를 `examples/` 에 자동 승격
> 2. **더 나은 예시 검색** — 임베딩(가능하면) 또는 TF-IDF 로 top-K 관련 예시 주입
> 3. **실패 패턴 증류** — corrections 를 LLM 으로 모아 `lessons.md` 로 정제
> 4. **점수 추적** — 모든 실행의 점수를 `runs.jsonl` 에 기록해서 퇴행 감지

## 데이터 파일 (`/Users/bagjun-won/t/knowledge/`)

| 파일 | 용도 | 쓰는 모듈 |
|------|------|----------|
| `rules.md` | 수동 편집 규칙 (사람이 작성) | `llm_refiner` |
| `examples/*.md` | 입력 → 편집본 예시 | `retrieval`, `llm_refiner` |
| `runs.jsonl` | 실행 기록 (score, mismatches, hashes) | `memory`, `scorer`, `tools/learning_report.py` |
| `corrections.jsonl` | 사용자 👎 + 수정 기록 | `memory`, `app/web_app.py /feedback` |
| `promotions.jsonl` | examples/ 로 승격한 케이스 idempotency | `memory.promote_to_example` |
| `lessons.md` | corrections 에서 증류한 규칙 | `memory.distill_lessons`, `tools/distill.py` |
| `cache/llm_cache.json` | LLM 응답 캐시 | `llm_refiner` |
| `cache/embeddings.json` | retrieval 인덱스 | `retrieval` |
| `cache/retrieval_query.json` | 쿼리 캐시 | `retrieval` |
| `cache/last_distill.json` | distill 쿨다운 타임스탬프 | `memory` |
| `report.md` | 학습 루프 상태 리포트 | `tools/learning_report.py` |

## 구성 요소 흐름

```
┌─────────────┐      ┌─────────────┐      ┌──────────────┐
│  업로드     │ ───> │ render_     │ ───> │ llm_refiner  │
│  (웹/CLI)   │      │ final_notice│      │  .refine()   │
└─────────────┘      └─────────────┘      └──────┬───────┘
                                                  │
                                    heuristics ───┤
                                    LLM call  ────┤
                                                  │
        ┌─────────────────────────────────────────┘
        │ refine() 내부에서:
        │   - retrieval.top_k(features) → 예시 주입
        │   - memory.lessons_snippet() → 교훈 주입
        │   - memory.load_corrections() → 회피 컨텍스트
        ▼
┌─────────────┐      ┌──────────────┐       ┌──────────────┐
│ 사용자 UI   │ ───> │ scorer.score │ ────> │ memory.      │
│ 👍/👎 +수정 │      │  _case()     │       │  log_run()   │
└─────┬───────┘      └──────────────┘       └──────┬───────┘
      │                                             │
      ▼                                             ▼
┌─────────────┐                             ┌──────────────┐
│ corrections │<─── distill_lessons ───────>│ lessons.md   │
│    .jsonl   │     (tools/distill.py)      └──────────────┘
└─────┬───────┘                                     
      │                                             
      ▼                                             
┌─────────────┐                             ┌──────────────┐
│ promotions  │<─── promote_to_example ────>│ examples/    │
│   .jsonl    │     (score >= 0.9)          │ auto_*.md    │
└─────────────┘                             └──────┬───────┘
                                                    │
                                                    ▼
                                            retrieval 재인덱싱
                                            (mtime 변경 감지)
```

## 임베딩 경로

1. `retrieval._detect_embedding_model()` 이 `/api/tags` 를 조회해서 `nomic-embed-text`,
   `mxbai-embed`, `all-minilm`, `bge-`, `snowflake-arctic-embed` 계열을 찾는다.
2. 발견되면 `api/embeddings` 로 각 예시를 벡터화해서 `embeddings.json` 저장.
3. 발견되지 않으면 **TF-IDF 폴백** — 한글 바이그램 + 단어 토큰 + IDF + L2 norm.
4. `examples/` mtime fingerprint 가 바뀌면 자동 재인덱싱.

> **현 서버 상태:** `ollama.hyphen.it.com` 에서 임베딩 모델 미발견 → TF-IDF 모드로 동작.

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `USE_LLM_REFINER` | `1` | 0 으로 두면 rule-based only |
| `OLLAMA_URL` | `https://ollama.hyphen.it.com/api/generate` | 생성 엔드포인트 |
| `OLLAMA_BASE` | `https://ollama.hyphen.it.com` | 임베딩/태그 베이스 |
| `OLLAMA_MODEL` | `gemma4:26b` | 생성 모델 |
| `OLLAMA_EMBED_MODEL` | (자동 감지) | 수동 지정 시 override |
| `OLLAMA_TIMEOUT` | `60` | 초 |
| `LLM_LOOP_PROMOTE_THRESHOLD` | `0.9` | 승격 최소 점수 |
| `LLM_LOOP_DISTILL_MIN` | `20` | 증류 시작 최소 correction 수 |
| `LLM_LOOP_DISTILL_COOLDOWN` | `1800` | 증류 쿨다운(초) |

## 알려진 한계

- **RL 아님.** 모델이 학습되는 것이 아니라 프롬프트 컨텍스트가 성장하는 구조.
  장기적으로는 예시/레슨 수가 늘면 토큰 비용이 선형 증가하고, 검색 품질이 저하될 수 있다.
- **TF-IDF 폴백의 부정확성.** 한글 바이그램 기반이라 숫자/주소가 많은 입력에는 잘 작동하지만,
  의미적으로 비슷한 용도(근린시설 vs 상가,오피스텔 등)을 완벽히 묶지는 못한다.
  Ollama 에 임베딩 모델이 배포되면 자동으로 사용한다.
- **LLM refiner 의 위험성.** `gemma4:26b` 가 잘못된 JSON 을 주면 캐시에 negative 로 저장되고
  rule-based 결과로 폴백한다. 하지만 간헐적으로 레이턴시가 커질 수 있다.
- **증류의 제한적 범위.** distill 은 마지막 200 corrections 만 보고, 쿨다운 30 분.
  거대한 correction 세트에서 드문 패턴은 놓칠 수 있다.
- **스코어링은 근사치.** PDF 기반 스코어는 셀 단위 정확 매칭이 아니라 fuzzy match.
  True/false 라기보다 퇴행 감지용 신호로 이해해야 한다.
- **Fine-tune 없음.** 모델 가중치는 그대로이므로 `gemma4:26b` 의 한국어 품질이 상한선.

## 재생산 방법

```bash
# 1. 파이프라인 정상 작동 확인
python3 tools/diff_ui3gye.py           # Total compared: 20, mismatches: 0
python3 run_batch_test.py               # 10 케이스 OK

# 2. 학습 루프 상태 보기
python3 tools/learning_report.py        # report.md 생성

# 3. 증류 수동 트리거
python3 tools/distill.py --force

# 4. 검색 재인덱싱 (examples/ 수정 후)
python3 -c "import sys; sys.path.insert(0,'app'); import retrieval; retrieval.reindex()"
```
