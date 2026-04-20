# 법원경매공고 원고 편집기

`HWP 5.x` 또는 `HWPX` 형식의 법원경매공고 원고를 읽어서, `법원경매공고 원고 편집 기준 (2023.12.04).pdf`에 맞춘 새 원고를 `HTML`로 생성하는 도구다.

현재 버전의 동작 방식:

- HWP 본문에서 텍스트를 추출한다.
- 사건번호 단위로 항목을 나눈다.
- `소재지 + 상세내역`을 `소재지 및 면적 [㎡]`로 합친다.
- `용도` 칼럼을 별도 칼럼으로 둔다.
- `감정평가액 / 최저매각가격 [단위:원]` 형식으로 가격 칼럼을 렌더링한다.
- 용도별로 묶고, 각 용도 안에서는 사건번호 오름차순으로 정렬한다.

## 실행

```bash
python3 app/court_auction_editor.py 'CS_20250403_20250317150333.hwp' -o output
```

`.hwpx`도 같은 방식으로 넣을 수 있다.

PDF까지 같이 만들려면:

```bash
python3 app/court_auction_editor.py 'CS_20250403_20250317150333.hwp' -o output --pdf
```

생성 파일:

- `output/*.edited.html`
- `output/*.normalized.json`
- `output/*.edited.pdf` (`--pdf` 사용 시)
- `output/*.hwp-friendly.rtf` (`python3 app/render_hwp_friendly_rtf.py ...` 실행 시)
- `output/*.hwp-friendly.docx` (`한글에서 바로 열어 수정하기 좋은 표 기반 작업본`)

## 웹 앱

간단한 업로드형 웹 서버도 같이 쓸 수 있다.

```bash
python3 app/web_app.py --port 8000
```

브라우저에서 `http://127.0.0.1:8000`으로 접속한 뒤 `.hwp` 또는 `.hwpx` 파일을 올리면:

- 편집본 HTML
- 정규화 JSON
- 최종본 HTML
- HWP 친화 작업본 RTF/DOCX

를 한 번에 묶은 `.zip` 파일을 내려준다.

PDF 옵션까지 같이 쓰려면 로컬에 Chrome 계열 브라우저가 설치되어 있어야 한다.

## 주의

- 이 도구는 원본 HWP 바이너리를 직접 수정하지 않는다.
- 대신 신문 공고용으로 다시 편집된 `HTML` 원고를 만든다.
- 일부 문서는 표 구조가 다를 수 있어서, 비고/상세내역 분류는 휴리스틱 기반이다.
- 실제 납품 전에는 생성된 HTML을 열어 한 번 육안 검수하는 게 맞다.

## 다음 단계 후보

- HTML을 바로 PDF로 저장하는 자동화 추가
- 사건번호/물건번호/병합 셀 인식을 더 정확하게 하는 규칙 강화
- 동일 양식의 HWP 여러 건 일괄 변환
