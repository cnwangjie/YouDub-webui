from __future__ import annotations

import json

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
