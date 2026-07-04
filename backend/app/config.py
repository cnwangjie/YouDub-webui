from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
COOKIE_DIR = DATA_DIR / "cookies"
DB_PATH = DATA_DIR / "youdub.sqlite"
YOUTUBE_COOKIE_PATH = COOKIE_DIR / "youtube.txt"
WORKFOLDER = Path(os.getenv("WORKFOLDER", str(REPO_ROOT / "workfolder"))).expanduser()
LOG_DIR = DATA_DIR / "logs"
MODEL_CACHE_DIR = Path(os.getenv("MODEL_CACHE_DIR", str(DATA_DIR / "modelscope"))).expanduser()


def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    WORKFOLDER.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def device() -> str:
    configured = os.getenv("DEVICE") or os.getenv("CUDA_DEVICE")
    if configured:
        return configured
    return "cuda"


def openai_defaults() -> dict[str, str]:
    return {
        "base_url": os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or "https://api.openai.com/v1",
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "model": os.getenv("OPENAI_MODEL") or os.getenv("OPENAI_MODEL_NAME") or "gpt-4o-mini",
        "translate_concurrency": os.getenv("OPENAI_TRANSLATE_CONCURRENCY", "5"),
        "translate_use_batch": os.getenv("OPENAI_TRANSLATE_USE_BATCH", "true"),
    }


def openai_image_defaults() -> dict[str, str]:
    image_api_key = os.getenv("OPENAI_IMAGE_API_KEY")
    return {
        "base_url": os.getenv("OPENAI_IMAGE_BASE_URL", "https://api.openai.com/v1"),
        "api_key": image_api_key if image_api_key is not None else os.getenv("OPENAI_API_KEY", ""),
        "model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2"),
    }


def ffmpeg_binary() -> str:
    return os.getenv("FFMPEG_PATH", "").strip() or "ffmpeg"


def ffprobe_binary() -> str:
    return os.getenv("FFPROBE_PATH", "").strip() or "ffprobe"


def bilibili_publish_defaults() -> dict[str, str]:
    return {
        "tid": os.getenv("BILIBILI_TID", "21"),
        "copyright": os.getenv("BILIBILI_COPYRIGHT", "2"),
        "line": os.getenv("BILIBILI_UPLOAD_LINE", "bda2"),
        "threads": os.getenv("BILIBILI_UPLOAD_THREADS", "3"),
        "source": os.getenv("BILIBILI_SOURCE", ""),
    }


def ytdlp_defaults() -> dict[str, str]:
    return {
        "proxy_port": os.getenv("YTDLP_PROXY_PORT", ""),
    }
