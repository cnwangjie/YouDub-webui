from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from openai import OpenAI

from ..config import openai_image_defaults
from ..database import get_openai_settings
from ..sources import SourceConfig
from .openai_client import normalize_openai_base_url
from .openai_translate import _call_json, _client

log = logging.getLogger(__name__)

DESCRIPTION_LIMIT = 4000
TAG_LIMIT = 40
THUMBNAIL_TIMEOUT_SECONDS = 20
IMAGE_EDIT_TIMEOUT_SECONDS = 180


def artifact_path(session: Path) -> Path:
    return session / "metadata" / "localized_metadata.json"


def load_artifact(session: Path) -> dict[str, Any] | None:
    path = artifact_path(session)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def metadata_file(session: Path) -> Path:
    ytdlp = session / "metadata" / "ytdlp_info.json"
    if ytdlp.exists():
        return ytdlp
    local = session / "metadata" / "local_info.json"
    if local.exists():
        return local
    raise FileNotFoundError("Task metadata is not available yet.")


def _description(info: dict[str, Any]) -> str:
    description = str(info.get("description") or "").strip()
    if len(description) > DESCRIPTION_LIMIT:
        return description[:DESCRIPTION_LIMIT] + "..."
    return description


def _tags(info: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("tags", "categories"):
        raw = info.get(key)
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
        elif isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
        if len(result) >= TAG_LIMIT:
            break
    return result


def _thumbnail_url(info: dict[str, Any]) -> str:
    direct = str(info.get("thumbnail") or "").strip()
    if direct:
        return direct
    thumbnails = info.get("thumbnails")
    if not isinstance(thumbnails, list):
        return ""
    candidates = [item for item in thumbnails if isinstance(item, dict) and item.get("url")]
    if not candidates:
        return ""
    candidates.sort(key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
    return str(candidates[-1]["url"]).strip()


def _thumbnail_suffix(url: str, content_type: str) -> str:
    parsed_suffix = Path(urlparse(url).path).suffix.lower()
    if parsed_suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return parsed_suffix
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def download_thumbnail(session: Path, url: str) -> Path | None:
    if not url:
        return None
    try:
        response = requests.get(url, timeout=THUMBNAIL_TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception as exc:
        log.warning("Failed to download thumbnail: %s", exc)
        return None
    suffix = _thumbnail_suffix(url, response.headers.get("content-type", ""))
    path = session / "media" / f"thumbnail{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return path


def _cover_prompt(
    *,
    source: SourceConfig,
    title: str,
    translated_title: str,
    description: str,
    translated_description: str,
    tags: list[str],
) -> str:
    return f"""Edit this video thumbnail for a localized publication.

Goal:
- Translate any visible text in the image from {source.asr_language_name} into {source.target_language_name}.
- Preserve the original composition, subject, style, colors, branding, faces, objects, and overall layout.
- Keep the image usable as a 16:9 video cover.
- Do not add unrelated text, watermarks, logos, badges, QR codes, or extra claims.
- If the image has no readable text, preserve the image and make only minimal layout-safe refinements.
- Make all rendered text crisp and readable.

Localized title for context:
{translated_title or title}

Original title:
{title}

Translated description for context:
{translated_description or description}

Localized tags:
{json.dumps(tags, ensure_ascii=False)}
"""


def translate_thumbnail(
    session: Path,
    thumbnail_file: Path | None,
    *,
    source: SourceConfig,
    title: str,
    translated_title: str,
    description: str,
    translated_description: str,
    translated_tags: list[str],
) -> tuple[Path | None, dict[str, Any]]:
    settings = openai_image_defaults()
    metadata: dict[str, Any] = {
        "model": settings["model"],
        "base_url": normalize_openai_base_url(settings["base_url"]),
        "error": "",
    }
    if not thumbnail_file or not thumbnail_file.exists():
        metadata["error"] = "Thumbnail file is not available."
        return None, metadata
    if not settings["api_key"]:
        metadata["error"] = "OpenAI image API key is not configured."
        return None, metadata

    client = OpenAI(
        api_key=settings["api_key"],
        base_url=normalize_openai_base_url(settings["base_url"]),
        timeout=IMAGE_EDIT_TIMEOUT_SECONDS,
        max_retries=0,
    )
    output_file = session / "media" / "thumbnail.localized.png"
    prompt = _cover_prompt(
        source=source,
        title=title,
        translated_title=translated_title,
        description=description,
        translated_description=translated_description,
        tags=translated_tags,
    )
    try:
        with thumbnail_file.open("rb") as image:
            response = client.images.edit(
                model=settings["model"],
                image=image,
                prompt=prompt,
                size="auto",
                quality="high",
                input_fidelity="high",
                output_format="png",
                response_format="b64_json",
            )
        if not response.data or not response.data[0].b64_json:
            raise RuntimeError("OpenAI image edit response did not include image data.")
        output_file.write_bytes(base64.b64decode(response.data[0].b64_json))
        return output_file, metadata
    except Exception as exc:
        log.warning("Failed to translate thumbnail with OpenAI Images API: %s", exc)
        metadata["error"] = str(exc)
        return None, metadata


def _translate_prompt(
    *,
    source: SourceConfig,
    title: str,
    description: str,
    tags: list[str],
) -> str:
    return f"""Translate and localize video metadata for publishing.
Source language: {source.asr_language_name}
Target language: {source.target_language_name}

Return strict JSON with this schema:
{{
  "translated_title": "<natural target-language title>",
  "translated_description": "<natural target-language description>",
  "translated_tags": ["<target-language tag>", "..."]
}}

Rules:
- Preserve product names, person names, URLs, code names, model names, and brand names unless a widely accepted target-language name exists.
- Keep the title concise and publishable. Do not add clickbait beyond the original meaning.
- Keep the description faithful. Preserve URLs and line breaks when useful.
- Translate tags naturally. Remove duplicates. Keep at most {TAG_LIMIT} tags.
- Output JSON only.

Original title:
{title}

Original description:
{description or "(none)"}

Original tags:
{json.dumps(tags, ensure_ascii=False)}
"""


def localize_metadata(session: Path, source: SourceConfig) -> dict[str, Any]:
    info = json.loads(metadata_file(session).read_text(encoding="utf-8"))
    title = str(info.get("title") or "").strip()
    description = _description(info)
    tags = _tags(info)
    thumbnail_url = _thumbnail_url(info)
    thumbnail_file = download_thumbnail(session, thumbnail_url)

    settings = get_openai_settings()
    client = _client(settings["base_url"], settings["api_key"])
    translated = _call_json(
        client,
        settings["model"],
        "You output strict JSON only.",
        _translate_prompt(source=source, title=title, description=description, tags=tags),
    )
    translated_title = str(translated.get("translated_title") or "").strip()
    translated_description = str(translated.get("translated_description") or "").strip()
    translated_tags = [
        str(item).strip()
        for item in translated.get("translated_tags", [])
        if str(item).strip()
    ][:TAG_LIMIT]
    localized_thumbnail_file, thumbnail_translation = translate_thumbnail(
        session,
        thumbnail_file,
        source=source,
        title=title,
        translated_title=translated_title,
        description=description,
        translated_description=translated_description,
        translated_tags=translated_tags,
    )
    publish_thumbnail_file = localized_thumbnail_file or thumbnail_file
    payload = {
        "title": title,
        "description": description,
        "tags": tags,
        "thumbnail_url": thumbnail_url,
        "source_thumbnail_file": str(thumbnail_file) if thumbnail_file else "",
        "thumbnail_file": str(publish_thumbnail_file) if publish_thumbnail_file else "",
        "translated_thumbnail_file": str(localized_thumbnail_file) if localized_thumbnail_file else "",
        "thumbnail_translation": thumbnail_translation,
        "translated_title": translated_title,
        "translated_description": translated_description,
        "translated_tags": translated_tags,
        "model": settings["model"],
        "base_url": normalize_openai_base_url(settings["base_url"]),
    }
    path = artifact_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
