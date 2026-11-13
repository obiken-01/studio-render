import subprocess

import pytest

from app import probe as probe_mod
from app.errors import ApiError
from app.probe import probe_media
from app.render.ffmpeg import INPUT_PROTOCOLS
from tests.media import (
    ffprobe_json,
    make_click_track,
    make_photo,
    make_video,
    make_video_with_cover,
)


def test_input_protocols_are_local_only():
    assert INPUT_PROTOCOLS == ["-protocol_whitelist", "file,pipe"]


def test_ffprobe_restricts_protocols(tmp_path, monkeypatch):
    clip = make_video(tmp_path / "a.mp4", 320, 240, 1.0)
    seen = []
    real = subprocess.run
    monkeypatch.setattr(probe_mod.subprocess, "run",
                        lambda args, **kw: seen.append(args) or real(args, **kw))
    probe_media(clip, "video")
    (args,) = seen
    i = args.index("-protocol_whitelist")
    assert args[i + 1] == "file,pipe" and i < len(args) - 1
    assert args[-1].endswith("a.mp4")


def test_probe_skips_attached_pictures(monkeypatch, tmp_path):
    cover = {"codec_type": "video", "width": 64, "height": 64,
             "disposition": {"attached_pic": 1}, "duration": "2.0", "avg_frame_rate": "0/0"}
    real = {"codec_type": "video", "width": 320, "height": 240,
            "disposition": {"attached_pic": 0}, "duration": "2.0", "avg_frame_rate": "30/1"}
    monkeypatch.setattr(probe_mod, "_ffprobe", lambda p: {"streams": [cover, real]})
    r = probe_media(tmp_path / "x.mp4", "video")
    assert (r["width"], r["height"], r["fps"]) == (320, 240, 30.0)
    monkeypatch.setattr(probe_mod, "_ffprobe", lambda p: {"streams": [cover]})
    with pytest.raises(ApiError) as e:
        probe_media(tmp_path / "x.mp4", "video")
    assert e.value.code == "unreadable_file"


def test_probe_video_with_cover_art(tmp_path):
    p = make_video_with_cover(tmp_path / "c.mp4", 320, 240, 2.0)
    assert any(s.get("disposition", {}).get("attached_pic") == 1
               for s in ffprobe_json(p)["streams"])
    r = probe_media(p, "video")
    assert (r["width"], r["height"]) == (320, 240)


@pytest.mark.parametrize("entry", ["/etc/passwd", "http://127.0.0.1:9/seg.ts"])
def test_playlist_disguised_as_mp4_is_unreadable(tmp_path, entry):
    p = tmp_path / "x.mp4"
    p.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.0,\n{entry}\n"
                 "#EXT-X-ENDLIST\n")
    for kind in ("video", "audio"):
        with pytest.raises(ApiError) as e:
            probe_media(p, kind)
        assert e.value.code == "unreadable_file"


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
