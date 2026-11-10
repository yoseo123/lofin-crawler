#!/usr/bin/env python3
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_from_directory, session

from crawl_lofin import URL, CrawlStoppedError, crawl, with_timestamp_suffix
from src.central_source import CENTRAL_LIST_URL, LOCAL_SOURCE, normalize_source
from src.chunked_writer import ChunkedPolicyWriter
from src.env_loader import load_env_file
from src.exporter import export_to_excel, export_to_markdown
from src.policy_index import export_policy_index
from src.jev_reranker import JevReranker
from src.policy_rag import (
    DEFAULT_COLLECTION,
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_TOP_K,
    OpenAIEmbeddingClient,
    QdrantPolicyStore,
    SimilarPolicySearch,
)
from src.web_helpers import (
    build_run_params,
    is_allowed_download_filename,
    user_facing_error_message,
)

load_env_file()

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

_TRUE_VALUES = {"1", "true", "yes", "on", "y"}
_secret_key = (os.getenv("LOFIN_WEB_SECRET_KEY") or "").strip()
if not _secret_key:
    _secret_key = secrets.token_hex(32)
    print("[WARN] LOFIN_WEB_SECRET_KEY가 없어 임시 키를 사용합니다. 재시작 시 세션이 초기화됩니다.")
app.config["SECRET_KEY"] = _secret_key
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_SECURE"] = (os.getenv("LOFIN_WEB_COOKIE_SECURE") or "").strip().lower() in _TRUE_VALUES

_ACTIVE_STATUSES = {"queued", "running", "stopping"}
_TERMINAL_STATUSES = {"completed", "failed", "stopped"}
_MAX_JOB_HISTORY = 200
_JOB_RETENTION_MS = 24 * 60 * 60 * 1000
_API_RUN_WINDOW_SECONDS = 60.0
_API_RUN_MAX_REQUESTS = 5
_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_active_job_id: str | None = None
_rate_lock = threading.Lock()
_run_request_history: dict[str, list[float]] = {}
_similar_search_service: SimilarPolicySearch | None = None


def _is_loopback_address(raw: str) -> bool:
    value = str(raw or "").strip()
    if not value:
        return False
    if value.startswith("::ffff:"):
        value = value.split("::ffff:", 1)[1]
    try:
        return bool(ipaddress.ip_address(value).is_loopback)
    except ValueError:
        return False


def _normalize_origin(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _allowed_origins() -> set[str]:
    allowed = {_normalize_origin(request.host_url)}
    extra = (os.getenv("LOFIN_ALLOWED_ORIGINS") or "").strip()
    if not extra:
        return {origin for origin in allowed if origin}
    for item in extra.split(","):
        origin = _normalize_origin(item)
        if origin:
            allowed.add(origin)
    return {origin for origin in allowed if origin}


def _request_origin() -> str:
    return _normalize_origin(request.headers.get("Origin") or request.headers.get("Referer") or "")


def _origin_is_trusted() -> bool:
    origin = _request_origin()
    if not origin:
        return True
    return origin in _allowed_origins()


def _ensure_csrf_token() -> str:
    token = str(session.get("_csrf_token") or "")
    if token:
        return token
    token = secrets.token_urlsafe(32)
    session["_csrf_token"] = token
    return token


def _submitted_csrf_token() -> str:
    header_token = str(request.headers.get("X-CSRF-Token") or "").strip()
    if header_token:
        return header_token

    form_token = str(request.form.get("csrf_token") or "").strip()
    if form_token:
        return form_token

    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return str(payload.get("csrf_token", "")).strip()
    return ""


def _csrf_token_is_valid() -> bool:
    expected = str(session.get("_csrf_token") or "")
    actual = _submitted_csrf_token()
    if not expected or not actual:
        return False
    return hmac.compare_digest(expected, actual)


def _client_key() -> str:
    forwarded = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    remote = str(request.remote_addr or "").strip()
    if remote:
        return remote
    return "unknown"


def _is_run_rate_limited() -> bool:
    now = time.monotonic()
    client = _client_key()
    with _rate_lock:
        history = _run_request_history.setdefault(client, [])
        cutoff = now - _API_RUN_WINDOW_SECONDS
        history[:] = [ts for ts in history if ts >= cutoff]
        if len(history) >= _API_RUN_MAX_REQUESTS:
            return True
        history.append(now)
        return False


def _prune_jobs_locked() -> None:
    expire_before = _now_ms() - _JOB_RETENTION_MS
    stale_ids: list[str] = []
    for job_id, job in _jobs.items():
        if job_id == _active_job_id:
            continue
        status = str(job.get("status", ""))
        updated_at = int(job.get("updated_at", 0) or 0)
        if status in _TERMINAL_STATUSES and updated_at < expire_before:
            stale_ids.append(job_id)

    for job_id in stale_ids:
        _jobs.pop(job_id, None)

    if len(_jobs) <= _MAX_JOB_HISTORY:
        return

    removable = sorted(
        (
            (job_id, int(job.get("updated_at", 0) or 0))
            for job_id, job in _jobs.items()
            if job_id != _active_job_id and str(job.get("status", "")) in _TERMINAL_STATUSES
        ),
        key=lambda item: item[1],
    )
    while len(_jobs) > _MAX_JOB_HISTORY and removable:
        old_job_id, _ = removable.pop(0)
        _jobs.pop(old_job_id, None)


def _render_index(
    *,
    result: dict[str, Any] | None,
    error: str | None,
    form_defaults: dict[str, object],
):
    return render_template(
        "index.html",
        result=result,
        error=error,
        recent_files=_recent_outputs(),
        form_defaults=form_defaults,
        csrf_token=_ensure_csrf_token(),
    )


@app.before_request
def enforce_request_protection() -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return

    if not _origin_is_trusted():
        abort(403)

    if not _csrf_token_is_valid():
        abort(403)


@app.before_request
def enforce_local_access() -> None:
    if _is_loopback_address(str(request.remote_addr or "")):
        return
    if (os.getenv("LOFIN_ALLOW_REMOTE") or "").strip().lower() in _TRUE_VALUES:
        return
    abort(403)


def _recent_outputs(limit: int = 10) -> list[str]:
    files = [
        path
        for pattern in ("*.md", "*.xlsx", "*.json")
        for path in BASE_DIR.glob(pattern)
    ]
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    return [f.name for f in files[:limit]]


def _default_form_defaults() -> dict[str, object]:
    return {
        "keyword": "",
        "source": LOCAL_SOURCE,
        "year": "",
        "fiscal_year": "2026",
        "region": "",
        "limit": "10",
        "chunk_size": "10000",
        "output_format": "md",
        "output": "out.md",
        "headed": False,
    }


def _request_form_data() -> dict[str, str]:
    return {
        "keyword": request.form.get("keyword", ""),
        "source": request.form.get("source", ""),
        "year": request.form.get("year", ""),
        "fiscal_year": request.form.get("fiscal_year", ""),
        "region": request.form.get("region", ""),
        "limit": request.form.get("limit", ""),
        "chunk_size": request.form.get("chunk_size", ""),
        "output_format": request.form.get("output_format", ""),
        "output": request.form.get("output", ""),
        "headed": request.form.get("headed", ""),
    }


def _coerce_checkbox(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else ""
    text = str(value or "").strip().lower()
    return "on" if text in {"1", "true", "yes", "on", "y"} else ""


def _json_form_data(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "keyword": str(payload.get("keyword", "")),
        "source": normalize_source(str(payload.get("source", ""))),
        "year": str(payload.get("year", "")),
        "fiscal_year": str(payload.get("fiscal_year", "")),
        "region": str(payload.get("region", "")),
        "limit": str(payload.get("limit", "")),
        "chunk_size": str(payload.get("chunk_size", "")),
        "output_format": str(payload.get("output_format", "")),
        "output": str(payload.get("output", "")),
        "headed": _coerce_checkbox(payload.get("headed", "")),
    }


def _similar_policy_service() -> SimilarPolicySearch:
    global _similar_search_service
    if _similar_search_service is not None:
        return _similar_search_service

    qdrant_url = (os.getenv("LOFIN_QDRANT_URL") or "http://127.0.0.1:6333").strip()
    collection = (os.getenv("LOFIN_QDRANT_COLLECTION") or DEFAULT_COLLECTION).strip()
    model = (os.getenv("LOFIN_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL).strip()
    dimensions = int(os.getenv("LOFIN_EMBEDDING_DIMENSIONS") or DEFAULT_EMBEDDING_DIMENSIONS)
    reranker = None
    if (os.getenv("LOFIN_JEV_API_KEY") or "").strip() or (os.getenv("BEATAPI_API_KEY") or "").strip():
        try:
            reranker = JevReranker()
        except Exception:
            app.logger.warning("Jev 초기화 실패: 기존 유사 정책 검색을 사용합니다.")
    _similar_search_service = SimilarPolicySearch(
        embedding_client=OpenAIEmbeddingClient(model=model, dimensions=dimensions),
        store=QdrantPolicyStore(url=qdrant_url, collection=collection, vector_size=dimensions),
        reranker=reranker,
    )
    return _similar_search_service


def _similar_policy_request_payload(payload: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
    query = str(payload.get("query", "")).strip()
    top_k = max(1, min(int(payload.get("top_k", DEFAULT_TOP_K) or DEFAULT_TOP_K), 50))
    filters = {
        "year_from": payload.get("year_from"),
        "year_to": payload.get("year_to"),
        "region": str(payload.get("region", "")).strip(),
        "municipality": str(payload.get("municipality", "")).strip(),
        "category": str(payload.get("category", "")).strip(),
    }
    return query, top_k, filters


def _write_output_files(rows: list[dict[str, str]], params: dict[str, object]) -> dict[str, object]:
    output_format = str(params.get("output_format", "md") or "md")
    output_path = with_timestamp_suffix(BASE_DIR / str(params["output"]))
    md_path = output_path.with_suffix(".md") if output_format in {"md", "both"} else None
    xlsx_path = output_path.with_suffix(".xlsx") if output_format in {"xlsx", "both"} else None

    if md_path is not None:
        export_to_markdown(rows, md_path)
    if xlsx_path is not None:
        export_to_excel(rows, xlsx_path)

    primary_path = md_path or xlsx_path
    if primary_path is None:
        raise ValueError("출력 형식이 올바르지 않습니다.")

    json_output_path = primary_path.with_suffix(".json")
    index_result = export_policy_index(
        rows,
        json_output_path,
        source=str(params.get("source", LOCAL_SOURCE)),
        source_url=URL if str(params.get("source", LOCAL_SOURCE)) == LOCAL_SOURCE else CENTRAL_LIST_URL,
        default_year=str(params.get("year", "") or ""),
        default_region=str(params.get("region", "") or ""),
        source_file=primary_path.name,
        xlsx_file=xlsx_path.name if xlsx_path is not None else "",
    )

    return {
        "file": primary_path.name,
        "md_file": md_path.name if md_path is not None else "",
        "xlsx_file": xlsx_path.name if xlsx_path is not None else "",
        "json_file": json_output_path.name,
        "indexed": int(index_result.indexed),
        "index_skipped": int(index_result.skipped),
    }


def _chunked_output_enabled(params: dict[str, object]) -> bool:
    chunk_size = int(params.get("chunk_size", 0) or 0)
    limit = int(params.get("limit", 0) or 0)
    return chunk_size > 0 and limit > chunk_size


def _create_chunked_writer(params: dict[str, object]) -> ChunkedPolicyWriter:
    output_format = str(params.get("output_format", "md") or "md")
    output_path = with_timestamp_suffix(BASE_DIR / str(params["output"]))
    return ChunkedPolicyWriter(
        output_path=output_path,
        output_format=output_format,
        chunk_size=int(params.get("chunk_size", 10000) or 10000),
        source=str(params.get("source", LOCAL_SOURCE)),
        source_url=URL if str(params.get("source", LOCAL_SOURCE)) == LOCAL_SOURCE else CENTRAL_LIST_URL,
        default_year=str(params.get("year", "") or ""),
        default_region=str(params.get("region", "") or ""),
    )


def _chunked_result_payload(result) -> dict[str, object]:
    first_file = result.files[0] if result.files else result.manifest_file
    first_md = next((name for name in result.files if name.endswith(".md")), "")
    first_xlsx = next((name for name in result.files if name.endswith(".xlsx")), "")
    first_json = result.json_files[0] if result.json_files else ""
    return {
        "file": first_file,
        "md_file": first_md,
        "xlsx_file": first_xlsx,
        "json_file": first_json,
        "manifest_file": result.manifest_file,
        "chunks": int(result.chunks),
        "indexed": int(result.indexed),
        "index_skipped": int(result.index_skipped),
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _release_active_job_locked(job_id: str) -> None:
    global _active_job_id
    if _active_job_id == job_id:
        _active_job_id = None


def _serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(job.get("id", "")),
        "status": str(job.get("status", "queued")),
        "message": str(job.get("message", "")),
        "progress": {
            "percent": int(job.get("progress_percent", 0) or 0),
            "current": int(job.get("progress_current", 0) or 0),
            "total": int(job.get("progress_total", 0) or 0),
        },
        "stop_requested": bool(job.get("stop_requested", False)),
        "result": job.get("result"),
        "error": str(job.get("error", "")),
        "created_at": int(job.get("created_at", 0) or 0),
        "updated_at": int(job.get("updated_at", 0) or 0),
    }


def _set_job_error_locked(job: dict[str, Any], message: str) -> None:
    job["status"] = "failed"
    job["message"] = message
    job["error"] = message
    job["updated_at"] = _now_ms()


def _run_crawl_job(job_id: str, params: dict[str, object]) -> None:
    def is_stop_requested() -> bool:
        with _jobs_lock:
            job = _jobs.get(job_id)
            return bool(job and job.get("stop_requested"))

    def on_progress(payload: dict[str, object]) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                return
            if job.get("status") in {"completed", "failed", "stopped"}:
                return
            if job.get("stop_requested"):
                job["status"] = "stopping"
            else:
                job["status"] = "running"
            job["message"] = str(payload.get("message", "") or job.get("message", ""))
            job["progress_percent"] = max(0, min(int(payload.get("percent", 0) or 0), 100))
            job["progress_current"] = max(0, int(payload.get("current", 0) or 0))
            job["progress_total"] = max(0, int(payload.get("total", 0) or 0))
            job["updated_at"] = _now_ms()

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        if job.get("stop_requested"):
            job["status"] = "stopped"
            job["message"] = "작업이 시작되기 전에 중지되었습니다."
            job["updated_at"] = _now_ms()
            _release_active_job_locked(job_id)
            return
        job["status"] = "running"
        job["message"] = "크롤링을 시작합니다."
        job["updated_at"] = _now_ms()

    try:
        writer = _create_chunked_writer(params) if _chunked_output_enabled(params) else None
        try:
            rows = crawl(
                keyword=str(params["keyword"]),
                limit=int(params["limit"]),
                headless=not bool(params["headed"]),
                slow_mo_ms=0,
                source=str(params.get("source", LOCAL_SOURCE)),
                year=str(params["year"] or "") or None,
                region=str(params["region"] or "") or None,
                debug_dir=BASE_DIR / "debug",
                progress_callback=on_progress,
                stop_requested=is_stop_requested,
                row_callback=writer.add_row if writer is not None else None,
            )
        except Exception:
            if writer is not None:
                writer.close(complete=False)
            raise

        if is_stop_requested():
            if writer is not None:
                writer.close(complete=False)
            raise CrawlStoppedError("사용자 요청으로 작업이 중지되었습니다.")

        if writer is not None:
            chunked_result = writer.close(complete=True)
            count = int(chunked_result.count)
            if count == 0:
                raise RuntimeError("수집 결과가 없습니다. 조건을 바꿔 다시 시도하세요.")
            output_result = _chunked_result_payload(chunked_result)
        elif not rows:
            raise RuntimeError("수집 결과가 없습니다. 조건을 바꿔 다시 시도하세요.")
        else:
            count = len(rows)
            on_progress(
                {
                    "message": "결과 파일과 검색 색인을 저장합니다.",
                    "percent": 96,
                    "current": len(rows),
                    "total": len(rows),
                }
            )

            output_result = _write_output_files(rows, params)

        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                return
            job["status"] = "completed"
            job["message"] = "완료되었습니다."
            job["error"] = ""
            job["progress_percent"] = 100
            job["progress_current"] = count
            job["progress_total"] = count
            job["result"] = {
                "count": count,
                **output_result,
            }
            job["updated_at"] = _now_ms()
            _release_active_job_locked(job_id)
    except CrawlStoppedError as exc:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                return
            job["status"] = "stopped"
            job["message"] = str(exc) or "사용자 요청으로 작업이 중지되었습니다."
            job["error"] = ""
            job["updated_at"] = _now_ms()
            _release_active_job_locked(job_id)
    except Exception as exc:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                return
            _set_job_error_locked(job, user_facing_error_message(exc))
            _release_active_job_locked(job_id)


@app.get("/")
def index_get():
    return _render_index(
        result=None,
        error=None,
        form_defaults=_default_form_defaults(),
    )


@app.post("/")
def index_post():
    form_data = _request_form_data()

    try:
        params = build_run_params(form_data)
        writer = _create_chunked_writer(params) if _chunked_output_enabled(params) else None
        try:
            rows = crawl(
                keyword=params["keyword"],
                limit=params["limit"],
                headless=not params["headed"],
                slow_mo_ms=0,
                source=str(params.get("source", LOCAL_SOURCE)),
                year=params["year"] or None,
                region=params["region"] or None,
                debug_dir=BASE_DIR / "debug",
                row_callback=writer.add_row if writer is not None else None,
            )
        except Exception:
            if writer is not None:
                writer.close(complete=False)
            raise

        if writer is not None:
            chunked_result = writer.close(complete=True)
            if chunked_result.count == 0:
                return _render_index(
                    result=None,
                    error="수집 결과가 없습니다. 조건을 바꿔 다시 시도하세요.",
                    form_defaults=form_data,
                )
            output_result = _chunked_result_payload(chunked_result)
            count = int(chunked_result.count)
        elif not rows:
            return _render_index(
                result=None,
                error="수집 결과가 없습니다. 조건을 바꿔 다시 시도하세요.",
                form_defaults=form_data,
            )
        else:
            output_result = _write_output_files(rows, params)
            count = len(rows)

        return _render_index(
            result={
                "count": count,
                **output_result,
            },
            error=None,
            form_defaults=form_data,
        )

    except Exception as exc:
        return _render_index(
            result=None,
            error=user_facing_error_message(exc),
            form_defaults=form_data,
        )


@app.post("/api/run")
def api_run():
    global _active_job_id

    if _is_run_rate_limited():
        return jsonify({"error": "요청이 너무 많습니다. 잠시 후 다시 시도하세요."}), 429

    json_payload = request.get_json(silent=True)
    if isinstance(json_payload, dict):
        form_data = _json_form_data(json_payload)
    else:
        form_data = _request_form_data()

    try:
        params = build_run_params(form_data)
    except Exception as exc:
        return jsonify({"error": user_facing_error_message(exc)}), 400

    now_ms = _now_ms()
    job_id = uuid.uuid4().hex

    with _jobs_lock:
        _prune_jobs_locked()

        if _active_job_id is not None:
            active = _jobs.get(_active_job_id)
            if active and str(active.get("status")) in _ACTIVE_STATUSES:
                return (
                    jsonify(
                        {
                            "error": "이미 실행 중인 작업이 있습니다. 현재 작업을 중지하거나 완료 후 다시 시도하세요.",
                            "job": _serialize_job(active),
                        }
                    ),
                    409,
                )

        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "message": "작업 대기 중입니다.",
            "progress_percent": 0,
            "progress_current": 0,
            "progress_total": 0,
            "stop_requested": False,
            "result": None,
            "error": "",
            "created_at": now_ms,
            "updated_at": now_ms,
        }
        _active_job_id = job_id
        snapshot = _serialize_job(_jobs[job_id])

    threading.Thread(target=_run_crawl_job, args=(job_id, params), daemon=True).start()
    return jsonify({"job": snapshot}), 202


@app.get("/api/jobs/active")
def api_active_job():
    with _jobs_lock:
        if _active_job_id is None:
            return jsonify({"job": None})
        job = _jobs.get(_active_job_id)
        if not job:
            return jsonify({"job": None})
        return jsonify({"job": _serialize_job(job)})


@app.get("/api/jobs/<job_id>")
def api_job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
        return jsonify({"job": _serialize_job(job)})


@app.post("/api/jobs/<job_id>/stop")
def api_stop_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "작업을 찾을 수 없습니다."}), 404

        status = str(job.get("status", ""))
        if status in {"completed", "failed", "stopped"}:
            return jsonify({"job": _serialize_job(job), "error": "이미 종료된 작업입니다."}), 409

        job["stop_requested"] = True
        if status == "queued":
            job["status"] = "stopped"
            job["message"] = "작업이 시작되기 전에 중지되었습니다."
            _release_active_job_locked(job_id)
        else:
            job["status"] = "stopping"
            job["message"] = "중지 요청을 처리 중입니다."
        job["updated_at"] = _now_ms()
        return jsonify({"job": _serialize_job(job)})


@app.post("/api/similar-policies")
def api_similar_policies():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON 요청 본문이 필요합니다."}), 400
    try:
        query, top_k, filters = _similar_policy_request_payload(payload)
        if not query:
            raise ValueError("검색어를 입력하세요.")
        results = _similar_policy_service().search(query, top_k=top_k, filters=filters)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": user_facing_error_message(exc)}), 503
    return jsonify({"count": len(results), "results": results})


@app.get("/download/<path:filename>")
def download(filename: str):
    if not is_allowed_download_filename(filename):
        abort(400)
    if not (BASE_DIR / filename).is_file():
        abort(404)
    return send_from_directory(BASE_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    port = int(os.getenv("LOFIN_WEB_PORT", "8787"))
    host = os.getenv("LOFIN_WEB_HOST", "0.0.0.0")
    print(f"[INFO] Web UI: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
