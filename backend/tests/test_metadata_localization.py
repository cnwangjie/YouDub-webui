from __future__ import annotations

import json
from base64 import b64encode
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from backend.app.adapters import metadata_localization
from backend.app.adapters import bilibili_publish
from backend.app.sources import detect_source


def test_localize_metadata_uses_metadata_model(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata = session / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "ytdlp_info.json").write_text(
        json.dumps(
            {
                "title": "Title",
                "description": "Description",
                "tags": ["AI"],
                "uploader": "Author",
                "upload_date": "20260705",
                "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk",
            }
        ),
        encoding="utf-8",
    )
    seen: dict[str, str] = {}

    monkeypatch.setattr(
        metadata_localization,
        "get_openai_settings",
        lambda: {
            "base_url": "https://example.com/v1",
            "api_key": "sk-test",
            "model": "main-model",
            "metadata_model": "metadata-model",
            "thumbnail_prompt_model": "thumbnail-prompt-model",
        },
    )
    monkeypatch.setattr(metadata_localization, "_client", lambda base_url, api_key: object())

    def fake_call_json(client, model, system, user):
        seen["model"] = model
        return {
            "translated_title": "标题",
            "translated_description": "简介",
            "translated_tags": ["人工智能"],
            "bilibili_title": "【中配】标题 - Author",
            "bilibili_description": "适合 Bilibili 的正文",
            "bilibili_tags": ["人工智能", "科技"],
            "bilibili_tid": 201,
        }

    monkeypatch.setattr(metadata_localization, "_call_json", fake_call_json)
    monkeypatch.setattr(metadata_localization, "download_thumbnail", lambda session, url: None)
    monkeypatch.setattr(
        metadata_localization,
        "translate_thumbnail",
        lambda *args, **kwargs: (None, {}),
    )

    payload = metadata_localization.localize_metadata(
        session,
        detect_source("https://www.youtube.com/watch?v=abcdefghijk"),
        partition_options=[{"id": 201, "label": "科技 / 计算机技术", "description": ""}],
    )

    assert seen["model"] == "metadata-model"
    assert payload["model"] == "metadata-model"
    assert payload["bilibili"]["title"] == "【中配】标题 - Author"
    assert payload["bilibili"]["tags"] == ["人工智能", "科技"]
    assert payload["bilibili"]["tid"] == 201
    assert payload["bilibili"]["partition_label"] == "科技 / 计算机技术"
    assert payload["bilibili"]["description"] == (
        "原视频标题：Title\n"
        "原作者：Author\n"
        "发布日期：2026-07-05\n"
        "视频链接：https://www.youtube.com/watch?v=abcdefghijk\n"
        "\n"
        "适合 Bilibili 的正文"
    )


def test_bilibili_publish_fields_use_localized_publish_metadata(tmp_path):
    session = tmp_path / "session"
    metadata = session / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "ytdlp_info.json").write_text(
        json.dumps(
            {
                "title": "Original",
                "uploader": "Author",
                "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk",
            }
        ),
        encoding="utf-8",
    )
    (metadata / "localized_metadata.json").write_text(
        json.dumps(
            {
                "title": "Original",
                "description": "",
                "tags": ["AI"],
                "translated_title": "翻译标题",
                "translated_description": "",
                "translated_tags": ["人工智能"],
                "thumbnail_file": "",
                "bilibili": {
                    "title": "【中配】翻译标题 - Author",
                    "description": "Bilibili description",
                    "tags": ["人工智能", "科技"],
                    "tid": 201,
                    "source": "https://www.youtube.com/watch?v=abcdefghijk",
                },
            }
        ),
        encoding="utf-8",
    )

    fields = bilibili_publish._publish_fields(
        session,
        detect_source("https://www.youtube.com/watch?v=abcdefghijk"),
    )

    assert fields["title"] == "【中配】翻译标题 - Author"
    assert fields["description"] == "Bilibili description"
    assert fields["tags"] == ["人工智能", "科技"]
    assert fields["tid"] == 201
    assert fields["copyright"] == 1

    repost_fields = bilibili_publish._publish_fields(
        session,
        detect_source("https://www.youtube.com/watch?v=abcdefghijk"),
        {"copyright": 2},
    )
    assert repost_fields["copyright"] == 2


def test_generate_thumbnail_edits_source_image(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata = session / "metadata"
    media = session / "media"
    metadata.mkdir(parents=True)
    media.mkdir(parents=True)
    source_thumbnail = media / "thumbnail.jpg"
    from PIL import Image

    image = Image.new("RGB", (16, 9), "red")
    image.save(source_thumbnail)
    (metadata / "ytdlp_info.json").write_text(
        json.dumps({"title": "Original", "description": "Description", "tags": ["AI"]}),
        encoding="utf-8",
    )
    (metadata / "localized_metadata.json").write_text(
        json.dumps(
            {
                "title": "Original",
                "description": "Description",
                "tags": ["AI"],
                "thumbnail_url": "",
                "source_thumbnail_file": str(source_thumbnail),
                "thumbnail_file": str(source_thumbnail),
                "translated_thumbnail_file": "",
                "generated_thumbnail_file": "",
                "thumbnail_translation": {},
                "thumbnail_generation": {},
                "translated_title": "翻译标题",
                "translated_description": "翻译简介",
                "translated_tags": ["人工智能"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        metadata_localization,
        "get_openai_settings",
        lambda: {
            "base_url": "https://example.com/v1",
            "api_key": "sk-test",
            "model": "main-model",
            "metadata_model": "metadata-model",
            "thumbnail_prompt_model": "thumbnail-prompt-model",
        },
    )
    monkeypatch.setattr(
        metadata_localization,
        "openai_image_defaults",
        lambda: {
            "base_url": "https://image.example/v1",
            "api_key": "sk-image",
            "model": "gpt-image-2",
        },
    )
    monkeypatch.setattr(metadata_localization, "_client", lambda base_url, api_key: object())
    monkeypatch.setattr(
        metadata_localization,
        "_call_json",
        lambda *args, **kwargs: {"prompt": "Edit the supplied thumbnail and translate visible text to Chinese."},
    )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = b64encode(buffer.getvalue()).decode("ascii")
    captured: dict[str, object] = {}

    class FakeImages:
        def edit(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.images = FakeImages()

    monkeypatch.setattr(metadata_localization, "OpenAI", FakeOpenAI)

    payload = metadata_localization.generate_thumbnail(
        session,
        detect_source("https://www.youtube.com/watch?v=abcdefghijk"),
    )

    assert captured["model"] == "gpt-image-2"
    assert captured["prompt"] == "Edit the supplied thumbnail and translate visible text to Chinese."
    assert captured["image"] is not None
    assert captured["input_fidelity"] == "high"
    assert captured["output_format"] == "png"
    assert Path(payload["thumbnail_file"]).exists()
    assert Path(payload["thumbnail_file"]).name == "thumbnail.localized.png"
    assert payload["translated_thumbnail_file"] == payload["thumbnail_file"]
    assert payload["thumbnail_generation"]["mode"] == "edit"
    assert payload["thumbnail_generation"]["prompt_model"] == "thumbnail-prompt-model"


def test_generate_thumbnail_supports_openrouter_image_api(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata = session / "metadata"
    media = session / "media"
    metadata.mkdir(parents=True)
    media.mkdir(parents=True)
    source_thumbnail = media / "thumbnail.jpg"
    from PIL import Image

    image = Image.new("RGB", (16, 9), "blue")
    image.save(source_thumbnail)
    (metadata / "ytdlp_info.json").write_text(
        json.dumps({"title": "Original", "description": "Description", "tags": ["AI"]}),
        encoding="utf-8",
    )
    (metadata / "localized_metadata.json").write_text(
        json.dumps(
            {
                "title": "Original",
                "description": "Description",
                "tags": ["AI"],
                "thumbnail_url": "",
                "source_thumbnail_file": str(source_thumbnail),
                "thumbnail_file": str(source_thumbnail),
                "translated_thumbnail_file": "",
                "thumbnail_translation": {},
                "thumbnail_generation": {},
                "translated_title": "翻译标题",
                "translated_description": "翻译简介",
                "translated_tags": ["人工智能"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        metadata_localization,
        "openai_image_defaults",
        lambda: {
            "base_url": "https://openrouter.ai/api/v1/images",
            "api_key": "sk-or-test",
            "model": "openai/gpt-image-2",
        },
    )
    monkeypatch.setattr(
        metadata_localization,
        "get_openai_settings",
        lambda: {
            "base_url": "https://example.com/v1",
            "api_key": "sk-test",
            "model": "main-model",
            "metadata_model": "metadata-model",
            "thumbnail_prompt_model": "thumbnail-prompt-model",
        },
    )
    monkeypatch.setattr(metadata_localization, "_client", lambda base_url, api_key: object())
    monkeypatch.setattr(
        metadata_localization,
        "_call_json",
        lambda *args, **kwargs: {"prompt": "Preserve the reference thumbnail and localize visible text."},
    )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = b64encode(buffer.getvalue()).decode("ascii")
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"b64_json": encoded, "media_type": "image/png"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(metadata_localization.requests, "post", fake_post)

    payload = metadata_localization.generate_thumbnail(
        session,
        detect_source("https://www.youtube.com/watch?v=abcdefghijk"),
    )

    request_json = captured["json"]
    assert captured["url"] == "https://openrouter.ai/api/v1/images"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
    assert request_json["model"] == "openai/gpt-image-2"
    assert request_json["prompt"] == "Preserve the reference thumbnail and localize visible text."
    assert request_json["aspect_ratio"] == "16:9"
    assert request_json["input_references"][0]["image_url"]["url"].startswith("data:image/")
    assert Path(payload["thumbnail_file"]).name == "thumbnail.localized.png"
    assert payload["thumbnail_translation"]["provider"] == "openrouter"
    assert payload["thumbnail_generation"]["prompt_model"] == "thumbnail-prompt-model"
