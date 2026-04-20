#!/usr/bin/env python3
"""법원경매공고 웹 편집기 (Flask 버전).

화면:
  GET  /                          → 업로드 폼
  POST /convert                   → 업로드를 파이프라인에 태우고 결과로 리디렉트
  GET  /jobs/<id>                 → 결과(파일 링크, 인라인 PDF 미리보기)
  GET  /jobs/<id>/files/<name>    → 단일 파일 서빙(?dl=1 이면 attachment)
  GET  /jobs/<id>/download        → 전체 ZIP 다운로드

옵션으로 `.indd`를 같이 업로드하면 InDesign 2026으로 조판 PDF도 export.
"""

from __future__ import annotations

import argparse
import io
import mimetypes
import os
import threading
import time
import traceback
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    url_for,
)

from court_auction_editor import build_document, html_to_pdf
from render_final_notice import load_entries, render_html as render_final_html, render_pdf as render_final_pdf, format_entry
from render_hwp_friendly_rtf import render_docx
from render_xlsx import render_xlsx
from render_xlsx_designed import render_xlsx_designed

from batch_process import run_indesign_export

try:
    import memory
    import scorer
except ImportError:
    memory = None  # type: ignore
    scorer = None  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
JOBS_ROOT = ROOT / "output" / "web"
SOURCE_EXTS = {".hwp", ".hwpx"}
INDD_EXT = ".indd"


@dataclass
class JobRecord:
    job_id: str
    folder: Path
    status: str = "pending"
    source_name: str = ""
    outputs: list[Path] = field(default_factory=list)
    indesign_pdf: Path | None = None
    error: str = ""
    details: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    doc: dict | None = None
    rendered: list[dict] = field(default_factory=list)
    case_id: str = ""


JOBS: dict[str, JobRecord] = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------


def normalize_stem(filename: str) -> str:
    stem = Path(filename).stem or "upload"
    stem = unicodedata.normalize("NFKC", stem).strip()
    cleaned: list[str] = []
    for ch in stem:
        if ch.isalnum() or ch in {"-", "_", "."}:
            cleaned.append(ch)
        elif ch.isspace():
            cleaned.append("_")
    result = "".join(cleaned).strip("._")
    return result or "upload"


@dataclass
class UploadedFile:
    filename: str
    content: bytes


# ---------------------------------------------------------------------------
# 파이프라인
# ---------------------------------------------------------------------------


def run_pipeline(
    job: JobRecord,
    hwp_file: UploadedFile,
    indd_file: UploadedFile | None,
    include_indesign: bool,
) -> None:
    def step(msg: str) -> None:
        job.details.append(msg)

    try:
        safe_stem = normalize_stem(hwp_file.filename)
        folder = job.folder
        folder.mkdir(parents=True, exist_ok=True)

        source_path = folder / f"{safe_stem}{Path(hwp_file.filename).suffix or '.hwp'}"
        source_path.write_bytes(hwp_file.content)
        job.source_name = hwp_file.filename
        step("파일 업로드 완료")

        step("내용을 분석하고 있습니다")
        edited_html_path, json_path = build_document(source_path, folder)
        doc = load_entries(json_path)
        job.doc = doc
        job.case_id = safe_stem
        n_entries = len(doc.get("entries", []))
        # Snapshot rendered entries for feedback UI.
        try:
            job.rendered = [
                format_entry(e)
                for e in doc.get("entries", [])
                if e.get("usage") not in {"자동차", "선박", "건설기계", "항공기"}
            ]
        except Exception:
            job.rendered = []
        # Score + log run (no reference PDF here, structural heuristic).
        if scorer is not None and memory is not None:
            try:
                scorer.score_case(
                    case_id=job.case_id,
                    pipeline_output=job.rendered,
                    entries_raw=doc.get("entries", []),
                )
            except Exception:
                pass

        song_docx = folder / f"{safe_stem}-송.docx"
        render_docx(doc, song_docx)

        step("편집 기준을 적용하고 있습니다")
        final_html = folder / f"{safe_stem}.final.html"
        final_html.write_text(render_final_html(doc), encoding="utf-8")

        step("PDF를 만들고 있습니다")
        edited_pdf = folder / f"{safe_stem}.편집본.pdf"
        html_to_pdf(edited_html_path, edited_pdf)

        final_pdf = folder / f"{safe_stem}.최종.pdf"
        render_final_pdf(final_html, final_pdf)

        final_xlsx = folder / f"{safe_stem}.최종.xlsx"
        try:
            render_xlsx(doc, final_xlsx)
        except Exception:
            final_xlsx = None

        designed_xlsx = folder / f"{safe_stem}.최종.디자인.xlsx"
        try:
            render_xlsx_designed(doc, designed_xlsx)
        except Exception:
            designed_xlsx = None
        step("변환 완료")

        job.outputs = [
            song_docx,
            edited_pdf,
            final_pdf,
            *([final_xlsx] if final_xlsx else []),
            *([designed_xlsx] if designed_xlsx else []),
            edited_html_path,
            final_html,
            json_path,
        ]

        if include_indesign and indd_file is not None:
            indd_path = folder / f"{safe_stem}.source.indd"
            indd_path.write_bytes(indd_file.content)
            indesign_pdf = folder / f"{safe_stem}.InDesign조판.pdf"
            try:
                run_indesign_export(indd_path, indesign_pdf)
                job.indesign_pdf = indesign_pdf
                job.outputs.append(indesign_pdf)
                job.details.append(f"InDesign export: {indd_file.filename}")
            except Exception as exc:  # noqa: BLE001
                job.details.append(f"InDesign export 실패: {exc}")
        elif include_indesign and indd_file is None:
            job.details.append("InDesign 옵션은 체크됐지만 .indd 업로드가 없어서 skip.")

        job.status = "ok"
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.details.append(traceback.format_exc(limit=3))
    finally:
        job.ended_at = time.time()


# ---------------------------------------------------------------------------
# 템플릿
# ---------------------------------------------------------------------------


BASE_CSS = """
:root {
  --bg: #f6f0e5;
  --panel: #fffdf8;
  --ink: #1f1b16;
  --muted: #6f5f49;
  --accent: #b74d1b;
  --accent-soft: #f6dcc6;
  --border: #d8c4aa;
  --ok: #236b36;
  --error: #9f1d1d;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
  background:
    radial-gradient(circle at top left, #ffe3c7 0, transparent 32%),
    linear-gradient(180deg, var(--bg), #efe4d2);
  color: var(--ink);
  min-height: 100vh;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 860px; margin: 60px auto; padding: 0 20px; }
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 36px;
  box-shadow: 0 18px 40px rgba(77, 49, 24, 0.08);
}
.panel + .panel { margin-top: 20px; }
h1 { margin: 0 0 10px; font-size: 30px; letter-spacing: -0.01em; }
h2 { margin: 0 0 12px; font-size: 18px; }
p { margin: 0 0 12px; line-height: 1.6; color: var(--muted); }
p.lead { color: var(--ink); font-size: 16px; margin-bottom: 26px; }
form { display: grid; gap: 16px; margin-top: 10px; }
label.field { display: grid; gap: 8px; font-size: 14.5px; font-weight: 600; color: var(--ink); }
.upload-zone {
  position: relative;
  padding: 32px 20px;
  border: 2px dashed var(--border);
  border-radius: 16px;
  background: #fff;
  text-align: center;
  transition: border-color 0.15s, background 0.15s;
}
.upload-zone:hover { border-color: var(--accent); background: #fffaf1; }
.upload-zone input[type="file"] {
  position: absolute; inset: 0; opacity: 0; cursor: pointer;
}
.upload-zone .upload-icon { font-size: 28px; margin-bottom: 6px; }
.upload-zone .upload-label { font-size: 15px; font-weight: 600; color: var(--ink); }
.upload-zone .upload-hint { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
.upload-zone.has-file { border-style: solid; border-color: var(--accent); background: #fffaf1; }
button.primary {
  border: 0;
  border-radius: 999px;
  padding: 16px 28px;
  background: var(--accent);
  color: #fff;
  font-size: 16px;
  font-weight: 700;
  cursor: pointer;
  transition: opacity 0.15s;
}
button.primary:hover { opacity: 0.92; }
button.primary:disabled { opacity: 0.55; cursor: progress; }
.status { margin: 0 0 14px; padding: 14px 16px; border-radius: 12px; font-size: 14.5px; }
.status.ok { background: #ecf8ee; color: var(--ok); }
.status.error { background: #fdeeee; color: var(--error); }
.status.info { background: var(--accent-soft); color: #6a4018; }
.pdf-list { list-style: none; padding: 0; margin: 0 0 20px; display: grid; gap: 10px; }
.pdf-list li {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border: 1px solid var(--border);
  border-radius: 12px; background: #fffaf1; font-size: 14.5px;
}
.pdf-list .doc-name { font-weight: 600; color: var(--ink); }
.pdf-list .doc-actions { display: flex; gap: 8px; }
.pdf-list .doc-actions a {
  padding: 8px 14px; border-radius: 999px;
  border: 1px solid var(--accent); color: var(--accent);
  font-size: 13px; font-weight: 600;
}
.pdf-list .doc-actions a:hover { background: var(--accent); color: #fff; text-decoration: none; }
iframe.preview {
  width: 100%; height: 820px; border: 1px solid var(--border); border-radius: 14px;
  background: #fff;
}
.action-row { display: flex; gap: 10px; align-items: center; margin-top: 4px; }
.action-row a.secondary {
  padding: 10px 18px; border-radius: 999px;
  border: 1px solid var(--border); color: var(--muted);
  font-size: 13.5px; font-weight: 600;
}
.action-row a.secondary:hover { border-color: var(--accent); color: var(--accent); text-decoration: none; }
.small-hint { font-size: 13px; color: var(--muted); margin-top: 6px; }
.progress-message { font-size: 15.5px; color: var(--ink); margin-top: 6px; }
"""


BASE_LAYOUT = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>{{ base_css|safe }}</style>
</head><body><div class="wrap">{{ body|safe }}</div></body></html>
"""


UPLOAD_FORM = """
<div class="panel">
  <h1>법원경매공고 편집기</h1>
  <p class="lead">원본 파일을 업로드하시면 편집기준에 맞춘 PDF를 자동으로 만들어 드립니다.</p>
  {% if error %}<p class="status error">{{ error }}</p>{% endif %}
  <form method="post" action="{{ url_for('convert') }}" enctype="multipart/form-data" id="upload-form">
    <label class="field">원본 파일
      <div class="upload-zone" id="drop-zone">
        <div class="upload-icon">📄</div>
        <div class="upload-label" id="file-label">파일을 선택하거나 여기로 끌어다 놓으세요</div>
        <div class="upload-hint">지원 형식: .hwp, .hwpx</div>
        <input type="file" name="hwp" accept=".hwp,.hwpx" required id="file-input">
      </div>
    </label>
    <button type="submit" class="primary" id="submit-btn">변환 시작</button>
  </form>
</div>
<script>
(function(){
  var input = document.getElementById('file-input');
  var zone  = document.getElementById('drop-zone');
  var label = document.getElementById('file-label');
  var btn   = document.getElementById('submit-btn');
  var form  = document.getElementById('upload-form');
  input.addEventListener('change', function(){
    if (input.files && input.files[0]) {
      label.textContent = input.files[0].name;
      zone.classList.add('has-file');
    }
  });
  form.addEventListener('submit', function(){
    btn.disabled = true;
    btn.textContent = '업로드 중...';
  });
})();
</script>
"""


JOB_VIEW = """
{% if job.status == 'running' %}
<script>setTimeout(function(){location.reload();}, 2000);</script>
<style>
.spinner{display:inline-block;width:22px;height:22px;border:3px solid #f3dbbf;border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:-5px;margin-right:12px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
<div class="panel">
  <h1><span class="spinner"></span>변환 중입니다</h1>
  <p class="lead">원본: {{ job.source_name or '업로드 파일' }}</p>
  <p class="status info">잠시만 기다려 주세요. 화면이 자동으로 새로 고쳐집니다.</p>
  {% if progress_message %}<p class="progress-message">{{ progress_message }}</p>{% endif %}
</div>
{% else %}
<div class="panel">
  {% if job.status == 'ok' %}
    <h1>변환이 완료되었습니다</h1>
    <p class="lead">원본: {{ job.source_name or '-' }}</p>
  {% else %}
    <h1>변환에 실패했습니다</h1>
    <p class="status error">문제가 발생했습니다. 파일을 다시 확인하시거나 관리자에게 문의해 주세요.</p>
  {% endif %}
  {% if pdf_items %}
    <h2>생성된 PDF</h2>
    <ul class="pdf-list">
    {% for item in pdf_items %}
      <li>
        <span class="doc-name">{{ item.label }}</span>
        <span class="doc-actions">
          <a href="{{ item.href }}" target="_blank">열기</a>
          <a href="{{ item.href }}?dl=1">다운로드</a>
        </span>
      </li>
    {% endfor %}
    </ul>
  {% endif %}
  <div class="action-row">
    <a class="secondary" href="{{ url_for('index') }}">새 파일 변환하기</a>
  </div>
</div>
{% if preview %}
<div class="panel">
  <h2>미리보기</h2>
  <iframe class="preview" src="{{ preview.href }}" title="PDF 미리보기"></iframe>
</div>
{% endif %}
{% endif %}
"""


def render_page(title: str, body_template: str, **ctx) -> str:
    body = render_template_string(body_template, **ctx)
    return render_template_string(BASE_LAYOUT, title=title, base_css=BASE_CSS, body=body)


# ---------------------------------------------------------------------------
# Flask 앱
# ---------------------------------------------------------------------------


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB


@app.get("/")
def index():
    return render_page("법원경매공고 자동화", UPLOAD_FORM, error="")


@app.post("/convert")
def convert():
    hwp = request.files.get("hwp")
    if hwp is None or not hwp.filename:
        return render_page("법원경매공고 자동화", UPLOAD_FORM, error="HWP/HWPX 파일이 없습니다."), 400
    if Path(hwp.filename).suffix.lower() not in SOURCE_EXTS:
        return render_page("법원경매공고 자동화", UPLOAD_FORM, error=".hwp 또는 .hwpx 파일만 올릴 수 있습니다."), 400

    hwp_bytes = hwp.read()
    if not hwp_bytes:
        return render_page("법원경매공고 자동화", UPLOAD_FORM, error="업로드된 HWP 파일이 비어 있습니다."), 400

    use_indesign = request.form.get("use_indesign") == "1"
    indd_storage = request.files.get("indd")
    indd_upload: UploadedFile | None = None
    if indd_storage and indd_storage.filename:
        if Path(indd_storage.filename).suffix.lower() != INDD_EXT:
            return render_page("법원경매공고 자동화", UPLOAD_FORM, error="소스 템플릿은 .indd 파일이어야 합니다."), 400
        indd_bytes = indd_storage.read()
        if indd_bytes:
            indd_upload = UploadedFile(filename=indd_storage.filename, content=indd_bytes)

    hwp_upload = UploadedFile(filename=hwp.filename, content=hwp_bytes)

    job_id = uuid.uuid4().hex[:10]
    folder = JOBS_ROOT / job_id
    folder.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        job = JobRecord(job_id=job_id, folder=folder, status="running")
        JOBS[job_id] = job

    worker = threading.Thread(
        target=run_pipeline,
        args=(job, hwp_upload, indd_upload, use_indesign),
        daemon=True,
    )
    worker.start()
    return redirect(url_for("job_view", job_id=job_id), code=303)


PDF_LABEL_MAP = [
    ("최종", "최종 편집본 PDF"),
    ("편집본", "1차 편집본 PDF"),
    ("InDesign", "InDesign 조판 PDF"),
]


def _pdf_label(name: str) -> str:
    for key, label in PDF_LABEL_MAP:
        if key in name:
            return label
    return "PDF"


@app.get("/jobs/<job_id>")
def job_view(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        abort(404, description="잡을 찾을 수 없습니다")

    status_class = {"ok": "ok", "error": "error"}.get(job.status, "info")
    pdf_items = []
    for path in job.outputs:
        if not path.exists() or path.suffix.lower() != ".pdf":
            continue
        pdf_items.append({
            "name": path.name,
            "label": _pdf_label(path.name),
            "href": url_for("job_file", job_id=job.job_id, name=path.name),
        })

    preview = None
    primary_pdf = next(
        (
            p for p in job.outputs
            if p.suffix.lower() == ".pdf" and ("최종" in p.name or "InDesign" in p.name)
        ),
        None,
    )
    if primary_pdf and primary_pdf.exists():
        preview = {
            "name": primary_pdf.name,
            "href": url_for("job_file", job_id=job.job_id, name=primary_pdf.name),
        }

    progress_message = ""
    if job.status == "running" and job.details:
        progress_message = job.details[-1]

    return render_page(
        f"변환 결과 · {job.source_name or job.job_id}",
        JOB_VIEW,
        job=job,
        status_class=status_class,
        pdf_items=pdf_items,
        preview=preview,
        progress_message=progress_message,
    )


def _build_feedback_rows(job: JobRecord) -> list[dict]:
    rows = []
    for entry_idx, r in enumerate(job.rendered or []):
        case_nums = (r.get("case") or "").splitlines()
        primary = case_nums[0] if case_nums else f"entry{entry_idx}"
        item = r.get("item") or ""
        locs = r.get("locations") or []
        usages = r.get("usages") or []
        for i, loc in enumerate(locs):
            key = f"{primary}|item{item}|location|{i}"
            rows.append({
                "key": key, "case": primary, "item": item,
                "field": "location", "row": i, "value": loc,
            })
        for i, ug in enumerate(usages):
            key = f"{primary}|item{item}|usage|{i}"
            rows.append({
                "key": key, "case": primary, "item": item,
                "field": "usage", "row": i, "value": ug,
            })
        note = r.get("note") or ""
        key = f"{primary}|item{item}|note|0"
        rows.append({
            "key": key, "case": primary, "item": item,
            "field": "note", "row": 0, "value": note,
        })
    return rows


@app.get("/jobs/<job_id>/files/<path:name>")
def job_file(job_id: str, name: str):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    target = (job.folder / name).resolve()
    try:
        target.relative_to(job.folder.resolve())
    except ValueError:
        abort(403)
    if not target.is_file():
        abort(404)
    as_attachment = request.args.get("dl") == "1"
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return send_file(target, mimetype=mime, as_attachment=as_attachment, download_name=target.name)


@app.post("/feedback")
def feedback():
    if memory is None:
        return jsonify({"ok": False, "error": "memory module unavailable"}), 500
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id", "")
    cell_key = data.get("cell_key") or ""
    verdict = (data.get("verdict") or "").lower()
    before = data.get("before") or ""
    after = data.get("after") or ""
    reason = data.get("reason") or ""
    job = JOBS.get(job_id)
    case_id = (job.case_id if job else "") or data.get("case_id") or "unknown"
    try:
        if verdict == "down" and after:
            memory.record_correction(
                case_id=case_id,
                cell_key=cell_key,
                before=before,
                after=after,
                reason=reason,
                input_data={"job_id": job_id},
            )
        # Always log a cell-level score for visibility.
        if scorer is not None and job is not None:
            try:
                scorer.score_case(
                    case_id=case_id,
                    pipeline_output=job.rendered,
                    user_feedback=[{
                        "cell_key": cell_key,
                        "verdict": verdict,
                        "before": before,
                        "fix": after,
                    }],
                )
            except Exception:
                pass
        # High-score promotion: if verdict=up and we have a full-entry consensus signal,
        # we don't know entry-level score from a single cell. Promote heuristically:
        # if a job has mostly up-votes (>= 0.9) for its rendered entries and not yet promoted.
        if job is not None and verdict == "up":
            try:
                _maybe_promote_from_job(job)
            except Exception:
                pass
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


def _maybe_promote_from_job(job: JobRecord) -> None:
    """Count votes recorded for this job in runs.jsonl; promote if >= 0.9 threshold."""
    if memory is None or not job.rendered or not job.case_id:
        return
    # Re-read runs.jsonl, find feedback entries for this case since job started.
    runs = memory.load_runs()
    up = 0
    down = 0
    for r in runs:
        if r.get("case_id") != job.case_id:
            continue
        if r.get("source") != "feedback":
            continue
        for cs in r.get("cell_scores") or []:
            if cs.get("score", 0) >= 1.0:
                up += 1
            else:
                down += 1
    total = up + down
    if total < 5:
        return
    score = up / total
    if score < memory.PROMOTE_THRESHOLD:
        return
    entries_raw = (job.doc or {}).get("entries", []) if job.doc else []
    entries_filtered = [
        e for e in entries_raw
        if e.get("usage") not in {"자동차", "선박", "건설기계", "항공기"}
    ]
    memory.promote_to_example(
        case_id=job.case_id,
        score=score,
        entries=entries_filtered,
        rendered_entries=job.rendered,
        source="web_feedback",
        note=f"👍 {up}/{total} 셀",
    )


@app.post("/admin/distill")
def admin_distill():
    if memory is None:
        return jsonify({"ok": False, "error": "memory module unavailable"}), 500
    force = request.args.get("force", "0") == "1"
    try:
        result = memory.distill_lessons(force=force)
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/admin/status")
def admin_status():
    if memory is None:
        return jsonify({"ok": False, "error": "memory module unavailable"}), 500
    try:
        from retrieval import mode as retrieval_mode
    except Exception:
        retrieval_mode = lambda: "n/a"  # type: ignore
    try:
        runs = memory.load_runs()
        corrections = memory.load_corrections()
        return jsonify({
            "ok": True,
            "runs": len(runs),
            "corrections": len(corrections),
            "retrieval_mode": retrieval_mode(),
            "llm_enabled": os.environ.get("USE_LLM_REFINER", "1") not in {"0", "", "false", "False"},
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/jobs/<job_id>/download")
def download_zip(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    if not job.outputs:
        abort(404, description="다운로드할 파일이 없습니다")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in job.outputs:
            if path.exists():
                archive.write(path, arcname=path.name)
    buf.seek(0)
    stem = normalize_stem(job.source_name or job.job_id)
    filename_star = quote(f"{stem}.zip")
    resp = Response(buf.getvalue(), mimetype="application/zip")
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{filename_star}"
    return resp


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="법원경매공고 업로드 기반 웹 편집기 (Flask)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    print(f"Listening on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
