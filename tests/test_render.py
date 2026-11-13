import threading
import time
from pathlib import Path

import pytest

from app.editlist import ASPECTS, EditList
from app.models import FileMeta
from app.probe import probe_media
from app.render import assemble as assemble_mod
from app.render import pipeline as pipeline_mod
from app.render.ffmpeg import RenderError
from app.render.pipeline import render_edit_list
from app.render.segments import video_args
from tests.media import (
    ffprobe_json,
    make_click_track,
    make_photo,
    make_video,
    make_video_with_cover,
)

KINDS = {"video": "video", "video24": "video", "video60": "video", "rotated": "video",
         "photo": "photo", "music": "audio"}


@pytest.fixture(scope="session")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    paths = {
        "video": make_video(d / "v.mp4", 1920, 1080, 6.0, 30),
        "video24": make_video(d / "v24.mp4", 1280, 720, 3.0, 24),
        "video60": make_video(d / "v60.mp4", 1280, 720, 3.0, 60),
        "rotated": make_video(d / "rot.mp4", 1920, 1080, 3.0, 30, rotate=90),
        "photo": make_photo(d / "p.jpg", 2000, 1500),
        "music": make_click_track(d / "m.wav", 120, 10.0),
    }
    out = {}
    for name, path in paths.items():
        r = probe_media(path, KINDS[name])
        out[name] = FileMeta(name, KINDS[name], path, path.name, path.stat().st_size,
                             r["duration_sec"], r["width"], r["height"], r["fps"],
                             r["rotation"], 0.0)
    return out


def el_dict(aspect, mode, timeline, music_len):
    w, h = ASPECTS[aspect]
    return {
        "version": 1,
        "project": {"aspect": aspect, "width": w, "height": h, "fps": 30},
        "music": {"fileId": "music", "startSec": 1.0, "endSec": 1.0 + music_len,
                  "fadeOutSec": 0.5, "beats": []},
        "audioMode": mode,
        "timeline": timeline,
    }


def vseg(start, dur, in_sec, file_id="video", transition="cut"):
    return {"type": "video", "slotStartSec": start, "durationSec": dur,
            "source": {"fileId": file_id, "inSec": in_sec, "outSec": in_sec + dur},
            "transitionOut": transition}


def pseg(start, dur, motion="zoom_in", transition="cut"):
    return {"type": "photo", "slotStartSec": start, "durationSec": dur,
            "source": {"fileId": "photo"}, "motion": {"kind": motion},
            "transitionOut": transition}


def run(tmp_path, files, d, cancel=None, timeout=300.0, progress=None):
    out = tmp_path / "out.mp4"
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    render_edit_list(EditList.model_validate(d), files, out, work,
                     progress or (lambda f: None), cancel or threading.Event(), timeout)
    return out


def render(tmp_path, files, d, **kw):
    return ffprobe_json(run(tmp_path, files, d, **kw))


def streams(info, kind):
    return [s for s in info["streams"] if s["codec_type"] == kind]


@pytest.mark.parametrize("aspect,w,h", [("9:16", 1080, 1920), ("16:9", 1920, 1080),
                                        ("4:5", 1080, 1350)])
@pytest.mark.parametrize("mode", ["music", "silent"])
def test_five_second_render(tmp_path, files, aspect, w, h, mode):
    d = el_dict(aspect, mode, [
        vseg(0.0, 2.0, 0.0),
        pseg(2.0, 2.0, "zoom_in"),
        vseg(4.0, 1.0, 3.0),
    ], 5.0)
    info = render(tmp_path, files, d)
    v = streams(info, "video")[0]
    assert (v["width"], v["height"]) == (w, h)
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert v["r_frame_rate"] == "30/1"
    assert int(v["nb_frames"]) == 150
    assert bool(streams(info, "audio")) == (mode == "music")
    if mode == "music":
        assert streams(info, "audio")[0]["codec_name"] == "aac"
    assert len(streams(info, "audio")) <= 1


@pytest.mark.parametrize("motion", ["zoom_in", "zoom_out", "pan_left", "pan_right"])
def test_photo_motions_render(tmp_path, files, motion):
    d = el_dict("9:16", "silent", [pseg(0.0, 1.0, motion)], 1.0)
    v = streams(render(tmp_path, files, d), "video")[0]
    assert int(v["nb_frames"]) == 30


@pytest.mark.parametrize("src", ["video24", "video60"])
def test_other_frame_rates_normalised(tmp_path, files, src):
    d = el_dict("9:16", "silent", [vseg(0.0, 2.0, 0.0, file_id=src)], 2.0)
    v = streams(render(tmp_path, files, d), "video")[0]
    assert v["r_frame_rate"] == "30/1"
    assert int(v["nb_frames"]) == 60


def test_rotated_clip_renders_upright(tmp_path, files):
    assert (files["rotated"].width, files["rotated"].height) == (1080, 1920)
    d = el_dict("9:16", "silent", [vseg(0.0, 1.0, 0.0, file_id="rotated")], 1.0)
    v = streams(render(tmp_path, files, d), "video")[0]
    assert (v["width"], v["height"]) == (1080, 1920)
    assert "side_data_list" not in v
    assert "rotate" not in v.get("tags", {})


def test_crossfade_keeps_total_duration(tmp_path, files):
    d = el_dict("16:9", "silent", [
        vseg(0.0, 1.0, 0.0, transition="crossfade"),
        vseg(1.0, 1.0, 2.0),
        vseg(2.0, 1.0, 4.0),
    ], 3.0)
    v = streams(render(tmp_path, files, d), "video")[0]
    assert int(v["nb_frames"]) == 90
    assert v["r_frame_rate"] == "30/1"


def test_mixed_crossfade_and_cuts_with_music(tmp_path, files):
    d = el_dict("4:5", "music", [
        pseg(0.0, 1.0, "pan_left", "crossfade"),
        vseg(1.0, 1.0, 0.0, transition="crossfade"),
        vseg(2.0, 1.0, 2.0),
        vseg(3.0, 1.0, 4.0),
    ], 4.0)
    info = render(tmp_path, files, d)
    assert int(streams(info, "video")[0]["nb_frames"]) == 120
    assert len(streams(info, "audio")) == 1


def test_progress_reaches_one_and_is_monotonic(tmp_path, files):
    seen = []
    d = el_dict("9:16", "silent", [vseg(0.0, 1.0, 0.0), vseg(1.0, 1.0, 2.0)], 2.0)
    run(tmp_path, files, d, progress=seen.append)
    assert seen and seen[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in seen)
    assert seen == sorted(seen)


def test_cancel_stops_and_cleans_workdir(tmp_path, files):
    cancel = threading.Event()
    cancel.set()
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "stray.txt").write_text("x")
    d = el_dict("9:16", "silent", [vseg(0.0, 1.0, 0.0)], 1.0)
    with pytest.raises(RenderError) as e:
        run(tmp_path, files, d, cancel=cancel)
    assert str(e.value) == "The render was cancelled."
    assert list((tmp_path / "work").iterdir()) == []


def test_timeout_raises(tmp_path, files):
    d = el_dict("9:16", "silent", [vseg(0.0, 1.0, 0.0)], 1.0)
    with pytest.raises(RenderError) as e:
        run(tmp_path, files, d, timeout=0)
    assert str(e.value) == "The render took too long."
    assert list((tmp_path / "work").iterdir()) == []


def test_cancel_midway_kills_ffmpeg(tmp_path, files):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    d = el_dict("9:16", "silent", [vseg(float(i), 1.0, 0.0) for i in range(5)], 5.0)
    start = time.monotonic()
    try:
        run(tmp_path, files, d, cancel=cancel)
    except RenderError as e:
        assert str(e) == "The render was cancelled."
    assert time.monotonic() - start < 60
    assert list((tmp_path / "work").iterdir()) == []


def test_empty_segment_is_a_render_error(tmp_path, files):
    d = el_dict("9:16", "silent", [{"type": "empty", "slotStartSec": 0.0, "durationSec": 1.0}], 1.0)
    with pytest.raises(RenderError) as e:
        run(tmp_path, files, d)
    assert str(e.value) == "The video could not be rendered."


def test_missing_workdir_does_not_mask_the_render_error(tmp_path, files):
    d = el_dict("9:16", "silent", [vseg(0.0, 1.0, 0.0)], 1.0)
    with pytest.raises(RenderError) as e:
        render_edit_list(EditList.model_validate(d), files, tmp_path / "out.mp4",
                         tmp_path / "gone", lambda f: None, threading.Event(), 0)
    assert str(e.value) == "The render took too long."


def _inputs_are_restricted(args):
    """Every `-i` must have the protocol whitelist among the options since the last input."""
    last = 0
    for i, a in enumerate(args):
        if a == "-i":
            opts = args[last:i]
            assert "-protocol_whitelist" in opts, args
            assert opts[opts.index("-protocol_whitelist") + 1] == "file,pipe"
            last = i + 2
    return args.count("-i")


@pytest.mark.parametrize("crossfade", [False, True])
def test_every_ffmpeg_input_is_restricted(tmp_path, files, monkeypatch, crossfade):
    calls = []

    def fake_run(args, *a, **kw):
        calls.append(args)
        if "-frames:v" in args:
            Path(args[-1]).write_bytes(b"x")

    monkeypatch.setattr(pipeline_mod, "run_ffmpeg", fake_run)
    monkeypatch.setattr(assemble_mod, "run_ffmpeg", fake_run)
    d = el_dict("9:16", "music", [
        vseg(0.0, 1.0, 0.0, transition="crossfade" if crossfade else "cut"),
        pseg(1.0, 1.0),
    ], 2.0)
    run(tmp_path, files, d)
    assert len(calls) == 3
    assert [_inputs_are_restricted(c) for c in calls] == [1, 1, 3 if crossfade else 2]


def test_beat_decode_restricts_protocols(files, monkeypatch):
    import subprocess

    from app import beats as beats_mod
    seen = []
    real = subprocess.run
    monkeypatch.setattr(beats_mod.subprocess, "run",
                        lambda args, **kw: seen.append(args) or real(args, **kw))
    beats_mod.analyze(files["music"].path)
    assert _inputs_are_restricted(seen[0]) == 1


def test_stage_one_maps_the_probed_video_stream(files, tmp_path):
    seg = EditList.model_validate(
        el_dict("9:16", "silent", [vseg(0.0, 1.0, 0.0)], 1.0)).timeline[0]
    args = video_args(seg, files["video"], 1080, 1920, tmp_path / "o.mp4")
    i = args.index("-map")
    assert args[i + 1] == "0:V:0" and i > args.index("-i")


def test_clip_with_cover_art_renders_the_real_video(tmp_path, files):
    p = make_video_with_cover(tmp_path / "cover.mp4", 320, 240, 2.0)
    r = probe_media(p, "video")
    clip = FileMeta("cover", "video", p, p.name, p.stat().st_size, r["duration_sec"],
                    r["width"], r["height"], r["fps"], r["rotation"], 0.0)
    d = el_dict("16:9", "silent", [vseg(0.0, 1.0, 0.0, file_id="cover")], 1.0)
    v = streams(render(tmp_path, {**files, "cover": clip}, d), "video")
    assert len(v) == 1 and (v[0]["width"], v[0]["height"]) == (1920, 1080)
    assert int(v[0]["nb_frames"]) == 30


def test_failed_render_leaves_no_output(tmp_path, files):
    out = tmp_path / "out.mp4"
    out.write_bytes(b"half written")
    cancel = threading.Event()
    cancel.set()
    d = el_dict("9:16", "silent", [vseg(0.0, 1.0, 0.0)], 1.0)
    with pytest.raises(RenderError):
        render_edit_list(EditList.model_validate(d), files, out, tmp_path / "work",
                         lambda f: None, cancel, 300.0)
    assert not out.exists()
