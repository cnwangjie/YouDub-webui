from __future__ import annotations

import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Thread
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from . import cancellation, database, worker
from .adapters.local_subtitles import parse_srt, uploaded_subtitle_dir
from .adapters.local_video import remove_upload, uploaded_video_dir
from .adapters.openai_translate import list_models as list_openai_models
from .adapters.openai_translate import test_connection as test_openai_connection
from .config import WORKFOLDER, YOUTUBE_COOKIE_PATH, ensure_runtime_dirs
from .pipeline import run_task_stage
from .runtime_checks import validate_runtime_device
from .sanitize import sanitize_text
from .sources import detect_source
from .stage_reset import remove_stage_artifacts
from .stages import STAGE_NAMES
from .youtube import LOCAL_UPLOAD_DIRECTIONS, extract_video_id, is_local_upload_url

ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".wmv"}
ALLOWED_SUBTITLE_SUFFIXES = {".srt"}
LOCAL_UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_LOCAL_UPLOAD_BYTES = int(os.getenv("LOCAL_UPLOAD_MAX_BYTES", str(4 * 1024 * 1024 * 1024)))
MAX_LOCAL_SUBTITLE_BYTES = int(os.getenv("LOCAL_SUBTITLE_MAX_BYTES", str(20 * 1024 * 1024)))

TaskListStatus = Literal["all", "queued", "running", "paused", "succeeded", "failed", "cancelled"]
TaskListExecutionMode = Literal["all", "auto", "manual"]
TaskListSort = Literal[
    "created_desc",
    "created_asc",
    "started_desc",
    "started_asc",
    "completed_desc",
    "completed_asc",
    "status_asc",
    "status_desc",
    "title_asc",
    "title_desc",
]

ACTIVE_BILIBILI_PUBLISHES: set[str] = set()


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return "********"


class TaskCreate(BaseModel):
    url: str
    execution_mode: str = "auto"
    auto_start: bool = True


class ContinueTaskRequest(BaseModel):
    execution_mode: str | None = None


class YouTubeCookieUpdate(BaseModel):
    content: str


class OpenAISettingsUpdate(BaseModel):
    base_url: str
    api_key: str = ""
    clear_api_key: bool = False
    model: str
    translate_concurrency: str = ""
    translate_use_batch: bool = True


class OpenAIModelsRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""


class OpenAITestRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class YtdlpSettingsUpdate(BaseModel):
    proxy_port: str = ""


class BilibiliQrPollRequest(BaseModel):
    auth_code: str


class BilibiliPublishRequest(BaseModel):
    title: str = ""
    description: str = ""
    source: str = ""
    tags: list[str] | str = ""
    tid: int | None = None


def normalize_proxy_port(value: str) -> str:
    proxy_port = value.strip()
    if not proxy_port:
        return ""
    if not proxy_port.isdigit():
        raise HTTPException(status_code=422, detail="Proxy port must be numeric.")
    port = int(proxy_port)
    if port < 1 or port > 65535:
        raise HTTPException(status_code=422, detail="Proxy port must be between 1 and 65535.")
    return str(port)


def normalize_translate_concurrency(value: str) -> str:
    concurrency = value.strip()
    if not concurrency:
        return ""
    if not all("0" <= char <= "9" for char in concurrency):
        raise HTTPException(status_code=422, detail="Translate concurrency must be numeric.")
    workers = int(concurrency)
    if workers < 1 or workers > 200:
        raise HTTPException(
            status_code=422, detail="Translate concurrency must be between 1 and 200."
        )
    return concurrency


def bool_setting(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_runtime_dirs()
    database.init_db()
    database.backfill_titles_from_metadata()
    database.fail_stale_active_tasks()
    try:
        from .adapters.bilibili_publish import fail_stale_running_publishes

        fail_stale_running_publishes()
    except Exception:
        pass
    worker.start(run_task_stage)
    yield


app = FastAPI(title="YouDub API", lifespan=lifespan)


DEFAULT_CORS_ORIGIN_REGEX = (
    r"^https?://("
    r"localhost|"
    r"127(?:\.\d{1,3}){3}|"
    r"0\.0\.0\.0|"
    r"10(?:\.\d{1,3}){3}|"
    r"192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|"
    r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}|"
    r"\[::1\]"
    r"):3000$"
)


def cors_origins() -> list[str]:
    defaults = ["http://localhost:3000", "http://127.0.0.1:3000"]
    configured = os.getenv("CORS_ALLOW_ORIGINS", "")
    extra = [origin.strip() for origin in configured.split(",") if origin.strip()]
    return [*defaults, *extra]


def cors_origin_regex() -> str:
    configured = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip()
    return configured or DEFAULT_CORS_ORIGIN_REGEX


app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_origin_regex=cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/worker")
def get_worker_status() -> dict:
    return worker.status()


@app.post("/api/worker/start")
def start_worker() -> dict:
    worker.start(run_task_stage)
    return worker.status()


@app.post("/api/worker/stop")
def stop_worker() -> dict:
    worker.pause()
    return worker.status()


def _ensure_runtime_ready() -> None:
    try:
        validate_runtime_device()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def normalize_execution_mode(value: str) -> str:
    try:
        return database.normalize_execution_mode(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskCreate) -> dict:
    try:
        video_id = extract_video_id(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing_id = database.find_task_by_video_id(video_id)
    if existing_id:
        return database.get_task(existing_id)

    if payload.auto_start:
        _ensure_runtime_ready()
    task_id = database.create_task(
        payload.url.strip(),
        task_id=video_id,
        execution_mode=normalize_execution_mode(payload.execution_mode),
        auto_start=payload.auto_start,
    )
    if payload.auto_start:
        worker.enqueue(task_id)
    return database.get_task(task_id)


def _clean_upload_filename(filename: str | None) -> str:
    original = Path(filename or "").name.strip()
    if not original:
        raise HTTPException(status_code=422, detail="Video filename is required.")
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise HTTPException(status_code=422, detail="Unsupported video file type.")
    safe_stem = sanitize_text(Path(original).stem) or "video"
    return f"{safe_stem}{suffix}"


def _clean_subtitle_filename(filename: str | None) -> str:
    original = Path(filename or "").name.strip()
    if not original:
        raise HTTPException(status_code=422, detail="Subtitle filename is required.")
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_SUBTITLE_SUFFIXES:
        raise HTTPException(status_code=422, detail="Only .srt subtitle files are supported.")
    safe_stem = sanitize_text(Path(original).stem) or "subtitles"
    return f"{safe_stem}{suffix}"


def _save_uploaded_file(file: UploadFile, destination: Path, *, max_bytes: int, too_large_detail: str) -> int:
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        while True:
            chunk = file.file.read(LOCAL_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=too_large_detail)
            handle.write(chunk)
    if total == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")
    return total


def _validate_uploaded_srt(path: Path) -> None:
    try:
        parse_srt(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid SRT subtitle file encoding.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid SRT subtitle file: {exc}") from exc


@app.post("/api/tasks/upload", status_code=201)
def upload_local_video(
    direction: str = Form("en-zh"),
    file: UploadFile = File(...),
    subtitle_file: UploadFile | None = File(None),
    execution_mode: str = Form("auto"),
    auto_start: bool = Form(True),
) -> dict:
    if direction not in LOCAL_UPLOAD_DIRECTIONS:
        raise HTTPException(status_code=422, detail="Unsupported local video direction.")

    if auto_start:
        _ensure_runtime_ready()
    original_name = Path(file.filename or "").name.strip()
    stored_name = _clean_upload_filename(original_name)
    task_id = str(uuid.uuid4())
    try:
        _save_uploaded_file(
            file,
            uploaded_video_dir(WORKFOLDER, task_id) / stored_name,
            max_bytes=MAX_LOCAL_UPLOAD_BYTES,
            too_large_detail="Uploaded video is too large.",
        )
        if subtitle_file is not None and subtitle_file.filename:
            subtitle_name = _clean_subtitle_filename(subtitle_file.filename)
            subtitle_path = uploaded_subtitle_dir(WORKFOLDER, task_id) / subtitle_name
            _save_uploaded_file(
                subtitle_file,
                subtitle_path,
                max_bytes=MAX_LOCAL_SUBTITLE_BYTES,
                too_large_detail="Uploaded subtitle is too large.",
            )
            _validate_uploaded_srt(subtitle_path)
    except HTTPException:
        remove_upload(WORKFOLDER, task_id)
        raise

    url = f"local://upload/{task_id}?direction={direction}&filename={quote(original_name)}"
    database.create_task(
        url,
        task_id=task_id,
        execution_mode=normalize_execution_mode(execution_mode),
        auto_start=auto_start,
    )
    database.update_task(task_id, title=Path(original_name).stem)
    if auto_start:
        worker.enqueue(task_id)
    return database.get_task(task_id)


@app.get("/api/tasks/current")
def current_task() -> dict | None:
    return database.get_current_task()


@app.get("/api/tasks")
def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    q: str = Query("", max_length=200),
    status: TaskListStatus = "all",
    execution_mode: TaskListExecutionMode = "all",
    sort: TaskListSort = "created_desc",
    hide_completed: bool = Query(False),
) -> dict:
    return database.list_tasks_page(
        page=page,
        page_size=page_size,
        query=q,
        status=status,
        execution_mode=execution_mode,
        sort=sort,
        hide_completed=hide_completed,
    )


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


@app.post("/api/tasks/requeue-all")
def requeue_all_tasks() -> dict:
    _ensure_runtime_ready()
    task_ids = database.list_requeueable_task_ids()
    for task_id in task_ids:
        database.queue_task_for_continue(task_id)
        worker.enqueue(task_id)
    return {"queued": len(task_ids), "task_ids": task_ids}


def _is_inside_workfolder(path: Path) -> bool:
    workfolder = WORKFOLDER.resolve()
    try:
        path.resolve().relative_to(workfolder)
    except ValueError:
        return False
    return True


def _purge_task(task: dict) -> None:
    session_path = task.get("session_path")
    if session_path:
        session_dir = Path(session_path)
        if session_dir.exists() and _is_inside_workfolder(session_dir):
            shutil.rmtree(session_dir)
    log_file = database.log_path(task["id"])
    if log_file.exists():
        log_file.unlink()
    database.delete_task(task["id"])


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: str) -> Response:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a running task.")
    _purge_task(task)
    if is_local_upload_url(task["url"]):
        remove_upload(WORKFOLDER, task["id"])
    return Response(status_code=204)


@app.post("/api/tasks/{task_id}/rerun")
def rerun_task(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] == "running":
        raise HTTPException(status_code=409, detail="Cannot rerun a running task.")

    _ensure_runtime_ready()
    url = task["url"]
    execution_mode = task.get("execution_mode") or database.DEFAULT_EXECUTION_MODE
    _purge_task(task)
    new_id = database.create_task(url, task_id=task_id, execution_mode=execution_mode)
    worker.enqueue(new_id)
    return database.get_task(new_id)


@app.post("/api/tasks/{task_id}/stages/{stage_name}/redo")
def redo_stage(task_id: str, stage_name: str) -> dict:
    if stage_name not in STAGE_NAMES:
        raise HTTPException(status_code=404, detail="Stage not found.")
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if (task.get("execution_mode") or database.DEFAULT_EXECUTION_MODE) != "manual":
        raise HTTPException(status_code=409, detail="Only manual tasks support per-stage redo.")
    if task["status"] in {"running", "queued"}:
        raise HTTPException(status_code=409, detail="Task is already running or queued.")
    stage = next((item for item in task["stages"] if item["name"] == stage_name), None)
    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found.")
    if stage["status"] not in {"succeeded", "failed"}:
        raise HTTPException(status_code=409, detail="Only completed or failed stages can be redone.")
    _ensure_runtime_ready()
    session_path = task.get("session_path")
    if session_path:
        remove_stage_artifacts(Path(session_path), stage_name, detect_source(task["url"]))
    database.reset_stages_from(task_id, stage_name)
    worker.enqueue(task_id)
    return database.get_task(task_id)


@app.post("/api/tasks/{task_id}/continue")
def continue_task(task_id: str, payload: ContinueTaskRequest | None = None) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] not in {"paused", "cancelled"}:
        raise HTTPException(status_code=409, detail="Only paused or cancelled tasks can be continued.")
    if payload and payload.execution_mode is not None:
        database.update_task(task_id, execution_mode=normalize_execution_mode(payload.execution_mode))
    _ensure_runtime_ready()
    database.queue_task_for_continue(task_id)
    worker.enqueue(task_id)
    return database.get_task(task_id)


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] == "cancelled":
        return task
    if task["status"] not in {"queued", "running", "paused"}:
        raise HTTPException(status_code=409, detail="Only queued, running, or paused tasks can be cancelled.")
    if task["status"] == "running":
        cancellation.request_cancel(task_id)
        database.mark_task_cancel_requested(task_id)
    else:
        database.cancel_task(task_id)
    return database.get_task(task_id)


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task["status"] != "failed":
        raise HTTPException(status_code=409, detail="Only failed tasks can be resumed.")
    _ensure_runtime_ready()
    database.reset_failed_for_resume(task_id)
    worker.enqueue(task_id)
    return database.get_task(task_id)


@app.get("/api/tasks/{task_id}/log", response_class=PlainTextResponse)
def task_log(task_id: str) -> str:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    path = database.log_path(task_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _task_session(task: dict) -> Path:
    session_path = task.get("session_path")
    if not session_path:
        raise HTTPException(status_code=409, detail="Task metadata is not available yet.")
    session = Path(session_path)
    if not session.exists() or not _is_inside_workfolder(session):
        raise HTTPException(status_code=404, detail="Task session is not available.")
    return session


def _public_localized_metadata(task_id: str, payload: dict) -> dict:
    thumbnail_file = payload.get("thumbnail_file")
    thumbnail_api_url = ""
    if thumbnail_file and Path(str(thumbnail_file)).exists():
        thumbnail_api_url = f"/api/tasks/{task_id}/artifact/thumbnail"
    return {**payload, "thumbnail_api_url": thumbnail_api_url}


@app.get("/api/tasks/{task_id}/metadata/localized")
def get_localized_metadata(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    session = _task_session(task)
    from .adapters.metadata_localization import load_artifact

    payload = load_artifact(session)
    if payload is None:
        raise HTTPException(status_code=404, detail="Localized metadata is not available.")
    return _public_localized_metadata(task_id, payload)


@app.post("/api/tasks/{task_id}/metadata/localized")
def generate_localized_metadata(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    session = _task_session(task)
    from .adapters.metadata_localization import localize_metadata

    try:
        payload = localize_metadata(session, detect_source(task["url"]))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to localize metadata: {exc}") from exc
    return _public_localized_metadata(task_id, payload)


@app.get("/api/tasks/{task_id}/artifact/thumbnail")
def task_thumbnail(task_id: str) -> FileResponse:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    session = _task_session(task)
    from .adapters.metadata_localization import load_artifact

    payload = load_artifact(session)
    thumbnail_file = Path(str((payload or {}).get("thumbnail_file") or ""))
    if not payload or not thumbnail_file.exists() or not _is_inside_workfolder(thumbnail_file):
        raise HTTPException(status_code=404, detail="Thumbnail is not available.")
    return FileResponse(thumbnail_file)


@app.post("/api/bilibili/qrcode")
def create_bilibili_qrcode() -> dict:
    from .adapters.bilibili_publish import get_bilibili_qrcode

    try:
        return get_bilibili_qrcode()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to create Bilibili QR code: {exc}") from exc


@app.post("/api/bilibili/qrcode/poll")
def poll_bilibili_qrcode(payload: BilibiliQrPollRequest) -> dict:
    from .adapters.bilibili_publish import poll_bilibili_qrcode

    try:
        return poll_bilibili_qrcode(payload.auth_code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to poll Bilibili QR code: {exc}") from exc


@app.get("/api/bilibili/credentials")
def get_bilibili_credentials_status() -> dict:
    credentials = database.get_bilibili_credentials()
    if not credentials:
        return {"configured": False}
    cookies = credentials.get("cookie_info", {}).get("cookies") or []
    names = [item.get("name") for item in cookies if isinstance(item, dict)]
    return {
        "configured": True,
        "platform": credentials.get("platform") or "",
        "has_bili_jct": "bili_jct" in names,
        "has_sessdata": "SESSDATA" in names,
    }


@app.delete("/api/bilibili/credentials", status_code=204)
def clear_bilibili_credentials() -> Response:
    database.clear_bilibili_credentials()
    return Response(status_code=204)


@app.get("/api/bilibili/publish/records")
def list_bilibili_publish_records() -> dict:
    from .adapters.bilibili_publish import list_publish_records

    return {"records": list_publish_records()}


@app.get("/api/bilibili/account/archives")
def list_bilibili_account_archives(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str = Query("is_pubed", max_length=40),
) -> dict:
    from .adapters.bilibili_publish import list_account_archives

    try:
        return list_account_archives(page=page, page_size=page_size, status=status)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list Bilibili archives: {exc}") from exc


@app.get("/api/bilibili/partitions")
def list_bilibili_partitions() -> dict:
    from .adapters.bilibili_publish import list_partitions

    try:
        return {"partitions": list_partitions()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list Bilibili partitions: {exc}") from exc


def _publish_task_to_bilibili(task_id: str, overrides: dict | None = None) -> None:
    try:
        task = database.get_task(task_id)
        if not task:
            return
        session = Path(str(task["session_path"]))
        final_path = Path(str(task["final_video_path"]))
        from .adapters.bilibili_publish import publish_to_bilibili

        publish_to_bilibili(final_path, session, detect_source(task["url"]), overrides)
    finally:
        ACTIVE_BILIBILI_PUBLISHES.discard(task_id)


@app.get("/api/tasks/{task_id}/bilibili/publish")
def get_bilibili_publish_status(task_id: str) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    session = _task_session(task)
    from .adapters.bilibili_publish import mark_stale_running_publish

    if task_id in ACTIVE_BILIBILI_PUBLISHES:
        from .adapters.bilibili_publish import load_publish_status

        status = load_publish_status(session)
    else:
        status = mark_stale_running_publish(session)
    if status is None:
        return {"status": "idle", "progress": 0, "message": "", "error": "", "result": None}
    return status


@app.post("/api/tasks/{task_id}/bilibili/publish")
def start_bilibili_publish(task_id: str, payload: BilibiliPublishRequest | None = None) -> dict:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    session = _task_session(task)
    final_path = task.get("final_video_path")
    if not final_path or not Path(final_path).exists():
        raise HTTPException(status_code=409, detail="Final video is not available.")
    if not database.get_bilibili_credentials():
        raise HTTPException(status_code=409, detail="Bilibili credentials are not configured. Scan QR code first.")
    from .adapters.bilibili_publish import load_publish_status, mark_stale_running_publish, write_publish_status

    current = load_publish_status(session) if task_id in ACTIVE_BILIBILI_PUBLISHES else mark_stale_running_publish(session)
    if current and current.get("status") == "running":
        return current
    overrides = payload.model_dump() if payload else {}
    previous = current or {}
    for key in ("title", "description", "source", "tags", "tid"):
        if overrides.get(key):
            continue
        if previous.get(key):
            overrides[key] = previous[key]
    tags = overrides.get("tags")
    if isinstance(tags, str):
        tag_text = tags
    else:
        tag_text = ",".join(str(tag).strip() for tag in tags if str(tag).strip())
    status = write_publish_status(
        session,
        {
            "status": "running",
            "progress": 0,
            "message": "Queued Bilibili publish",
            "title": overrides.get("title", "").strip(),
            "description": overrides.get("description", "").strip(),
            "source": overrides.get("source", "").strip(),
            "tags": [tag.strip() for tag in tag_text.replace("，", ",").replace("\n", ",").split(",") if tag.strip()][:10],
            "tid": overrides.get("tid"),
            "error": "",
            "result": None,
            "video_file": str(final_path),
        },
    )
    ACTIVE_BILIBILI_PUBLISHES.add(task_id)
    Thread(target=_publish_task_to_bilibili, args=(task_id, overrides), daemon=True).start()
    return status


@app.get("/api/tasks/{task_id}/artifact/final-video")
def final_video(task_id: str, download: bool = False) -> FileResponse:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    final_path = task.get("final_video_path")
    if not final_path or not Path(final_path).exists():
        raise HTTPException(status_code=404, detail="Final video is not available.")
    name = Path(final_path).name
    if download:
        return FileResponse(final_path, media_type="video/mp4", filename=name)
    headers = {"Content-Disposition": f'inline; filename="{name}"'}
    return FileResponse(final_path, media_type="video/mp4", headers=headers)


@app.get("/api/cookies/youtube")
def get_youtube_cookie() -> dict:
    exists = YOUTUBE_COOKIE_PATH.exists()
    size = YOUTUBE_COOKIE_PATH.stat().st_size if exists else 0
    updated_at = YOUTUBE_COOKIE_PATH.stat().st_mtime if exists else None
    return {"exists": exists, "size": size, "updated_at": updated_at, "content": ""}


@app.post("/api/cookies/youtube")
def save_youtube_cookie(payload: YouTubeCookieUpdate) -> dict:
    YOUTUBE_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = payload.content.strip()
    if content:
        YOUTUBE_COOKIE_PATH.write_text(content + "\n", encoding="utf-8")
    elif YOUTUBE_COOKIE_PATH.exists():
        YOUTUBE_COOKIE_PATH.unlink()
    return get_youtube_cookie()


@app.get("/api/settings/openai")
def get_openai_settings() -> dict:
    settings = database.get_openai_settings()
    return {
        "base_url": settings["base_url"],
        "api_key": mask_secret(settings["api_key"]),
        "has_api_key": bool(settings["api_key"]),
        "model": settings["model"],
        "translate_concurrency": settings["translate_concurrency"],
        "translate_use_batch": bool_setting(settings["translate_use_batch"]),
    }


@app.post("/api/settings/openai")
def save_openai_settings(payload: OpenAISettingsUpdate) -> dict:
    database.save_openai_settings(
        payload.base_url,
        payload.api_key,
        payload.model,
        normalize_translate_concurrency(payload.translate_concurrency),
        "true" if payload.translate_use_batch else "false",
        clear_api_key=payload.clear_api_key,
    )
    return get_openai_settings()


@app.post("/api/settings/openai/models")
def get_openai_models(payload: OpenAIModelsRequest) -> dict:
    settings = database.get_openai_settings()
    base_url = payload.base_url.strip() or settings["base_url"]
    api_key = payload.api_key.strip() or settings["api_key"]
    try:
        models = list_openai_models(base_url=base_url, api_key=api_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch models: {exc}") from exc
    return {"models": models}


@app.post("/api/settings/openai/test")
def test_openai_settings(payload: OpenAITestRequest) -> dict:
    settings = database.get_openai_settings()
    base_url = payload.base_url.strip() or settings["base_url"]
    api_key = payload.api_key.strip() or settings["api_key"]
    model = payload.model.strip() or settings["model"]
    try:
        result = test_openai_connection(base_url=base_url, api_key=api_key, model=model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OpenAI connection test failed: {exc}") from exc
    return {"ok": True, **result}


@app.post("/api/settings/sync-env")
def sync_settings_from_env() -> dict:
    synced = database.sync_settings_from_env()
    openai = synced["openai"]
    return {
        "openai": {
            "base_url": openai["base_url"],
            "api_key": mask_secret(openai["api_key"]),
            "has_api_key": bool(openai["api_key"]),
            "model": openai["model"],
            "translate_concurrency": openai["translate_concurrency"],
            "translate_use_batch": bool_setting(openai["translate_use_batch"]),
        },
        "ytdlp": synced["ytdlp"],
    }


@app.get("/api/settings/ytdlp")
def get_ytdlp_settings() -> dict:
    return database.get_ytdlp_settings()


@app.post("/api/settings/ytdlp")
def save_ytdlp_settings(payload: YtdlpSettingsUpdate) -> dict:
    database.save_ytdlp_settings(normalize_proxy_port(payload.proxy_port))
    return get_ytdlp_settings()
