"""Persistent user settings (stored as JSON in %APPDATA%/LCClips)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "LCClips"
APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = APP_DIR / "config.json"

DEVICE_DEFAULT = "Default"
DEVICE_NONE = "None (disabled)"

RESOLUTIONS = ["Native", "2160p", "1440p", "1080p", "900p", "720p", "480p"]
ENCODERS = ["auto", "h264_nvenc", "h264_amf", "h264_qsv", "libx264"]


def _default_clips_dir() -> str:
    return str(Path.home() / "Videos" / APP_NAME)


@dataclass
class Config:
    hotkey: str = "alt+f10"
    clip_seconds: int = 30
    monitor: int = 0
    resolution: str = "Native"
    fps: int = 60
    bitrate_mbps: int = 20
    encoder: str = "auto"
    draw_mouse: bool = True
    game_audio_device: str = DEVICE_DEFAULT  # output device to loop back
    mic_device: str = DEVICE_DEFAULT
    game_volume: float = 1.0
    mic_volume: float = 1.0
    audio_offset_ms: int = 0  # +delays audio, -advances it
    output_dir: str = field(default_factory=_default_clips_dir)
    ffmpeg_path: str = ""  # empty = auto-detect
    # Share links (see server/README.md)
    share_server_url: str = ""
    share_token: str = ""
    auto_upload: bool = False
    upload_bitrate_mbps: int = 8  # re-encode for upload; 0 = upload the original file

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            known = {f.name: f.type for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(cfg, k, v)
        except (OSError, ValueError):
            pass
        cfg.clip_seconds = max(5, min(600, int(cfg.clip_seconds)))
        return cfg

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def target_height(self) -> int | None:
        if self.resolution == "Native":
            return None
        return int(self.resolution.rstrip("p"))
