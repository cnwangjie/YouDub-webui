from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageSpec:
    name: str
    label: str


STAGES: tuple[StageSpec, ...] = (
    StageSpec("download", "Download"),
    StageSpec("separate", "Demucs"),
    StageSpec("asr", "Whisper"),
    StageSpec("asr_fix", "Split sentences"),
    StageSpec("translate", "Translate"),
    StageSpec("split_audio", "Split audio"),
    StageSpec("tts", "VoxCPM"),
    StageSpec("merge_audio", "Merge audio"),
    StageSpec("merge_video", "Merge video"),
)


STAGE_NAMES = tuple(stage.name for stage in STAGES)

STAGE_RESOURCES = {
    "download": "io",
    "translate": "io",
    "separate": "compute",
    "asr": "compute",
    "asr_fix": "compute",
    "split_audio": "compute",
    "tts": "compute",
    "merge_audio": "compute",
    "merge_video": "compute",
}
