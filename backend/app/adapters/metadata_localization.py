from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from openai import OpenAI

from ..config import bilibili_publish_defaults, openai_image_defaults
from ..database import get_openai_settings
from ..sources import SourceConfig
from .openai_client import normalize_openai_base_url
from .openai_translate import _call_json, _client

log = logging.getLogger(__name__)

DESCRIPTION_LIMIT = 4000
TAG_LIMIT = 40
BILIBILI_TAG_LIMIT = 10
BILIBILI_TITLE_LIMIT = 80
BILIBILI_DESCRIPTION_LIMIT = 2000
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


def _source_author(info: dict[str, Any]) -> str:
    return str(info.get("uploader") or info.get("channel") or info.get("creator") or "").strip()


def _source_url(info: dict[str, Any]) -> str:
    return str(info.get("webpage_url") or info.get("original_url") or info.get("url") or "").strip()


def _format_upload_date(info: dict[str, Any]) -> str:
    raw_date = str(info.get("upload_date") or "").strip()
    if len(raw_date) == 8 and raw_date.isdigit():
        return f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    return raw_date


def _trim(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _dedupe_tags(values: Any, limit: int) -> list[str]:
    raw_values = values if isinstance(values, list) else []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        tag = str(item).strip()
        normalized = tag.lower()
        if not tag or normalized in seen:
            continue
        result.append(tag)
        seen.add(normalized)
        if len(result) >= limit:
            break
    return result


def _partition_options_prompt(partition_options: list[dict[str, Any]]) -> str:
    if not partition_options:
        return "No Bilibili partition options are available. Return null for bilibili_tid."
    rows = []
    for item in partition_options[:120]:
        try:
            tid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        label = str(item.get("label") or item.get("name") or tid).strip()
        description = str(item.get("description") or "").strip()
        rows.append({"id": tid, "label": label, "description": description})
    if not rows:
        return "No Bilibili partition options are available. Return null for bilibili_tid."
    return json.dumps(rows, ensure_ascii=False)


def _partition_label(partition_options: list[dict[str, Any]], tid: int | None) -> str:
    if tid is None:
        return ""
    for item in partition_options:
        try:
            if int(item.get("id")) == tid:
                return str(item.get("label") or item.get("name") or "").strip()
        except (TypeError, ValueError):
            continue
    return ""


def _default_publish_title(title: str, translated_title: str, author: str) -> str:
    publish_title = translated_title or title or "YouDub video"
    value = f"【中配】{publish_title} - {author}" if author else f"【中配】{publish_title}"
    return _trim(value, BILIBILI_TITLE_LIMIT)


def _default_publish_description(
    *,
    title: str,
    author: str,
    upload_date: str,
    source_url: str,
    translated_description: str,
    description: str,
) -> str:
    header = [
        f"原视频标题：{title}" if title else "",
        f"原作者：{author}" if author else "",
        f"发布日期：{upload_date}" if upload_date else "",
        f"视频链接：{source_url}" if source_url else "",
    ]
    body = (translated_description or description).strip()
    parts = [line for line in header if line]
    if body:
        parts.extend(["", body])
    return _trim("\n".join(parts), BILIBILI_DESCRIPTION_LIMIT)


def _publish_description_with_source_header(
    *,
    title: str,
    author: str,
    upload_date: str,
    source_url: str,
    body: str,
    fallback_body: str,
) -> str:
    header = [
        f"原视频标题：{title}" if title else "",
        f"原作者：{author}" if author else "",
        f"发布日期：{upload_date}" if upload_date else "",
        f"视频链接：{source_url}" if source_url else "",
    ]
    parts = [line for line in header if line]
    content = (body or fallback_body).strip()
    if content:
        parts.extend(["", content])
    return _trim("\n".join(parts), BILIBILI_DESCRIPTION_LIMIT)


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
    author: str,
    upload_date: str,
    source_url: str,
    partition_options: list[dict[str, Any]],
) -> str:
    return f"""您的任务是将YouTube视频元数据翻译并本地化到目标语言，以便在哔哩哔哩上发布，需注重本土化表达且内容易于传播。请仔细遵循以下说明：

源语言：{source.asr_language_name}
目标语言：{source.target_language_name}

原始视频元数据：
<original_title>{title}</original_title>
<original_author>{author or "(unknown)"}</original_author>
<original_publish_date>{upload_date or "(unknown)"}</original_publish_date>
<source_video_link>{source_url or "(unknown)"}</source_video_link>
<original_description>{description or "(none)"}</original_description>
<original_tags>{json.dumps(tags, ensure_ascii=False)}</original_tags>

哔哩哔哩分区选项：{_partition_options_prompt(partition_options)}
标签限制：翻译后的标签（最多{TAG_LIMIT}个），哔哩哔哩标签（3-{BILIBILI_TAG_LIMIT}个）

翻译与本地化规则：
1. 保留产品名称、人名、URL、代号、型号和品牌名称，除非存在广泛接受的目标语言名称。
2. 翻译后的标题：简洁、可发布，结合本土化表达增强传播性，不添加超出原意的过度标题党内容。
3. 翻译后的描述：忠实于原文，保留有用的URL和换行，采用符合目标语言用户阅读习惯的表达以提升传播效果。
4. 翻译后的标签：自然翻译，去除重复，结合本土化常用表述，最多{TAG_LIMIT}个标签。
5. 哔哩哔哩标题：如果作者已知，使用格式“【中配】<翻译后的标题> - <原作者>”，标题需兼顾准确性与传播性。
6. 哔哩哔哩描述：整体表述需符合哔哩哔哩用户偏好，增强内容传播力。去除原始描述中的推广链接、联系方式、商业化内容等信息。
7. 哔哩哔哩标签：简洁、适合哔哩哔哩搜索，结合平台热门搜索词及本土化常用标签，3-{BILIBILI_TAG_LIMIT}个标签，无重复或话题标签。
8. 哔哩哔哩tid：从提供的分区选项中选择；如果没有合适的选项或无选项，则返回null。

仅输出严格的JSON，遵循以下模式：
{{
  "translated_title": "<自然的目标语言标题>",
  "translated_description": "<自然的目标语言描述>",
  "translated_tags": ["<目标语言标签>", "..."],
  "bilibili_title": "【中配】<翻译后的标题> - <原作者>",
  "bilibili_description": "<适合哔哩哔哩的描述>",
  "bilibili_tags": ["<推荐的哔哩哔哩标签>", "..."],
  "bilibili_tid": <推荐的分区ID或null>
}}
"""


def localize_metadata(
    session: Path,
    source: SourceConfig,
    partition_options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    info = json.loads(metadata_file(session).read_text(encoding="utf-8"))
    title = str(info.get("title") or "").strip()
    description = _description(info)
    tags = _tags(info)
    author = _source_author(info)
    upload_date = _format_upload_date(info)
    source_url = _source_url(info)
    partition_options = partition_options or []
    thumbnail_url = _thumbnail_url(info)
    thumbnail_file = download_thumbnail(session, thumbnail_url)

    settings = get_openai_settings()
    model = str(settings.get("metadata_model") or "").strip() or settings["model"]
    client = _client(settings["base_url"], settings["api_key"])
    translated = _call_json(
        client,
        model,
        "You output strict JSON only.",
        _translate_prompt(
            source=source,
            title=title,
            description=description,
            tags=tags,
            author=author,
            upload_date=upload_date,
            source_url=source_url,
            partition_options=partition_options,
        ),
    )
    translated_title = str(translated.get("translated_title") or "").strip()
    translated_description = str(translated.get("translated_description") or "").strip()
    translated_tags = _dedupe_tags(translated.get("translated_tags"), TAG_LIMIT)
    bilibili_tags = _dedupe_tags(translated.get("bilibili_tags"), BILIBILI_TAG_LIMIT)
    if not bilibili_tags:
        bilibili_tags = _dedupe_tags(translated_tags or tags, BILIBILI_TAG_LIMIT)
    try:
        bilibili_tid = int(translated.get("bilibili_tid")) if translated.get("bilibili_tid") else None
    except (TypeError, ValueError):
        bilibili_tid = None
    if bilibili_tid is None:
        try:
            bilibili_tid = int(bilibili_publish_defaults()["tid"])
        except (TypeError, ValueError):
            bilibili_tid = None
    bilibili_title = _trim(
        str(translated.get("bilibili_title") or "").strip()
        or _default_publish_title(title, translated_title, author),
        BILIBILI_TITLE_LIMIT,
    )
    bilibili_description = _publish_description_with_source_header(
        title=title,
        author=author,
        upload_date=upload_date,
        source_url=source_url,
        body=str(translated.get("bilibili_description") or "").strip(),
        fallback_body=translated_description or description,
    )
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
        "bilibili": {
            "title": bilibili_title,
            "description": bilibili_description,
            "tags": bilibili_tags,
            "tid": bilibili_tid,
            "partition_label": _partition_label(partition_options, bilibili_tid),
            "source": source_url,
        },
        "model": model,
        "base_url": normalize_openai_base_url(settings["base_url"]),
    }
    path = artifact_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
