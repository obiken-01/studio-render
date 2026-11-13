import pytest

from app.errors import ApiError
from app.probe import probe_media
from tests.media import make_click_track, make_photo, make_video


def test_probe_video(tmp_path):
    p = make_video(tmp_path / "a.mp4", 1920, 1080, 6.0, 30)
    r = probe_media(p, "video")
    assert (r["width"], r["height"], r["rotation"]) == (1920, 1080, 0)
    assert r["duration_sec"] == pytest.approx(6.0, abs=0.1)
    assert r["fps"] == pytest.approx(30.0)


def test_probe_rotated_reports_display_size(tmp_path):
    p = make_video(tmp_path / "r.mp4", 1920, 1080, 1.0, 30, rotate=90)
    r = probe_media(p, "video")
    assert (r["width"], r["height"]) == (1080, 1920)
    assert r["rotation"] % 360 in (90, 270)


def test_probe_photo_exif(tmp_path):
    p = make_photo(tmp_path / "p.jpg", 4000, 3000, exif_orientation=6)
    r = probe_media(p, "photo")
    assert (r["width"], r["height"]) == (3000, 4000)
    assert r["duration_sec"] is None and r["fps"] is None


def test_probe_audio(tmp_path):
    p = make_click_track(tmp_path / "c.wav", 120, 3.0)
    r = probe_media(p, "audio")
    assert r["duration_sec"] == pytest.approx(3.0, abs=0.1)
    assert r["width"] is None and r["fps"] is None


def test_probe_garbage_is_unreadable(tmp_path):
    p = tmp_path / "junk.mp4"
    p.write_bytes(bytes(range(256)) * 40)
    for kind in ("video", "photo", "audio"):
        with pytest.raises(ApiError) as e:
            probe_media(p, kind)
        assert e.value.code == "unreadable_file" and e.value.status == 422


def test_probe_wrong_stream_type_is_unreadable(tmp_path):
    p = make_click_track(tmp_path / "c.wav", 120, 2.0)
    with pytest.raises(ApiError) as e:
        probe_media(p, "video")
    assert e.value.code == "unreadable_file"
    v = make_video(tmp_path / "v.mp4", 320, 240, 1.0)
    with pytest.raises(ApiError) as e:
        probe_media(v, "audio")
    assert e.value.code == "unreadable_file"
