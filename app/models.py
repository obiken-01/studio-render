from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class Upload:
    upload_id: str
    filename: str
    size: int
    kind: Literal["video", "photo", "audio"]
    ext: str
    chunk_size: int
    total_chunks: int
    received: set[int]
    created_at: float


@dataclass
class FileMeta:
    file_id: str
    kind: Literal["video", "photo", "audio"]
    path: Path
    filename: str
    size: int
    duration_sec: float | None
    width: int | None  # display dimensions, after rotation
    height: int | None
    fps: float | None
    rotation: int
    created_at: float

    def public(self) -> dict:
        return {
            "fileId": self.file_id,
            "kind": self.kind,
            "filename": self.filename,
            "size": self.size,
            "durationSec": self.duration_sec,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "rotation": self.rotation,
        }
