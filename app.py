"""
법원 경매공고 편집기 — Flask 엔트리포인트 (AI 에디터↔리뷰어 루프 + SSE 스트리밍).

플로우:
  1. POST /upload → 파일 저장 → /process/<job_id> 리다이렉트
  2. GET /process/<job_id> → 스트리밍 UI 렌더
  3. GET /stream/<job_id> → EventSource용 SSE. run_loop 이벤트 전송.
  4. 루프가 끝나면 final JSON을 저장하고 PDF 변환.
  5. GET /pdf/<job_id> / /download/<job_id> 로 최종 PDF 제공.
"""

import io
import json
import os
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, request, render_template, send_file, redirect, url_for, abort, flash

from ai_errors import public_error_message
from orchestrator import run_loop, event as make_event, extract_raw_text
from render import html_to_pdf
from render_excel import render_xlsx
from qwen_client import edit_text as qwen_edit_text
from qwen_pipeline import run_pipeline as qwen_run_pipeline
from learning import append_corrections, make_correction_record, stats as learning_stats

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
OUTPUTS = BASE / "outputs"
STATES = BASE / "states"
UPLOADS.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)
STATES.mkdir(exist_ok=True)

app = Flask(__name__, template_folder=str(BASE / "templates"), static_folder=str(BASE / "static"))
app.secret_key = "court-auction-editor-local"
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB

ALLOWED_EXT = {".pdf", ".hwp", ".hwpx"}


def _find_upload(job_id: str) -> Path:
    for ext in ALLOWED_EXT:
        p = UPLOADS / f"{job_id}{ext}"
        if p.exists():
            return p
    abort(404)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/.well-known/<path:_>")
def well_known(_):
    return ("", 204)


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("파일을 선택해 주세요.")
        return redirect(url_for("index"))
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        flash(f"지원하지 않는 확장자: {ext}. .pdf / .hwp / .hwpx 만 가능합니다.")
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex[:12]
    src_path = UPLOADS / f"{job_id}{ext}"
    f.save(str(src_path))
    return redirect(url_for("process", job_id=job_id))


@app.route("/process/<job_id>")
def process(job_id: str):
    _find_upload(job_id)  # 404 방지 체크
    return render_template("process.html", job_id=job_id)


@app.route("/stream/<job_id>")
def stream(job_id: str):
    _find_upload(job_id)

    def gen():
        yield make_event(
            "fatal",
            message="레거시 스트리밍 편집 경로는 비활성화되었습니다. 현재 앱은 qwen.hyphen.it.com 기반 /run 경로만 사용합니다.",
        )
        yield make_event("end")

    return Response(
        gen(),
        status=410,
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/pdf/<job_id>")
def view_pdf(job_id: str):
    p = OUTPUTS / f"{job_id}.pdf"
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype="application/pdf")


@app.route("/download/<job_id>")
def download_pdf(job_id: str):
    p = OUTPUTS / f"{job_id}.pdf"
    if not p.exists():
        abort(404)
    return send_file(
        str(p),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"법원경매공고_편집본_{job_id}.pdf",
    )


@app.route("/xlsx/<job_id>")
def download_xlsx(job_id: str):
    p = OUTPUTS / f"{job_id}.xlsx"
    if not p.exists():
        abort(404)
    return send_file(
        str(p),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"법원경매공고_편집본_{job_id}.xlsx",
    )


JOBS: dict = {}  # job_id -> {status, phase, pdf, download, error, record_count}
JOBS_LOCK = threading.Lock()


def _set_job(job_id: str, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {})
        JOBS[job_id].update(kw)


def _get_job(job_id: str) -> dict:
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {}))


@app.route("/run/<job_id>", methods=["POST"])
def run(job_id: str):
    """파이프라인을 백그라운드로 시작. 진행 상태는 /run-status/<job_id>에서 폴링."""
    src = _find_upload(job_id)

    # 이미 시작/완료된 작업이면 즉시 현재 상태 반환
    cur = _get_job(job_id)
    if cur and cur.get("status") in ("running", "done"):
        return Response(
            json.dumps({"started": True, **cur}, ensure_ascii=False),
            mimetype="application/json; charset=utf-8",
        )

    _set_job(job_id, status="running", phase="시작 중…", pdf=None, download=None, error=None)

    def worker():
        import time as _time
        t0 = _time.time()
        _set_job(job_id, started_at=t0)
        try:
            _set_job(job_id, phase="원본 텍스트 추출 중…", percent=1, stage="extract_text")
            raw_text = extract_raw_text(str(src))
            _set_job(job_id, phase=f"hwp/pdf {len(raw_text):,}자 추출 완료", percent=2)

            def progress(msg: str):
                _set_job(job_id, phase=msg)

            def progress_detail(d: dict):
                _set_job(
                    job_id,
                    phase=d.get("phase", ""),
                    percent=d.get("percent"),
                    stage=d.get("stage"),
                    total_records=d.get("total"),
                    done_records=d.get("done"),
                )

            final = qwen_run_pipeline(
                raw_text, on_progress=progress, on_progress_detail=progress_detail
            )
            state_path = STATES / f"{job_id}.json"
            state_path.write_text(
                json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _set_job(job_id, phase="PDF 생성 중…", percent=95, stage="render")
            _render_outputs(job_id, final)
            _set_job(
                job_id,
                status="done",
                phase="완료",
                percent=100,
                stage="done",
                pdf=f"/pdf/{job_id}",
                download=f"/download/{job_id}",
                excel=f"/xlsx/{job_id}",
                record_count=len(final.get("records") or []),
                elapsed_sec=round(_time.time() - t0, 1),
            )
        except Exception as e:  # noqa: BLE001
            _set_job(job_id, status="error", error=public_error_message(e))

    threading.Thread(target=worker, daemon=True).start()
    return Response(
        json.dumps({"started": True, "status": "running", "phase": "시작 중…"}, ensure_ascii=False),
        mimetype="application/json; charset=utf-8",
    )


@app.route("/review/<job_id>", methods=["GET"])
def review(job_id: str):
    """편집 후 그룹 분류를 검수하는 페이지."""
    state_path = STATES / f"{job_id}.json"
    if not state_path.exists():
        abort(404)
    final = json.loads(state_path.read_text(encoding="utf-8"))
    # 검수 화면 표시 그룹: 게재 6개 + 게재제외 (자동차·선박)
    GROUPS = [
        "아파트",
        "연립주택/다세대/빌라",
        "단독주택,다가구주택",
        "상가/오피스텔,근린시설",
        "대지/임야/전답",
        "기타",
        "게재제외",
    ]
    return render_template(
        "review.html",
        job_id=job_id,
        header=final.get("header", {}),
        records=final.get("records", []),
        groups=GROUPS,
    )


@app.route("/review/<job_id>", methods=["POST"])
def review_save(job_id: str):
    """검수 결과(그룹 변경)를 받아 JSON 갱신 + PDF 재생성."""
    state_path = STATES / f"{job_id}.json"
    if not state_path.exists():
        abort(404)

    body = request.get_json(silent=True) or {}
    changes = body.get("changes") or {}  # {"0": "기타", "5": "기타"}
    order = body.get("order") or []
    final = json.loads(state_path.read_text(encoding="utf-8"))
    records = final.get("records", []) or []

    valid_groups = {
        "아파트",
        "연립주택/다세대/빌라",
        "단독주택,다가구주택",
        "상가/오피스텔,근린시설",
        "대지/임야/전답",
        "기타",
        "게재제외",
    }
    deleted = body.get("deleted") or []
    edits = body.get("edits") or {}  # {idx: {field: new_value}}

    # 1) 그룹 변경 (학습 데이터에 누적)
    correction_records = []
    for idx_str, new_group in changes.items():
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        if 0 <= idx < len(records) and new_group in valid_groups:
            old_group = records[idx].get("group", "기타")
            if old_group != new_group:
                correction_records.append(
                    make_correction_record(records[idx], old_group, new_group)
                )
            records[idx]["group"] = new_group
    if correction_records:
        try:
            append_corrections(correction_records)
        except Exception as e:  # noqa: BLE001
            print(f"[learning] 저장 실패: {e}")

    # 2) 필드 편집 (case_no, dup_tag, item_no, price, min_price, note, locations)
    EDITABLE = {"case_no", "dup_tag", "item_no", "price", "min_price", "note", "locations"}
    for idx_str, fields in edits.items():
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        if not (0 <= idx < len(records)) or not isinstance(fields, dict):
            continue
        for k, v in fields.items():
            if k not in EDITABLE:
                continue
            if k == "locations":
                if isinstance(v, list):
                    cleaned = []
                    for loc in v:
                        if isinstance(loc, dict):
                            cleaned.append({
                                "address": str(loc.get("address", "")),
                                "use": str(loc.get("use", "")),
                            })
                    records[idx][k] = cleaned
            else:
                records[idx][k] = str(v) if v is not None else ""

    # 3) 삭제 + 순서 재배치
    deleted_idxs = {
        int(i) for i in deleted if isinstance(i, (int, str)) and str(i).lstrip("-").isdigit()
    }
    alive = {idx for idx in range(len(records)) if idx not in deleted_idxs}
    ordered_idxs = []
    seen = set()
    for i in order:
        if not isinstance(i, (int, str)) or not str(i).lstrip("-").isdigit():
            continue
        idx = int(i)
        if idx in alive and idx not in seen:
            ordered_idxs.append(idx)
            seen.add(idx)
    for idx in range(len(records)):
        if idx in alive and idx not in seen:
            ordered_idxs.append(idx)

    records = [records[idx] for idx in ordered_idxs]

    final["records"] = records
    state_path.write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        _render_outputs(job_id, final)
    except Exception as e:  # noqa: BLE001
        return Response(
            json.dumps(
                {"ok": False, "error": public_error_message(e)},
                ensure_ascii=False,
            ),
            status=500,
            mimetype="application/json; charset=utf-8",
        )
    return Response(
        json.dumps(
            {
                "ok": True,
                "pdf": f"/pdf/{job_id}",
                "download": f"/download/{job_id}",
                "excel": f"/xlsx/{job_id}",
                "applied": len(changes),
                "ordered": len(ordered_idxs),
            },
            ensure_ascii=False,
        ),
        mimetype="application/json; charset=utf-8",
    )


@app.route("/run-status/<job_id>", methods=["GET"])
def run_status(job_id: str):
    cur = _get_job(job_id)
    if not cur:
        return Response(
            json.dumps({"status": "unknown"}, ensure_ascii=False),
            status=404,
            mimetype="application/json; charset=utf-8",
        )
    return Response(
        json.dumps(cur, ensure_ascii=False),
        mimetype="application/json; charset=utf-8",
    )


@app.route("/edit-text", methods=["GET"])
def edit_text_page():
    """원고를 붙여넣고 편집 기준에 따라 1회 편집하는 페이지."""
    return render_template("edit_text.html")


@app.route("/api/edit-text", methods=["POST"])
def api_edit_text():
    """단발 편집 API: {text} → {edited}. 루프 없이 Qwen 1회 호출."""
    body = request.get_json(silent=True) or {}
    raw = (body.get("text") or "").strip()
    if not raw:
        return Response(
            json.dumps({"error": "text 필드가 비어 있습니다."}, ensure_ascii=False),
            status=400,
            mimetype="application/json; charset=utf-8",
        )
    try:
        edited = qwen_edit_text(raw)
        return Response(
            json.dumps({"edited": edited}, ensure_ascii=False),
            mimetype="application/json; charset=utf-8",
        )
    except Exception as e:  # noqa: BLE001
        return Response(
            json.dumps(
                {"error": public_error_message(e)}, ensure_ascii=False
            ),
            status=500,
            mimetype="application/json; charset=utf-8",
        )


@app.route("/state/<job_id>")
def state(job_id: str):
    p = STATES / f"{job_id}.json"
    if not p.exists():
        abort(404)
    return Response(p.read_text(encoding="utf-8"), mimetype="application/json; charset=utf-8")


# ---------- 최종 JSON → HTML → PDF ----------

def _grouped_records(final_json: dict) -> tuple[dict, list]:
    group_order = [
        "아파트",
        "연립주택/다세대/빌라",
        "단독주택,다가구주택",
        "상가/오피스텔,근린시설",
        "대지/임야/전답",
        "기타",
    ]
    records = final_json.get("records", []) or []
    valid_groups = set(group_order)
    grouped = {}
    for r in records:
        g = (r.get("group") or "기타").strip()
        if g not in valid_groups:
            continue
        grouped.setdefault(g, []).append(r)
    ordered = [g for g in group_order if g in grouped]
    return grouped, ordered


def _render_pdf(job_id: str, final_json: dict) -> Path:
    from jinja2 import Environment, FileSystemLoader

    header = final_json.get("header", {}) or {}
    grouped, ordered = _grouped_records(final_json)

    env = Environment(
        loader=FileSystemLoader(str(BASE / "templates")),
        autoescape=True,
    )
    tmpl = env.get_template("notice.html")
    html = tmpl.render(header=header, grouped=grouped, group_order=ordered)

    pdf_path = OUTPUTS / f"{job_id}.pdf"
    html_to_pdf(html, str(pdf_path))
    return pdf_path


def _render_outputs(job_id: str, final_json: dict) -> None:
    _render_pdf(job_id, final_json)
    xlsx_path = OUTPUTS / f"{job_id}.xlsx"
    render_xlsx(final_json, str(xlsx_path))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 11222))
    app.run(host="127.0.0.1", port=port, debug=True, threaded=True)
