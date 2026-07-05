from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import DB_PATH, REPO_ROOT, ensure_runtime_dirs, openai_defaults, ytdlp_defaults
from .stages import STAGES


ACTIVE_STATUSES = ("queued", "running")
STALE_ON_RESTART_STATUSES = ("running",)
EXECUTION_MODES = ("auto", "manual")
DEFAULT_EXECUTION_MODE = "auto"
TASK_STATUSES = ("queued", "running", "paused", "succeeded", "failed", "cancelled")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    ensure_runtime_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              url TEXT NOT NULL,
              title TEXT,
              source_author TEXT,
              source_description TEXT,
              source_published_at TEXT,
              thumbnail_url TEXT,
              status TEXT NOT NULL,
              current_stage TEXT,
              session_path TEXT,
              final_video_path TEXT,
              duration_seconds INTEGER,
              error_message TEXT,
              created_at TEXT NOT NULL,
              started_at TEXT,
              completed_at TEXT,
              execution_mode TEXT NOT NULL DEFAULT 'auto'
            );

            CREATE TABLE IF NOT EXISTS task_stages (
              task_id TEXT NOT NULL,
              name TEXT NOT NULL,
              label TEXT NOT NULL,
              status TEXT NOT NULL,
              progress INTEGER,
              started_at TEXT,
              completed_at TEXT,
              last_message TEXT,
              error_message TEXT,
              PRIMARY KEY (task_id, name),
              FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        defaults = openai_defaults()
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (f"openai.{key}", value, now_iso()),
            )
        for key, value in ytdlp_defaults().items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (f"ytdlp.{key}", value, now_iso()),
            )
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "title" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN title TEXT")
        if "execution_mode" not in task_columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'auto'"
            )
        if "duration_seconds" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN duration_seconds INTEGER")
        if "source_author" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN source_author TEXT")
        if "source_description" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN source_description TEXT")
        if "source_published_at" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN source_published_at TEXT")
        if "thumbnail_url" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN thumbnail_url TEXT")
        stage_columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_stages)").fetchall()}
        if "progress" not in stage_columns:
            conn.execute("ALTER TABLE task_stages ADD COLUMN progress INTEGER")


def _duration_seconds(value: Any) -> int | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return round(duration)


def backfill_titles_from_metadata() -> None:
    import json
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_path, title, source_author, source_description,
                   source_published_at, thumbnail_url, duration_seconds
            FROM tasks
            WHERE session_path IS NOT NULL
              AND (
                (title IS NULL OR title = '')
                OR (source_author IS NULL OR source_author = '')
                OR (source_description IS NULL OR source_description = '')
                OR (source_published_at IS NULL OR source_published_at = '')
                OR (thumbnail_url IS NULL OR thumbnail_url = '')
                OR duration_seconds IS NULL
              )
            """
        ).fetchall()
    for row in rows:
        session = Path(row["session_path"])
        info_path = session / "metadata" / "ytdlp_info.json"
        if not info_path.exists():
            info_path = session / "metadata" / "local_info.json"
        if not info_path.exists():
            continue
        info = json.loads(info_path.read_text(encoding="utf-8"))
        title = (info.get("title") or "").strip()
        duration = _duration_seconds(info.get("duration"))
        updates: dict[str, Any] = {}
        if title and not (row["title"] or "").strip():
            updates["title"] = title
        author = str(info.get("uploader") or info.get("channel") or info.get("creator") or "").strip()
        if author and not (row["source_author"] or "").strip():
            updates["source_author"] = author
        description = str(info.get("description") or "").strip()
        if description and not (row["source_description"] or "").strip():
            updates["source_description"] = description
        upload_date = str(info.get("upload_date") or "").strip()
        published_at = ""
        if len(upload_date) == 8 and upload_date.isdigit():
            published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        else:
            published_at = str(info.get("release_date") or info.get("modified_date") or "").strip()
        if published_at and not (row["source_published_at"] or "").strip():
            updates["source_published_at"] = published_at
        thumbnail_url = str(info.get("thumbnail") or "").strip()
        if thumbnail_url and not (row["thumbnail_url"] or "").strip():
            updates["thumbnail_url"] = thumbnail_url
        if duration is not None and row["duration_seconds"] is None:
            updates["duration_seconds"] = duration
        if not updates:
            continue
        with connect() as conn:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",
                [*updates.values(), row["id"]],
            )


def fail_stale_active_tasks() -> None:
    with connect() as conn:
        active_tasks = conn.execute(
            "SELECT id, current_stage FROM tasks WHERE status IN "
            f"({','.join('?' for _ in STALE_ON_RESTART_STATUSES)})",
            STALE_ON_RESTART_STATUSES,
        ).fetchall()
        for task in active_tasks:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'queued', error_message = NULL, completed_at = NULL
                WHERE id = ?
                """,
                (task["id"],),
            )
            if task["current_stage"]:
                conn.execute(
                    """
                    UPDATE task_stages
                    SET status = 'pending', error_message = NULL, completed_at = NULL
                    WHERE task_id = ? AND name = ? AND status IN ('pending', 'running')
                    """,
                    (task["id"], task["current_stage"]),
                )


def normalize_execution_mode(value: str | None) -> str:
    mode = (value or DEFAULT_EXECUTION_MODE).strip().lower()
    if mode not in EXECUTION_MODES:
        raise ValueError(f"execution_mode must be one of: {', '.join(EXECUTION_MODES)}")
    return mode


def create_task(
    url: str,
    task_id: str | None = None,
    *,
    execution_mode: str = DEFAULT_EXECUTION_MODE,
    auto_start: bool = True,
    title: str | None = None,
    source_author: str | None = None,
    source_description: str | None = None,
    source_published_at: str | None = None,
    thumbnail_url: str | None = None,
    duration_seconds: int | None = None,
) -> str:
    new_id = task_id or str(uuid.uuid4())
    created_at = now_iso()
    mode = normalize_execution_mode(execution_mode)
    status = "queued" if auto_start else "paused"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
              id, url, title, source_author, source_description, source_published_at,
              thumbnail_url, duration_seconds, status, current_stage, created_at, execution_mode
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                url,
                title,
                source_author,
                source_description,
                source_published_at,
                thumbnail_url,
                duration_seconds,
                status,
                STAGES[0].name,
                created_at,
                mode,
            ),
        )
        conn.executemany(
            """
            INSERT INTO task_stages (task_id, name, label, status)
            VALUES (?, ?, ?, 'pending')
            """,
            [(new_id, stage.name, stage.label) for stage in STAGES],
        )
    return new_id


def find_task_by_video_id(video_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM tasks WHERE id = ? OR url LIKE ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (video_id, f"%{video_id}%"),
        ).fetchone()
    return row["id"] if row else None


def has_active_task() -> bool:
    with connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM tasks WHERE status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) LIMIT 1",
            ACTIVE_STATUSES,
        ).fetchone()
    return row is not None


def latest_task_id() -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    return row["id"] if row else None


TASK_SUMMARY_COLUMNS = (
    "id, url, title, source_author, source_published_at, thumbnail_url, "
    "status, current_stage, session_path, final_video_path, duration_seconds, error_message, "
    "created_at, started_at, completed_at, execution_mode"
)

TASK_LIST_SORTS = {
    "created_desc": "created_at DESC, rowid DESC",
    "created_asc": "created_at ASC, rowid ASC",
    "started_desc": "started_at IS NULL ASC, started_at DESC, rowid DESC",
    "started_asc": "started_at IS NULL ASC, started_at ASC, rowid ASC",
    "completed_desc": "completed_at IS NULL ASC, completed_at DESC, rowid DESC",
    "completed_asc": "completed_at IS NULL ASC, completed_at ASC, rowid ASC",
    "status_asc": (
        "CASE status "
        "WHEN 'queued' THEN 1 "
        "WHEN 'running' THEN 2 "
        "WHEN 'paused' THEN 3 "
        "WHEN 'cancelled' THEN 4 "
        "WHEN 'failed' THEN 5 "
        "WHEN 'succeeded' THEN 6 "
        "ELSE 99 END ASC, created_at DESC, rowid DESC"
    ),
    "status_desc": (
        "CASE status "
        "WHEN 'queued' THEN 1 "
        "WHEN 'running' THEN 2 "
        "WHEN 'paused' THEN 3 "
        "WHEN 'cancelled' THEN 4 "
        "WHEN 'failed' THEN 5 "
        "WHEN 'succeeded' THEN 6 "
        "ELSE 99 END DESC, created_at DESC, rowid DESC"
    ),
    "title_asc": "LOWER(COALESCE(NULLIF(TRIM(title), ''), url)) ASC, created_at DESC, rowid DESC",
    "title_desc": "LOWER(COALESCE(NULLIF(TRIM(title), ''), url)) DESC, created_at DESC, rowid DESC",
}


def list_tasks(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {TASK_SUMMARY_COLUMNS} FROM tasks "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        tasks = [dict(row) for row in rows]
        _attach_stages(conn, tasks)
    return tasks


def _attach_stages(conn: sqlite3.Connection, tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        return
    task_ids = [task["id"] for task in tasks]
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"""
        SELECT task_id, name, label, status, progress, started_at, completed_at,
               last_message, error_message
        FROM task_stages
        WHERE task_id IN ({placeholders})
        ORDER BY
          CASE name
            WHEN 'download' THEN 1
            WHEN 'separate' THEN 2
            WHEN 'asr' THEN 3
            WHEN 'asr_fix' THEN 4
            WHEN 'translate' THEN 5
            WHEN 'split_audio' THEN 6
            WHEN 'tts' THEN 7
            WHEN 'merge_audio' THEN 8
            WHEN 'merge_video' THEN 9
            ELSE 99
          END
        """,
        task_ids,
    ).fetchall()
    stages_by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    for row in rows:
        stages_by_task[row["task_id"]].append(dict(row))
    for task in tasks:
        task["stages"] = stages_by_task.get(task["id"], [])
        task["bilibili_publish_status"] = _bilibili_publish_status(task)


def _bilibili_publish_status(task: dict[str, Any]) -> str:
    session_path = str(task.get("session_path") or "")
    if session_path:
        status_path = Path(session_path) / "metadata" / "bilibili_publish.json"
        if status_path.exists():
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            status = str(payload.get("status") or "").strip()
            if status in {"draft", "running", "succeeded", "failed"}:
                return status
    final_video = str(task.get("final_video_path") or "")
    if final_video:
        return "draft"
    return "unpublished"


def list_tasks_page(
    *,
    page: int = 1,
    page_size: int = 20,
    query: str = "",
    status: str = "all",
    execution_mode: str = "all",
    sort: str = "created_desc",
    hide_completed: bool = False,
) -> dict[str, Any]:
    page = max(page, 1)
    page_size = max(page_size, 1)
    offset = (page - 1) * page_size
    where_parts: list[str] = []
    params: list[Any] = []

    needle = query.strip().lower()
    if needle:
        pattern = f"%{needle}%"
        where_parts.append(
            "(LOWER(COALESCE(title, '')) LIKE ? "
            "OR LOWER(url) LIKE ? "
            "OR LOWER(id) LIKE ?)"
        )
        params.extend([pattern, pattern, pattern])
    if status == "incomplete":
        where_parts.append("status != 'succeeded'")
    elif status != "all":
        where_parts.append("status = ?")
        params.append(status)
    if execution_mode != "all":
        where_parts.append("execution_mode = ?")
        params.append(execution_mode)

    where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    order_sql = TASK_LIST_SORTS.get(sort, TASK_LIST_SORTS["created_desc"])

    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM tasks{where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT {TASK_SUMMARY_COLUMNS} FROM tasks{where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        tasks = [dict(row) for row in rows]
        _attach_stages(conn, tasks)

    return {
        "tasks": tasks,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def list_bilibili_candidate_tasks(limit: int = 500) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, url, title, status, session_path, final_video_path,
                   created_at, completed_at
            FROM tasks
            WHERE session_path IS NOT NULL OR final_video_path IS NOT NULL
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_task(task_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return None
        stages = conn.execute(
            """
            SELECT * FROM task_stages
            WHERE task_id = ?
            ORDER BY
              CASE name
                WHEN 'download' THEN 1
                WHEN 'separate' THEN 2
                WHEN 'asr' THEN 3
                WHEN 'asr_fix' THEN 4
                WHEN 'translate' THEN 5
                WHEN 'split_audio' THEN 6
                WHEN 'tts' THEN 7
                WHEN 'merge_audio' THEN 8
                WHEN 'merge_video' THEN 9
                ELSE 99
              END
            """,
            (task_id,),
        ).fetchall()
    result = dict(task)
    result["stages"] = [dict(stage) for stage in stages]
    return result


def get_current_task() -> dict[str, Any] | None:
    task_id = latest_task_id()
    return get_task(task_id) if task_id else None


def delete_task(task_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.execute("DELETE FROM task_stages WHERE task_id = ?", (task_id,))
        return cursor.rowcount > 0


def queue_task_for_continue(task_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'queued', error_message = NULL, completed_at = NULL
            WHERE id = ?
            """,
            (task_id,),
        )


def list_requeueable_tasks() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, status FROM tasks
            WHERE status IN ('paused', 'cancelled', 'failed')
            ORDER BY created_at ASC, rowid ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_requeueable_task_ids() -> list[str]:
    return [task["id"] for task in list_requeueable_tasks()]


def cancel_task(task_id: str, message: str = "Task cancelled by user.") -> None:
    completed_at = now_iso()
    with connect() as conn:
        task = conn.execute("SELECT current_stage FROM tasks WHERE id = ?", (task_id,)).fetchone()
        conn.execute(
            """
            UPDATE tasks
            SET status = 'cancelled', error_message = ?, completed_at = ?
            WHERE id = ?
            """,
            (message, completed_at, task_id),
        )
        if task and task["current_stage"] and task["current_stage"] != "done":
            conn.execute(
                """
                UPDATE task_stages
                SET status = 'failed', error_message = ?, completed_at = ?, last_message = ?
                WHERE task_id = ? AND name = ? AND status = 'running'
                """,
                (message, completed_at, message, task_id, task["current_stage"]),
            )


def mark_task_cancel_requested(task_id: str, message: str = "Cancellation requested.") -> None:
    with connect() as conn:
        task = conn.execute("SELECT current_stage FROM tasks WHERE id = ?", (task_id,)).fetchone()
        conn.execute("UPDATE tasks SET error_message = ? WHERE id = ?", (message, task_id))
        if task and task["current_stage"] and task["current_stage"] != "done":
            conn.execute(
                """
                UPDATE task_stages
                SET last_message = ?
                WHERE task_id = ? AND name = ? AND status = 'running'
                """,
                (message, task_id, task["current_stage"]),
            )


def reset_stages_from(task_id: str, from_stage: str) -> None:
    from .stages import STAGE_NAMES

    if from_stage not in STAGE_NAMES:
        raise ValueError(f"Unknown stage: {from_stage}")

    start = STAGE_NAMES.index(from_stage)
    with connect() as conn:
        for stage in STAGE_NAMES[start:]:
            conn.execute(
                """
                UPDATE task_stages
                SET status = 'pending', started_at = NULL, completed_at = NULL,
                    progress = NULL, last_message = NULL, error_message = NULL
                WHERE task_id = ? AND name = ?
                """,
                (task_id, stage),
            )
        conn.execute(
            """
            UPDATE tasks
            SET status = 'queued', current_stage = ?, final_video_path = NULL,
                completed_at = NULL, error_message = NULL
            WHERE id = ?
            """,
            (from_stage, task_id),
        )


def reset_failed_for_resume(task_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE task_stages
            SET status = 'pending', started_at = NULL, completed_at = NULL,
                progress = NULL, last_message = NULL, error_message = NULL
            WHERE task_id = ? AND status IN ('failed', 'running')
            """,
            (task_id,),
        )
        conn.execute(
            """
            UPDATE tasks
            SET status = 'queued', error_message = NULL, completed_at = NULL,
                started_at = NULL
            WHERE id = ?
            """,
            (task_id,),
        )


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [task_id]
    with connect() as conn:
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)


def update_stage(task_id: str, name: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [task_id, name]
    with connect() as conn:
        conn.execute(f"UPDATE task_stages SET {assignments} WHERE task_id = ? AND name = ?", values)


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now_iso()),
        )


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_openai_settings() -> dict[str, str]:
    from .adapters.openai_client import normalize_openai_base_url

    defaults = openai_defaults()
    return {
        "base_url": normalize_openai_base_url(get_setting("openai.base_url", defaults["base_url"])),
        "api_key": get_setting("openai.api_key", defaults["api_key"]),
        "model": get_setting("openai.model", defaults["model"]),
        "translate_concurrency": get_setting(
            "openai.translate_concurrency", defaults["translate_concurrency"]
        ),
        "translate_use_batch": get_setting(
            "openai.translate_use_batch", defaults["translate_use_batch"]
        ),
    }


def save_openai_settings(
    base_url: str,
    api_key: str,
    model: str,
    translate_concurrency: str = "",
    translate_use_batch: str = "true",
    *,
    clear_api_key: bool = False,
) -> None:
    from .adapters.openai_client import normalize_openai_base_url

    set_setting("openai.base_url", normalize_openai_base_url(base_url))
    cleaned_api_key = api_key.strip()
    if clear_api_key:
        set_setting("openai.api_key", "")
    elif cleaned_api_key and set(cleaned_api_key) != {"*"}:
        set_setting("openai.api_key", cleaned_api_key)
    set_setting("openai.model", model.strip())
    if translate_concurrency.strip():
        set_setting("openai.translate_concurrency", translate_concurrency.strip())
    set_setting("openai.translate_use_batch", translate_use_batch.strip().lower())


def get_ytdlp_settings() -> dict[str, str]:
    defaults = ytdlp_defaults()
    return {
        "proxy_port": get_setting("ytdlp.proxy_port", defaults["proxy_port"]),
    }


def get_bilibili_credentials() -> dict[str, Any] | None:
    raw = get_setting("bilibili.credentials", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def save_bilibili_credentials(credentials: dict[str, Any]) -> None:
    set_setting("bilibili.credentials", json.dumps(credentials, ensure_ascii=False))


def clear_bilibili_credentials() -> None:
    set_setting("bilibili.credentials", "")


def save_ytdlp_settings(proxy_port: str) -> None:
    set_setting("ytdlp.proxy_port", proxy_port.strip())


def sync_settings_from_env() -> dict[str, dict[str, str]]:
    load_dotenv(REPO_ROOT / ".env", override=True)
    openai = openai_defaults()
    save_openai_settings(
        openai["base_url"],
        openai["api_key"],
        openai["model"],
        openai["translate_concurrency"],
        openai["translate_use_batch"],
        clear_api_key=not bool(openai["api_key"].strip()),
    )
    ytdlp = ytdlp_defaults()
    save_ytdlp_settings(ytdlp["proxy_port"])
    return {
        "openai": get_openai_settings(),
        "ytdlp": get_ytdlp_settings(),
    }


def log_path(task_id: str) -> Path:
    from .config import LOG_DIR

    return LOG_DIR / f"{task_id}.log"
