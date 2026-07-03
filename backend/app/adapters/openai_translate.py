from __future__ import annotations

import json
import logging
import os
import queue
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable

import httpx
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from ..sources import SourceConfig
from ._translate_prompts import PREPROCESS_PROMPT, TRANSLATE_RULES
from .openai_client import normalize_openai_base_url

log = logging.getLogger(__name__)

API_SETTING_KEYS = ("base_url", "api_key", "model")
PREPROCESS_RETRY = 2
TRANSLATE_RETRY = 2
DESCRIPTION_LIMIT = 500
DEFAULT_CONCURRENCY = 50
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60.0
ProgressCallback = Callable[[int, int, str], None]
LogCallback = Callable[[str], None]


class HotwordItem(BaseModel):
    src: str
    dst: str


class CorrectionItem(BaseModel):
    wrong: str
    correct: str


class PreprocessResponse(BaseModel):
    summary: str = ""
    hotwords: list[HotwordItem] = Field(default_factory=list)
    corrections: list[CorrectionItem] = Field(default_factory=list)


class TranslationItem(BaseModel):
    dst: str


def list_models(*, base_url: str, api_key: str) -> list[str]:
    if not api_key:
        raise ValueError("OpenAI API key is not configured.")
    timeout = _call_timeout()
    client = OpenAI(
        api_key=api_key,
        base_url=normalize_openai_base_url(base_url),
        timeout=httpx.Timeout(timeout),
        max_retries=0,
    )
    response = client.models.list()
    seen: set[str] = set()
    models: list[str] = []
    for item in response.data:
        model_id = getattr(item, "id", "")
        if model_id and model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return models


def _client(base_url: str, api_key: str) -> OpenAI:
    if not api_key:
        raise ValueError("OpenAI API key is not configured.")
    timeout = _call_timeout()
    return OpenAI(
        api_key=api_key,
        base_url=normalize_openai_base_url(base_url),
        timeout=httpx.Timeout(timeout),
        max_retries=0,
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        raise json.JSONDecodeError(f"no JSON object found; raw[:300]={raw[:300]!r}", raw, 0)
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(
            f"{exc.msg}; len={len(raw)}; raw[:300]={raw[:300]!r}; raw[-200:]={raw[-200:]!r}",
            raw,
            exc.pos,
        ) from None


def _call_timeout() -> float:
    raw = os.getenv("OPENAI_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_OPENAI_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        log.warning("Invalid OPENAI_TIMEOUT_SECONDS=%r, using %.1fs", raw, DEFAULT_OPENAI_TIMEOUT_SECONDS)
        return DEFAULT_OPENAI_TIMEOUT_SECONDS
    if timeout <= 0:
        log.warning("Invalid OPENAI_TIMEOUT_SECONDS=%r, using %.1fs", raw, DEFAULT_OPENAI_TIMEOUT_SECONDS)
        return DEFAULT_OPENAI_TIMEOUT_SECONDS
    return timeout


def _call_json(
    client: OpenAI,
    model: str,
    system: str,
    user: str,
    log_callback: LogCallback | None = None,
    log_prefix: str = "OpenAI",
) -> dict[str, Any]:
    timeout = _call_timeout()
    result: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)

    def request() -> None:
        try:
            started = False
            stream = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                stream=True,
                timeout=timeout,
            )
            chunks: list[str] = []
            chunk_count = 0
            for event in stream:
                delta = event.choices[0].delta.content or ""
                if delta:
                    if not started and log_callback:
                        log_callback(f"{log_prefix} stream received first content chunk")
                    started = True
                    chunk_count += 1
                    chunks.append(delta)
            if log_callback:
                log_callback(
                    f"{log_prefix} stream completed: {chunk_count} content chunks, "
                    f"{sum(len(chunk) for chunk in chunks)} chars"
                )
            result.put(("ok", "".join(chunks)))
        except Exception as exc:
            result.put(("error", exc))

    thread = Thread(target=request, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        if log_callback:
            log_callback(f"{log_prefix} stream timed out after {timeout:g}s")
        raise TimeoutError(f"OpenAI chat completion timed out after {timeout:g}s")

    status, value = result.get_nowait()
    if status == "error":
        raise value
    raw = value or "{}"
    return _extract_json(raw)


def _format_terms(items: list, fmt: str, empty: str) -> str:
    if not items:
        return empty
    return "\n".join(fmt.format(**item.model_dump()) for item in items)


def _meta_view(meta: dict[str, Any]) -> dict[str, str]:
    description = (meta.get("description") or "").strip()
    if len(description) > DESCRIPTION_LIMIT:
        description = description[:DESCRIPTION_LIMIT] + "..."
    return {
        "title": str(meta.get("title") or "").strip() or "(unknown)",
        "uploader": str(meta.get("uploader") or "").strip() or "(unknown)",
        "description": description or "(none)",
    }


def preprocess(
    full_text: str,
    meta: dict[str, Any],
    source: SourceConfig,
    *,
    base_url: str,
    api_key: str,
    model: str,
    log_callback: LogCallback | None = None,
) -> PreprocessResponse:
    user = PREPROCESS_PROMPT.format(
        src_language_name=source.asr_language_name,
        dst_language_name=source.target_language_name,
        full_text=full_text,
        **_meta_view(meta),
    )
    if log_callback:
        log_callback(
            "Preprocess request parameters:\n"
            + json.dumps(
                {
                    "base_url": normalize_openai_base_url(base_url),
                    "model": model,
                    "timeout_seconds": _call_timeout(),
                    "system": "You output strict JSON only.",
                    "user": user,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    client = _client(base_url, api_key)
    last_error: Exception | None = None
    for attempt in range(PREPROCESS_RETRY + 1):
        try:
            if log_callback:
                log_callback(f"Preprocess request attempt {attempt + 1}/{PREPROCESS_RETRY + 1}")
            if log_callback:
                data = _call_json(
                    client,
                    model,
                    "You output strict JSON only.",
                    user,
                    log_callback=log_callback,
                    log_prefix="Preprocess",
                )
            else:
                data = _call_json(client, model, "You output strict JSON only.", user)
            if log_callback:
                log_callback("Preprocess request completed")
            return PreprocessResponse.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            log.warning("preprocess attempt %d failed: %s", attempt + 1, exc)
            if log_callback:
                log_callback(f"Preprocess response validation failed on attempt {attempt + 1}: {exc}")
        except TimeoutError as exc:
            if log_callback:
                log_callback(f"Preprocess request timed out on attempt {attempt + 1}: {exc}")
            raise
        except Exception as exc:
            if log_callback:
                log_callback(f"Preprocess request failed on attempt {attempt + 1}: {exc}")
            raise
    log.error("preprocess gave up, returning empty: %s", last_error)
    if log_callback:
        log_callback("Preprocess gave up after invalid responses; continuing with empty preprocess data")
    return PreprocessResponse()


def _translate_system(source: SourceConfig, meta: dict[str, Any], pre: PreprocessResponse) -> str:
    rules = TRANSLATE_RULES[source.target_language]
    return rules.format(
        summary=pre.summary or "(none)",
        hotwords=_format_terms(pre.hotwords, "{src} -> {dst}", "(none)"),
        corrections=_format_terms(pre.corrections, "{wrong} -> {correct}", "(none)"),
        **_meta_view(meta),
    )


def _post_process(text: str, target_language: str) -> str:
    cleaned = text.strip()
    if target_language == "zh":
        cleaned = cleaned.replace("——", "，")
    return cleaned


def translate_sentence(
    text: str,
    target_language: str,
    client: OpenAI,
    model: str,
    system: str,
) -> str:
    last_error: Exception | None = None
    for attempt in range(TRANSLATE_RETRY):
        try:
            data = _call_json(client, model, system, text)
            item = TranslationItem.model_validate(data)
            if not item.dst.strip():
                raise ValueError("empty dst")
            return _post_process(item.dst, target_language)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc
            log.warning("translate attempt %d failed for %r: %s", attempt + 1, text[:60], exc)
    raise RuntimeError(f"translate_sentence failed after {TRANSLATE_RETRY} attempts: {last_error}")


def translate_batch(
    texts: list[str],
    source: SourceConfig,
    meta: dict[str, Any],
    pre: PreprocessResponse,
    *,
    base_url: str,
    api_key: str,
    model: str,
    concurrency: int = DEFAULT_CONCURRENCY,
    cached_translations: dict[int, str] | None = None,
    cache_callback: Callable[[int, str], None] | None = None,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
) -> list[str]:
    if not texts:
        if log_callback:
            log_callback("No sentences to translate")
        return []
    if log_callback:
        log_callback("Building translation system prompt")
    system = _translate_system(source, meta, pre)
    if log_callback:
        log_callback("Creating OpenAI client for translation batch")
    client = _client(base_url, api_key)
    cached = cached_translations or {}
    results: list[str | None] = [cached.get(index) for index in range(len(texts))]
    pending = [index for index, value in enumerate(results) if value is None]
    done = len(texts) - len(pending)
    log.info(
        "translate_batch: %d sentences, cached=%d, remaining=%d, concurrency=%d",
        len(texts),
        done,
        len(pending),
        concurrency,
    )
    if log_callback:
        log_callback(
            f"Translation batch prepared: {len(texts)} sentences, "
            f"{done} cached, {len(pending)} remaining, concurrency={concurrency}"
        )
    if not pending:
        if log_callback:
            log_callback("All sentences already available from partial cache")
        return [value or "" for value in results]
    if log_callback:
        log_callback(f"Submitting {len(pending)} translation requests")
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {
            pool.submit(
                translate_sentence,
                texts[index],
                source.target_language,
                client,
                model,
                system,
            ): index
            for index in pending
        }
        for future in as_completed(futures):
            index = futures[future]
            if log_callback:
                log_callback(f"Translation request completed for sentence {index + 1}/{len(texts)}")
            dst = future.result()
            results[index] = dst
            done += 1
            if cache_callback:
                cache_callback(index, dst)
                if log_callback:
                    log_callback(f"Cached sentence {index + 1}/{len(texts)} to partial translation file")
            if progress_callback:
                progress_callback(done, len(texts), f"Translated {done}/{len(texts)} sentences")
    if log_callback:
        log_callback("Translation batch completed")
    return [value or "" for value in results]


def _read_meta(session: Path) -> dict[str, Any]:
    info_file = session / "metadata" / "ytdlp_info.json"
    if not info_file.exists():
        return {}
    return json.loads(info_file.read_text(encoding="utf-8"))


def _speaker(utt: dict[str, Any]) -> str:
    additions = utt.get("additions") or {}
    if isinstance(additions, dict):
        return str(additions.get("speaker") or "1")
    return "1"


def _full_text(data: dict[str, Any], texts: list[str]) -> str:
    raw = data.get("result", {}).get("text") or ""
    if raw.strip():
        return raw
    return " ".join(texts)


def preprocess_artifact_path(session: Path) -> Path:
    return session / "metadata" / "translation_preprocess.json"


def partial_translation_path(session: Path, target_language: str) -> Path:
    return session / "metadata" / f"translation_partial.{target_language}.json"


def write_preprocess_artifact(session: Path, pre: PreprocessResponse) -> Path:
    path = preprocess_artifact_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pre.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_preprocess_artifact(session: Path) -> PreprocessResponse | None:
    path = preprocess_artifact_path(session)
    if not path.exists():
        return None
    return PreprocessResponse.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_partial_translation(path: Path, texts: list[str]) -> dict[int, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Ignoring unreadable translation partial cache: %s", path)
        return {}
    cached: dict[int, str] = {}
    for item in data.get("items") or []:
        try:
            index = int(item["index"])
            src = str(item["src"])
            dst = str(item["dst"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= index < len(texts) and texts[index] == src and dst.strip():
            cached[index] = dst
    return cached


def write_partial_translation(path: Path, texts: list[str], translations: dict[int, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "items": [
            {"index": index, "src": texts[index], "dst": translations[index]}
            for index in sorted(translations)
            if 0 <= index < len(texts)
        ]
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _concurrency_from(settings: dict[str, str]) -> int:
    raw = str(settings.get("translate_concurrency") or "").strip()
    if not raw or not all("0" <= char <= "9" for char in raw):
        return DEFAULT_CONCURRENCY
    concurrency = int(raw)
    if concurrency < 1 or concurrency > 200:
        return DEFAULT_CONCURRENCY
    return concurrency


def translate_asr(
    asr_file: Path,
    session: Path,
    settings: dict[str, str],
    source: SourceConfig,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
) -> Path:
    output_file = session / "metadata" / f"translation.{source.target_language}.json"
    if log_callback:
        log_callback(f"Checking final translation artifact: {output_file}")
    if output_file.exists():
        if log_callback:
            log_callback(f"Final translation artifact already exists, reusing: {output_file}")
        return output_file

    if log_callback:
        log_callback(f"Reading ASR sentences from {asr_file}")
    data = json.loads(asr_file.read_text(encoding="utf-8"))
    utterances = data["result"]["utterances"]
    texts = [u["text"].strip() for u in utterances]
    full_text = _full_text(data, texts)
    if log_callback:
        log_callback(f"Loaded {len(texts)} ASR sentences")
        log_callback("Reading source metadata")
    meta = _read_meta(session)

    api = {key: settings[key] for key in API_SETTING_KEYS if key in settings}
    if log_callback:
        log_callback("Checking translation preprocess artifact")
    pre = load_preprocess_artifact(session)
    if pre is None:
        if log_callback:
            log_callback("Running translation preprocess request")
        pre = preprocess(full_text, meta, source, **api, log_callback=log_callback)
        if log_callback:
            log_callback("Writing translation preprocess artifact")
        write_preprocess_artifact(session, pre)
        log.info("Wrote translation preprocess artifact to %s", preprocess_artifact_path(session))
        if log_callback:
            log_callback(f"Wrote translation preprocess artifact: {preprocess_artifact_path(session)}")
    else:
        log.info("Reusing translation preprocess artifact from %s", preprocess_artifact_path(session))
        if log_callback:
            log_callback(f"Reusing translation preprocess artifact: {preprocess_artifact_path(session)}")
    partial_file = partial_translation_path(session, source.target_language)
    if log_callback:
        log_callback(f"Loading partial translation cache: {partial_file}")
    cached_translations = load_partial_translation(partial_file, texts)
    if log_callback:
        log_callback(f"Loaded {len(cached_translations)}/{len(texts)} cached translations")
    if cached_translations and progress_callback:
        progress_callback(
            len(cached_translations),
            len(texts),
            f"Reused {len(cached_translations)}/{len(texts)} cached translations",
        )
    partial_lock = Lock()

    def cache_translation(index: int, dst: str) -> None:
        with partial_lock:
            cached_translations[index] = dst
            write_partial_translation(partial_file, texts, cached_translations)

    if log_callback:
        log_callback("Starting sentence translation")
    dst_list = translate_batch(
        texts,
        source,
        meta,
        pre,
        **api,
        concurrency=_concurrency_from(settings),
        cached_translations=cached_translations,
        cache_callback=cache_translation,
        progress_callback=progress_callback,
        log_callback=log_callback,
    )

    if log_callback:
        log_callback("Building final translation payload")
    translation = [
        {
            "src": text,
            "dst": dst,
            "src_lang": source.asr_language,
            "dst_lang": source.target_language,
            "start_time": utt["start_time"],
            "end_time": utt["end_time"],
            "speaker": _speaker(utt),
        }
        for text, dst, utt in zip(texts, dst_list, utterances)
    ]
    output_file.write_text(
        json.dumps({"translation": translation}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if log_callback:
        log_callback(f"Wrote final translation artifact: {output_file}")
    if partial_file.exists():
        partial_file.unlink()
        if log_callback:
            log_callback(f"Removed partial translation cache: {partial_file}")
    return output_file
