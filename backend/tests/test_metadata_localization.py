from __future__ import annotations

import json

from backend.app.adapters import metadata_localization
from backend.app.sources import detect_source


def test_localize_metadata_uses_metadata_model(monkeypatch, tmp_path):
    session = tmp_path / "session"
    metadata = session / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "ytdlp_info.json").write_text(
        json.dumps({"title": "Title", "description": "Description", "tags": ["AI"]}),
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
    )

    assert seen["model"] == "metadata-model"
    assert payload["model"] == "metadata-model"
