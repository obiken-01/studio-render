import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from app.errors import ApiError
from app.models import FileMeta

MIN_BEATS = 4
_CACHE_SIZE = 8


@dataclass
class BeatAnalysis:
    beats: list[float]
    tempo: float
    duration_sec: float


def _tempo(beats: list[float], fallback: float) -> float:
    """librosa's tempo is quantised to its analysis frames, so report the average pace of the
    beats it actually found (first to last) instead."""
    span = beats[-1] - beats[0]
    return round(60.0 * (len(beats) - 1) / span, 2) if span > 0 else fallback


def analyze(path: Path) -> BeatAnalysis:
    # librosa is slow to import, so it is loaded only when a song is actually analysed.
    import librosa
    import numpy as np

    try:
        y, sr = librosa.load(str(path), sr=22050, mono=True)
    except Exception:
        raise ApiError(
            422, "unreadable_file",
            "This file can't be read as a video, photo or audio file.") from None
    tempo, times = librosa.beat.beat_track(y=y, sr=sr, units="time")
    beats = [round(float(b), 3) for b in np.atleast_1d(times)]
    if len(beats) < MIN_BEATS:
        raise ApiError(422, "no_beats", "No clear beat was found in this song. Try another track.")
    return BeatAnalysis(
        beats=beats,
        tempo=_tempo(beats, float(np.atleast_1d(tempo)[0])),
        duration_sec=round(len(y) / sr, 3),
    )


_cache: OrderedDict[tuple[str, int, float], BeatAnalysis] = OrderedDict()
_cache_lock = threading.Lock()


def analyze_cached(store_file: FileMeta) -> BeatAnalysis:
    """Memoised per file. The key includes size and creation time so a deleted file's id
    can never serve stale beats, and the cache holds at most a few songs."""
    key = (store_file.file_id, store_file.size, store_file.created_at)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    result = analyze(store_file.path)
    with _cache_lock:
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return result
