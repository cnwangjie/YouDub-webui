from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import parse

import requests

from .. import database
from ..config import bilibili_publish_defaults
from ..sources import SourceConfig, detect_source
from .metadata_localization import load_artifact, localize_metadata

ProgressCallback = Callable[[int, str], None]

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
TITLE_LIMIT = 80
DESCRIPTION_LIMIT = 2000
TAG_LIMIT = 10
BILITV_APP_KEY = "4409e2ce8ffd12b8"
BILITV_APP_SECRET = "59b43e04ad6965f34319062b478f83dd"
BILIBILI_UPLOAD_MANAGER_URL = "https://member.bilibili.com/platform/upload-manager/article?page=1"


def _bilibili_headers() -> dict[str, str]:
    return {
        "user-agent": USER_AGENT,
        "referer": "https://www.bilibili.com/",
        "origin": "https://www.bilibili.com",
        "accept": "application/json, text/plain, */*",
    }


def _json_response(response: requests.Response) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError as exc:
        snippet = response.text[:300].replace("\n", " ")
        raise RuntimeError(
            f"Bilibili returned non-JSON response: HTTP {response.status_code}, "
            f"content-type={response.headers.get('content-type')}, body={snippet!r}"
        ) from exc


@dataclass
class PublishData:
    copyright: int = 2
    source: str = ""
    tid: int = 21
    cover: str = ""
    title: str = ""
    desc_format_id: int = 0
    desc: str = ""
    desc_v2: list[dict[str, Any]] = field(default_factory=list)
    dynamic: str = ""
    subtitle: dict[str, Any] = field(default_factory=lambda: {"open": 0, "lan": ""})
    tag: str = ""
    videos: list[dict[str, Any]] = field(default_factory=list)
    dtime: Any = None
    dolby: int = 0
    hires: int = 0
    no_reprint: int = 0
    is_only_self: int = 0
    charging_pay: int = 0


def status_path(session: Path) -> Path:
    return session / "metadata" / "bilibili_publish.json"


def load_status(session: Path) -> dict[str, Any] | None:
    path = status_path(session)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_status(session: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = status_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return payload


def _trim(text: str, limit: int) -> str:
    value = (text or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _signed_tv_payload(**values: Any) -> dict[str, Any]:
    payload = {
        "appkey": BILITV_APP_KEY,
        **values,
        "local_id": "0",
        "ts": int(time.time()),
    }
    encoded = parse.urlencode(payload)
    payload["sign"] = hashlib.md5(f"{encoded}{BILITV_APP_SECRET}".encode()).hexdigest()
    return payload


def get_bilibili_qrcode() -> dict[str, Any]:
    response = requests.post(
        "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code",
        data=_signed_tv_payload(),
        headers=_bilibili_headers(),
        timeout=10,
    )
    data = _json_response(response)
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to create Bilibili QR code: {data}")
    return data


def poll_bilibili_qrcode(auth_code: str) -> dict[str, Any]:
    response = requests.post(
        "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll",
        data=_signed_tv_payload(auth_code=auth_code),
        headers=_bilibili_headers(),
        timeout=10,
    )
    data = _json_response(response)
    if data.get("code") == 0 and data.get("data"):
        credentials = data["data"]
        credentials["platform"] = "BiliTV"
        database.save_bilibili_credentials(credentials)
        return {"status": "succeeded"}
    if data.get("code") == 86039:
        return {"status": "pending", "message": data.get("message") or "Waiting for QR confirmation"}
    return {"status": "failed", "message": data.get("message") or str(data), "raw": data}


def _cookies_from_credentials(credentials: dict[str, Any]) -> dict[str, str]:
    cookies = credentials.get("cookie_info", {}).get("cookies")
    if not isinstance(cookies, list):
        raise ValueError("Stored Bilibili credentials do not contain cookie_info.cookies.")
    return {str(item["name"]): str(item["value"]) for item in cookies}


class BilibiliPublisher:
    def __init__(self, credentials: dict[str, Any], progress_callback: ProgressCallback | None = None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "user-agent": USER_AGENT,
                "referer": "https://www.bilibili.com/",
                "connection": "keep-alive",
            }
        )
        self.credentials = credentials
        self.progress_callback = progress_callback
        self.csrf = ""

    def progress(self, value: int, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(max(0, min(100, value)), message)

    def login_by_stored_credentials(self) -> None:
        cookies = _cookies_from_credentials(self.credentials)
        if not cookies:
            raise ValueError("No Bilibili cookies found.")
        self.session.cookies.update(cookies)
        self.csrf = cookies.get("bili_jct", "")
        if not self.csrf:
            raise ValueError("Bilibili cookie must include bili_jct for upload CSRF.")
        response = self.session.get("https://api.bilibili.com/x/web-interface/nav", timeout=15)
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Bilibili cookie validation failed: {data}")

    def upload_cover(self, cover_file: str) -> str:
        if not cover_file:
            return ""
        path = Path(cover_file)
        if not path.exists():
            return ""
        payload = path.read_bytes()
        try:
            from io import BytesIO
            from PIL import Image

            with Image.open(path) as image:
                width, height = image.size
                ratio = 16 / 10
                if width / height > ratio:
                    delta = width - height * ratio
                    image = image.crop((delta / 2, 0, width - delta / 2, height))
                else:
                    delta = height - width / ratio
                    image = image.crop((0, delta / 2, width, height - delta / 2))
                buffer = BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=92)
                payload = buffer.getvalue()
        except Exception:
            pass
        encoded = b"data:image/jpeg;base64," + base64.b64encode(payload)
        response = self.session.post(
            "https://member.bilibili.com/x/vu/web/cover/up",
            data={"cover": encoded, "csrf": self.csrf},
            timeout=30,
        )
        data = response.json()
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"Bilibili cover upload failed: {data}")
        return str(data["data"]["url"]).replace("http:", "")

    def _preupload(self, video_file: Path, line: str) -> dict[str, Any]:
        query = f"upcdn={line}&probe_version=20221109" if line and line.upper() != "AUTO" else "probe_version=20221109"
        params = {
            "r": "upos",
            "profile": "ugcupos/bup",
            "ssl": 0,
            "version": "2.8.12",
            "build": 2081200,
            "name": video_file.name,
            "size": video_file.stat().st_size,
        }
        response = self.session.get(f"https://member.bilibili.com/preupload?{query}", params=params, timeout=15)
        data = response.json()
        if "chunk_size" not in data:
            raise RuntimeError(f"Bilibili preupload failed: {data}")
        return data

    def _upload_chunk(
        self,
        url: str,
        headers: dict[str, str],
        upload_id: str,
        chunks: int,
        total: int,
        index: int,
        chunk: bytes,
        chunk_size: int,
    ) -> dict[str, Any]:
        start = index * chunk_size
        params = {
            "uploadId": upload_id,
            "chunks": chunks,
            "total": total,
            "chunk": index,
            "size": chunk_size,
            "partNumber": index + 1,
            "start": start,
            "end": start + len(chunk),
        }
        last_error = None
        for _ in range(3):
            try:
                response = self.session.put(url, params=params, data=chunk, headers=headers, timeout=60)
                if response.status_code == 200:
                    return {"partNumber": index + 1, "eTag": "etag"}
                last_error = RuntimeError(f"chunk {index + 1} status {response.status_code}: {response.text[:200]}")
            except Exception as exc:
                last_error = exc
            time.sleep(1)
        raise RuntimeError(f"Bilibili chunk upload failed: {last_error}")

    def upload_video(self, video_file: Path, *, line: str = "bda2", threads: int = 3) -> dict[str, str]:
        total = video_file.stat().st_size
        pre = self._preupload(video_file, line)
        chunk_size = int(pre["chunk_size"])
        chunks = math.ceil(total / chunk_size)
        upos_uri = str(pre["upos_uri"])
        endpoint = str(pre["endpoint"])
        url = f"https:{endpoint}/{upos_uri.replace('upos://', '')}"
        headers = {"X-Upos-Auth": str(pre["auth"])}
        upload_id = self.session.post(f"{url}?uploads&output=json", headers=headers, timeout=30).json()["upload_id"]

        parts: list[dict[str, Any]] = []
        uploaded = 0
        self.progress(10, f"Uploading video to Bilibili: 0/{chunks} chunks")
        with video_file.open("rb") as handle, ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
            futures = {}
            for index in range(chunks):
                chunk = handle.read(chunk_size)
                futures[
                    pool.submit(
                        self._upload_chunk,
                        url,
                        headers,
                        upload_id,
                        chunks,
                        total,
                        index,
                        chunk,
                        chunk_size,
                    )
                ] = len(chunk)
            for future in as_completed(futures):
                parts.append(future.result())
                uploaded += futures[future]
                progress = 10 + round(uploaded / total * 70)
                self.progress(progress, f"Uploading video to Bilibili: {len(parts)}/{chunks} chunks")
        parts.sort(key=lambda item: item["partNumber"])
        complete_params = {
            "name": video_file.name,
            "uploadId": upload_id,
            "biz_id": pre["biz_id"],
            "output": "json",
            "profile": "ugcupos/bup",
        }
        response = self.session.post(url, params=complete_params, json={"parts": parts}, headers=headers, timeout=30)
        data = response.json()
        if data.get("OK") != 1:
            raise RuntimeError(f"Bilibili complete upload failed: {data}")
        return {
            "title": video_file.stem[:TITLE_LIMIT],
            "filename": Path(upos_uri).stem,
            "desc": "",
        }

    def submit(self, post_data: PublishData) -> dict[str, Any]:
        response = self.session.post(
            f"https://member.bilibili.com/x/vu/web/add?csrf={self.csrf}",
            json=asdict(post_data),
            timeout=30,
        )
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Bilibili submit failed: {data}")
        return data

    def list_archives(self, *, status: str = "is_pubed", page: int = 1, page_size: int = 20) -> dict[str, Any]:
        response = self.session.get(
            "https://member.bilibili.com/x/web/archives",
            params={"status": status, "pn": page, "ps": page_size},
            timeout=30,
        )
        data = _json_response(response)
        if data.get("code") != 0:
            response = self.session.get(
                "https://member.bilibili.com/x2/creative/web/archives/sp",
                params={"pn": page, "ps": page_size},
                timeout=30,
            )
            data = _json_response(response)
        if data.get("code") != 0:
            raise RuntimeError(f"Bilibili archive list failed: {data}")
        return data

    def archive_pre(self) -> dict[str, Any]:
        response = self.session.get("https://member.bilibili.com/x/vupre/web/archive/pre", timeout=30)
        data = _json_response(response)
        if data.get("code") != 0:
            raise RuntimeError(f"Bilibili archive pre failed: {data}")
        return data


def _metadata(session: Path, source: SourceConfig) -> dict[str, Any]:
    payload = load_artifact(session)
    if payload is None:
        payload = localize_metadata(session, source)
    return payload


def _source_metadata(session: Path) -> dict[str, Any]:
    path = session / "metadata" / "ytdlp_info.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _source_author(info: dict[str, Any]) -> str:
    return str(info.get("uploader") or info.get("channel") or info.get("creator") or "").strip()


def _source_url(info: dict[str, Any]) -> str:
    return str(info.get("webpage_url") or info.get("original_url") or info.get("url") or "").strip()


def _format_upload_date(info: dict[str, Any]) -> str:
    raw_date = str(info.get("upload_date") or "").strip()
    if len(raw_date) == 8 and raw_date.isdigit():
        return f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    timestamp = info.get("timestamp")
    try:
        if timestamp:
            return datetime.fromtimestamp(int(timestamp), timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""
    return raw_date


def _default_publish_title(metadata: dict[str, Any], info: dict[str, Any]) -> str:
    translated_title = str(metadata.get("translated_title") or metadata.get("title") or "YouDub video").strip()
    author = _source_author(info)
    return f"【中配】{translated_title} - {author}" if author else f"【中配】{translated_title}"


def _default_publish_description(metadata: dict[str, Any], info: dict[str, Any]) -> str:
    source_title = str(info.get("title") or metadata.get("title") or "").strip()
    author = _source_author(info)
    upload_date = _format_upload_date(info)
    video_url = _source_url(info)
    translated_description = str(
        metadata.get("translated_description") or metadata.get("description") or ""
    ).strip()
    header = [
        f"原视频标题：{source_title}" if source_title else "",
        f"原作者：{author}" if author else "",
        f"发布日期：{upload_date}" if upload_date else "",
        f"视频链接：{video_url}" if video_url else "",
    ]
    parts = [line for line in header if line]
    if translated_description:
        parts.extend(["", translated_description])
    return "\n".join(parts)


def _split_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_tags = [str(item) for item in value]
    else:
        raw = str(value or "")
        for separator in ("，", "\n", ";", "；"):
            raw = raw.replace(separator, ",")
        raw_tags = raw.split(",")
    tags: list[str] = []
    seen: set[str] = set()
    for tag in raw_tags:
        cleaned = tag.strip()
        if not cleaned or cleaned in seen:
            continue
        tags.append(cleaned)
        seen.add(cleaned)
        if len(tags) >= TAG_LIMIT:
            break
    return tags


def _publish_fields(
    session: Path,
    source: SourceConfig,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _metadata(session, source)
    source_info = _source_metadata(session)
    config = bilibili_publish_defaults()
    overrides = overrides or {}
    title = overrides.get("title") or _default_publish_title(metadata, source_info)
    description = overrides.get("description") or _default_publish_description(metadata, source_info)
    default_tags = metadata.get("translated_tags") or metadata.get("tags") or []
    override_tags = _split_tags(overrides.get("tags")) if overrides.get("tags") is not None else []
    tags = override_tags or _split_tags(default_tags)
    source_url = str(overrides.get("source") or config["source"] or _source_url(source_info)).strip()
    try:
        tid = int(overrides.get("tid") or config["tid"])
    except (TypeError, ValueError):
        tid = int(config["tid"])
    return {
        "title": _trim(str(title), TITLE_LIMIT),
        "description": _trim(str(description), DESCRIPTION_LIMIT),
        "tags": tags,
        "cover_file": str(metadata.get("thumbnail_file") or ""),
        "tid": tid,
        "copyright": int(config["copyright"]),
        "source": source_url,
        "line": config["line"],
        "threads": int(config["threads"]) if config["threads"].isdigit() else 3,
    }


def publish_status_path(session: Path) -> Path:
    return session / "metadata" / "bilibili_publish.json"


def load_publish_status(session: Path) -> dict[str, Any] | None:
    path = publish_status_path(session)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def mark_stale_running_publish(session: Path) -> dict[str, Any] | None:
    status = load_publish_status(session)
    if not status or status.get("status") != "running":
        return status
    status.update(
        {
            "status": "failed",
            "message": "Bilibili publish interrupted",
            "error": "Backend restarted or publish worker stopped before the upload completed. Start publishing again.",
        }
    )
    return write_publish_status(session, status)


def fail_stale_running_publishes() -> None:
    for task in database.list_bilibili_candidate_tasks():
        session_path = task.get("session_path")
        if not session_path:
            continue
        session = Path(str(session_path))
        if session.exists():
            mark_stale_running_publish(session)


def _status_updated_at(session: Path) -> str | None:
    path = publish_status_path(session)
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def write_publish_status(session: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = publish_status_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return payload


def _result_ids(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"aid": None, "bvid": "", "url": ""}
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    aid = data.get("aid") or data.get("archive_id")
    bvid = str(data.get("bvid") or "")
    url = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
    return {"aid": aid, "bvid": bvid, "url": url}


def _normalize_cover_url(value: Any) -> str:
    cover = str(value or "")
    if cover.startswith("//"):
        return f"https:{cover}"
    if cover.startswith("/"):
        return f"https://i0.hdslb.com{cover}"
    return cover


def _timestamp_to_iso(value: Any) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _normalize_archive(item: dict[str, Any]) -> dict[str, Any]:
    archive = item.get("Archive") if isinstance(item.get("Archive"), dict) else item
    stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}
    aid = archive.get("aid")
    bvid = str(archive.get("bvid") or "")
    return {
        "aid": aid,
        "bvid": bvid,
        "title": str(archive.get("title") or ""),
        "description": str(archive.get("desc") or ""),
        "cover": _normalize_cover_url(archive.get("cover")),
        "tag": str(archive.get("tag") or ""),
        "duration": int(archive.get("duration") or 0),
        "state": archive.get("state"),
        "state_desc": str(archive.get("state_desc") or ""),
        "created_at": _timestamp_to_iso(archive.get("ctime")),
        "published_at": _timestamp_to_iso(archive.get("ptime") or archive.get("online_time")),
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        "edit_url": f"https://member.bilibili.com/platform/upload-manager/article?keyword={aid}" if aid else "",
        "stats": {
            "view": int(stat.get("view") or stat.get("vv") or 0),
            "danmaku": int(stat.get("danmaku") or 0),
            "reply": int(stat.get("reply") or 0),
            "favorite": int(stat.get("favorite") or 0),
            "coin": int(stat.get("coin") or 0),
            "share": int(stat.get("share") or 0),
            "like": int(stat.get("like") or 0),
        },
    }


def list_account_archives(page: int = 1, page_size: int = 20, status: str = "is_pubed") -> dict[str, Any]:
    credentials = database.get_bilibili_credentials()
    if credentials is None:
        raise RuntimeError("Bilibili credentials are not configured. Scan QR code first.")
    publisher = BilibiliPublisher(credentials)
    publisher.login_by_stored_credentials()
    data = publisher.list_archives(status=status, page=page, page_size=page_size).get("data") or {}
    items = data.get("arc_audits") or data.get("archives") or []
    page_info = data.get("page") if isinstance(data.get("page"), dict) else {}
    return {
        "archives": [_normalize_archive(item) for item in items if isinstance(item, dict)],
        "page": {
            "page": int(page_info.get("pn") or page),
            "page_size": int(page_info.get("ps") or page_size),
            "total": int(page_info.get("count") or 0),
        },
    }


def _flatten_partition(item: dict[str, Any], parent_name: str = "") -> list[dict[str, Any]]:
    current_name = str(item.get("name") or "")
    children = item.get("children")
    if isinstance(children, list) and children:
        rows: list[dict[str, Any]] = []
        for child in children:
            if isinstance(child, dict):
                rows.extend(_flatten_partition(child, current_name))
        return rows
    try:
        tid = int(item.get("id"))
    except (TypeError, ValueError):
        return []
    return [
        {
            "id": tid,
            "name": current_name,
            "parent_name": parent_name,
            "label": f"{parent_name} / {current_name}" if parent_name else current_name,
            "description": str(item.get("description") or item.get("desc") or ""),
        }
    ]


def list_partitions() -> list[dict[str, Any]]:
    credentials = database.get_bilibili_credentials()
    if credentials is None:
        raise RuntimeError("Bilibili credentials are not configured. Scan QR code first.")
    publisher = BilibiliPublisher(credentials)
    publisher.login_by_stored_credentials()
    data = publisher.archive_pre().get("data") or {}
    typelist = data.get("typelist")
    if not isinstance(typelist, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in typelist:
        if isinstance(item, dict):
            rows.extend(_flatten_partition(item))
    rows.sort(key=lambda row: row["label"])
    return rows


def list_publish_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for task in database.list_bilibili_candidate_tasks():
        session_path = task.get("session_path")
        session = Path(str(session_path)) if session_path else None
        status = load_publish_status(session) if session and session.exists() else None
        final_video = str(task.get("final_video_path") or "")
        final_video_configured = bool(final_video)
        final_video_available = bool(final_video and Path(final_video).exists())
        if not status and not final_video_configured:
            continue

        metadata = load_artifact(session) if session and session.exists() else None
        try:
            fields = _publish_fields(session, detect_source(task["url"])) if session and session.exists() else {}
        except Exception:
            fields = {}
        title = (
            (status or {}).get("title")
            or fields.get("title")
            or task.get("title")
            or task.get("url")
            or task["id"]
        )
        thumbnail_file = str((metadata or {}).get("thumbnail_file") or "")
        thumbnail_api_url = ""
        if thumbnail_file and Path(thumbnail_file).exists():
            thumbnail_api_url = f"/api/tasks/{task['id']}/artifact/thumbnail"
        publish_status = (status or {}).get("status") or "draft"
        record_type = "published" if publish_status == "succeeded" else "draft"
        result_ids = _result_ids((status or {}).get("result"))
        records.append(
            {
                "task_id": task["id"],
                "task_status": task.get("status"),
                "type": record_type,
                "publish_status": publish_status,
                "progress": int((status or {}).get("progress") or 0),
                "message": (status or {}).get("message") or "",
                "error": (status or {}).get("error") or "",
                "title": str(title),
                "description": (status or {}).get("description") or fields.get("description") or "",
                "tags": (status or {}).get("tags") or fields.get("tags") or (metadata or {}).get("translated_tags") or [],
                "source": (status or {}).get("source") or fields.get("source") or "",
                "tid": int((status or {}).get("tid") or fields.get("tid") or bilibili_publish_defaults()["tid"]),
                "thumbnail_api_url": thumbnail_api_url,
                "final_video_available": final_video_available,
                "created_at": task.get("created_at"),
                "completed_at": task.get("completed_at"),
                "updated_at": _status_updated_at(session) if session else None,
                "draft_url": BILIBILI_UPLOAD_MANAGER_URL,
                **result_ids,
            }
        )
    return records


def publish_to_bilibili(
    video_file: Path,
    session: Path,
    source: SourceConfig,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields = _publish_fields(session, source, overrides)
    status: dict[str, Any] = {
        "status": "running",
        "progress": 0,
        "message": "Preparing Bilibili publish",
        "title": fields["title"],
        "description": fields["description"],
        "tags": fields["tags"],
        "source": fields["source"],
        "video_file": str(video_file),
        "result": None,
        "error": "",
    }

    def progress(value: int, message: str) -> None:
        status.update({"progress": value, "message": message})
        write_publish_status(session, status)

    write_publish_status(session, status)
    credentials = database.get_bilibili_credentials()
    if credentials is None:
        status.update(
            {
                "status": "failed",
                "message": "Bilibili publish failed",
                "error": "Bilibili credentials are not configured. Scan QR code first.",
            }
        )
        return write_publish_status(session, status)
    publisher = BilibiliPublisher(credentials, progress_callback=progress)
    try:
        progress(1, "Validating Bilibili credentials")
        publisher.login_by_stored_credentials()
        cover = publisher.upload_cover(fields["cover_file"])
        if cover:
            progress(8, "Uploaded Bilibili cover")
        video_part = publisher.upload_video(video_file, line=fields["line"], threads=fields["threads"])
        progress(85, "Submitting Bilibili archive")
        post_data = PublishData(
            copyright=fields["copyright"],
            source=fields["source"],
            tid=fields["tid"],
            cover=cover,
            title=fields["title"],
            desc=fields["description"],
            desc_v2=[{"raw_text": fields["description"], "biz_id": "", "type": 1}],
            tag=",".join(fields["tags"]),
            videos=[video_part],
        )
        result = publisher.submit(post_data)
        status.update(
            {
                "status": "succeeded",
                "progress": 100,
                "message": "Published to Bilibili",
                "result": result,
            }
        )
    except Exception as exc:
        status.update({"status": "failed", "message": "Bilibili publish failed", "error": str(exc)})
    return write_publish_status(session, status)
