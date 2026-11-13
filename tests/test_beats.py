import pytest

from app import beats as beats_mod
from app.beats import analyze, analyze_cached
from app.errors import ApiError
from app.models import FileMeta
from tests.media import make_click_track, make_silence


def test_click_track_tempo(tmp_path):
    a = analyze(make_click_track(tmp_path / "c.wav", bpm=120, dur=30))
    assert abs(a.tempo - 120) <= 2 and len(a.beats) >= 50 and abs(a.duration_sec - 30) < 0.1
    assert all(round(b, 3) == b for b in a.beats)


def test_silence_has_no_beats(tmp_path):  # Review Focus 2
    with pytest.raises(ApiError) as e:
        analyze(make_silence(tmp_path / "s.wav"))
    assert e.value.code == "no_beats"
    assert e.value.status == 422


def _meta(path, file_id="up_a", size=1, created_at=1.0):
    return FileMeta(file_id=file_id, kind="audio", path=path, filename="c.wav", size=size,
                    duration_sec=None, width=None, height=None, fps=None, rotation=0,
                    created_at=created_at)


def test_cache_reuses_result_and_is_keyed_by_file_identity(tmp_path, monkeypatch):
    path = make_click_track(tmp_path / "c.wav", bpm=120, dur=8)
    calls = []
    real = beats_mod.analyze
    monkeypatch.setattr(beats_mod, "analyze", lambda p: calls.append(p) or real(p))
    beats_mod._cache.clear()
    first = analyze_cached(_meta(path))
    assert analyze_cached(_meta(path)) is first and len(calls) == 1
    # Same id but a different upload (new created_at): must not serve the old result.
    analyze_cached(_meta(path, created_at=2.0))
    assert len(calls) == 2


def test_cache_is_bounded(tmp_path, monkeypatch):
    fake = beats_mod.BeatAnalysis([0.0, 1.0, 2.0, 3.0], 60.0, 4.0)
    monkeypatch.setattr(beats_mod, "analyze", lambda p: fake)
    beats_mod._cache.clear()
    for i in range(20):
        analyze_cached(_meta(tmp_path / "x.wav", file_id=f"up_{i}"))
    assert len(beats_mod._cache) == 8
