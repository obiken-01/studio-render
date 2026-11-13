from pathlib import Path

import pytest
from pydantic import ValidationError

from app.editlist import (
    XFADE_FRAMES,
    XFADE_SEC,
    EditList,
    snap,
    validate_for_render,
    validate_structure,
)
from app.errors import ApiError
from app.models import FileMeta


def _meta(file_id, kind, duration, w=None, h=None):
    return FileMeta(file_id, kind, Path("/x"), f"{file_id}.bin", 1, duration, w, h, None, 0, 0.0)


FILES = {
    "v1": _meta("v1", "video", 20.0, 3840, 2160),
    "p1": _meta("p1", "photo", None, 4000, 3000),
    "m1": _meta("m1", "audio", 60.0),
}


def good() -> dict:
    return {
        "version": 1,
        "project": {"aspect": "9:16", "width": 1080, "height": 1920, "fps": 30},
        "music": {"fileId": "m1", "startSec": 10.0, "endSec": 13.0, "fadeOutSec": 1.0, "beats": []},
        "audioMode": "music",
        "timeline": [
            {"type": "video", "slotStartSec": 0.0, "durationSec": 1.0,
             "source": {"fileId": "v1", "inSec": 4.0, "outSec": 5.0}},
            {"type": "photo", "slotStartSec": 1.0, "durationSec": 2.0,
             "source": {"fileId": "p1"}, "motion": {"kind": "zoom_in"}},
        ],
    }


def test_good_list_passes():
    validate_for_render(EditList.model_validate(good()), FILES)


def test_defaults_applied():
    el = EditList.model_validate(good())
    for seg in el.timeline:
        assert seg.transitionOut == "cut"
        assert (seg.crop.focusX, seg.crop.focusY) == (0.5, 0.5)


def test_unknown_field_rejected():
    d = good()
    d["timeline"][0]["speed"] = 2
    with pytest.raises(ValidationError):
        EditList.model_validate(d)


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d["project"].update(width=720), "bad_project"),
    (lambda d: d["project"].update(fps=24), "bad_project"),
    (lambda d: d["timeline"][1].update(slotStartSec=1.5), "timeline_gap"),
    (lambda d: d["music"].update(endSec=14.0), "duration_mismatch"),
    (lambda d: d["timeline"][0]["source"].update(fileId="zz"), "unknown_file"),
    (lambda d: d["timeline"][0]["source"].update(fileId="p1"), "wrong_file_kind"),
    (lambda d: d["timeline"][0]["source"].update(inSec=19.5, outSec=20.5), "bad_trim"),
    (lambda d: d["timeline"][0]["source"].update(outSec=5.5), "bad_trim"),
    (lambda d: d["music"].update(fileId="v1"), "wrong_file_kind"),
    (lambda d: d["timeline"][1].update(transitionOut="crossfade"), "bad_transition"),
])
def test_rule_violations(mutate, code):
    d = good()
    mutate(d)
    with pytest.raises(ApiError) as e:
        validate_for_render(EditList.model_validate(d), FILES)
    assert e.value.code == code
    assert e.value.status == 422
    assert e.value.message


def test_structure_rules_need_no_files():
    d = good()
    d["timeline"][1]["slotStartSec"] = 1.5
    with pytest.raises(ApiError) as e:
        validate_structure(EditList.model_validate(d))
    assert e.value.code == "timeline_gap"


def test_empty_slots_listed():
    d = good()
    d["timeline"][1] = {"type": "empty", "slotStartSec": 1.0, "durationSec": 2.0}
    with pytest.raises(ApiError) as e:
        validate_for_render(EditList.model_validate(d), FILES)
    assert e.value.code == "unfilled_slots" and e.value.details == {"indexes": [1]}
    assert "2" in e.value.message


def test_unfilled_slots_reported_before_other_errors():
    d = good()
    d["timeline"][1] = {"type": "empty", "slotStartSec": 1.0, "durationSec": 2.0}
    d["project"]["fps"] = 24
    with pytest.raises(ApiError) as e:
        validate_for_render(EditList.model_validate(d), FILES)
    assert e.value.code == "unfilled_slots"


def test_crossfade_needs_tail_footage():
    d = good()
    d["timeline"][0]["transitionOut"] = "crossfade"
    d["timeline"][0]["source"].update(inSec=19.0, outSec=20.0)
    with pytest.raises(ApiError) as e:
        validate_for_render(EditList.model_validate(d), FILES)
    assert e.value.code == "bad_trim"


def test_crossfade_with_tail_footage_passes():
    d = good()
    d["timeline"][0]["transitionOut"] = "crossfade"
    validate_for_render(EditList.model_validate(d), FILES)


def _music_at(d, start, end):
    d["music"].update(startSec=start, endSec=end)


def test_music_past_the_end_of_the_song_rejected():
    d = good()
    _music_at(d, 58.0, 61.0)  # the song is 60 s long
    with pytest.raises(ApiError) as e:
        validate_for_render(EditList.model_validate(d), FILES)
    assert (e.value.status, e.value.code) == (422, "bad_music_range")
    assert "song" in e.value.message


def test_music_up_to_one_frame_past_the_end_passes():
    d = good()
    _music_at(d, 57.0 + 1 / 60, 60.0 + 1 / 60)
    validate_for_render(EditList.model_validate(d), FILES)


def test_music_range_ignored_when_silent():
    d = good()
    _music_at(d, 58.0, 61.0)
    d["audioMode"] = "silent"
    validate_for_render(EditList.model_validate(d), FILES)


def test_crossfade_tail_is_four_frames():
    assert XFADE_FRAMES == 4 and XFADE_SEC == pytest.approx(4 / 30)
    d = good()
    d["timeline"][0]["transitionOut"] = "crossfade"
    d["timeline"][0]["source"].update(inSec=20.0 - 1.0 - 4 / 30, outSec=20.0 - 4 / 30)
    validate_for_render(EditList.model_validate(d), FILES)
    d["timeline"][0]["source"].update(inSec=20.0 - 1.0 - 3 / 30, outSec=20.0 - 3 / 30)
    with pytest.raises(ApiError) as e:
        validate_for_render(EditList.model_validate(d), FILES)
    assert e.value.code == "bad_trim"


def test_timeline_length_is_bounded():
    d = good()
    d["music"].update(startSec=0.0, endSec=601 / 30)
    d["timeline"] = [{"type": "empty", "slotStartSec": i / 30, "durationSec": 1 / 30}
                     for i in range(601)]
    with pytest.raises(ValidationError):
        EditList.model_validate(d)
    d["timeline"] = d["timeline"][:600]
    d["music"]["endSec"] = 20.0
    EditList.model_validate(d)


def test_beat_list_is_bounded():
    d = good()
    d["music"]["beats"] = [0.5] * 5001
    with pytest.raises(ValidationError):
        EditList.model_validate(d)
    d["music"]["beats"] = [0.5] * 5000
    EditList.model_validate(d)


def test_focus_out_of_range_rejected():
    d = good()
    d["timeline"][0]["crop"] = {"focusX": 1.2, "focusY": 0.5}
    with pytest.raises(ValidationError):
        EditList.model_validate(d)


def test_silent_mode_still_requires_music_block():
    d = good()
    d["audioMode"] = "silent"
    validate_for_render(EditList.model_validate(d), FILES)
    del d["music"]
    with pytest.raises(ValidationError):
        EditList.model_validate(d)


def test_empty_music_file_id_ok_at_model_level():
    d = good()
    d["music"]["fileId"] = ""
    el = EditList.model_validate(d)
    with pytest.raises(ApiError) as e:
        validate_for_render(el, FILES)
    assert e.value.code == "unknown_file"


def test_other_aspects_accepted():
    d = good()
    d["project"] = {"aspect": "4:5", "width": 1080, "height": 1350, "fps": 30}
    validate_for_render(EditList.model_validate(d), FILES)


def test_snap():
    assert snap(1.02) == round(31 / 30, 6) and snap(0.0) == 0.0


def test_file_meta_public():
    pub = FILES["v1"].public()
    assert pub["fileId"] == "v1" and pub["durationSec"] == 20.0
    assert set(pub) == {"fileId", "kind", "filename", "size", "durationSec",
                        "width", "height", "fps", "rotation"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("mutate", [
    lambda d, v: d["music"].update(endSec=v),
    lambda d, v: d["music"].update(startSec=v),
    lambda d, v: d["timeline"][0]["source"].update(inSec=v),
    lambda d, v: d["timeline"][0]["source"].update(outSec=v),
    lambda d, v: d["timeline"][0].update(durationSec=v),
    lambda d, v: d["timeline"][0].update(slotStartSec=v),
    lambda d, v: d["timeline"][0].update(crop={"focusX": v, "focusY": 0.5}),
    lambda d, v: d["music"].update(beats=[v]),
])
def test_non_finite_numbers_rejected(mutate, bad):
    d = good()
    mutate(d, bad)
    with pytest.raises(ValidationError):
        EditList.model_validate(d)
