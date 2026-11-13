from collections import Counter
from pathlib import Path

import pytest

from app.editlist import FRAME, EditList, Music, Project, snap, validate_for_render
from app.errors import ApiError
from app.fill import MOTIONS, fill_slots
from app.models import FileMeta
from app.slots import Slot, build_slots


def _meta(file_id, kind, dur):
    return FileMeta(file_id=file_id, kind=kind, path=Path("x"), filename=file_id, size=1,
                    duration_sec=dur, width=1920, height=1080, fps=30.0, rotation=0,
                    created_at=0.0)


def vid(file_id, dur):
    return _meta(file_id, "video", dur)


def photo(file_id):
    return _meta(file_id, "photo", None)


SLOTS = [Slot(float(i), 1.0) for i in range(20)]


def _videos(segs):
    return [s for s in segs if s.type == "video"]


def test_no_back_to_back_repeats():
    segs, w = fill_slots(SLOTS, [vid("a", 30), vid("b", 30), vid("c", 30)], [], seed=1)
    ids = [s.source.fileId for s in segs]
    assert all(x != y for x, y in zip(ids, ids[1:], strict=False)) and w == []


def test_even_spread():
    segs, _ = fill_slots(SLOTS, [vid("a", 30), vid("b", 30), vid("c", 30)], [], seed=3)
    counts = Counter(s.source.fileId for s in segs)
    assert set(counts) == {"a", "b", "c"} and max(counts.values()) - min(counts.values()) <= 1


def test_same_seed_same_result():
    vids = [vid("a", 30), vid("b", 30), vid("c", 30)]
    ph = [photo("p")]
    one, _ = fill_slots(SLOTS, vids, ph, seed=5)
    two, _ = fill_slots(SLOTS, vids, ph, seed=5)
    other, _ = fill_slots(SLOTS, vids, ph, seed=6)
    assert one == two and one != other


def test_input_order_does_not_change_result():
    vids = [vid("a", 30), vid("b", 30), vid("c", 30)]
    one, _ = fill_slots(SLOTS, vids, [photo("p"), photo("q")], seed=2)
    two, _ = fill_slots(SLOTS, vids[::-1], [photo("q"), photo("p")], seed=2)
    assert one == two


def test_windows_do_not_overlap_within_a_clip():
    segs, w = fill_slots(SLOTS, [vid("a", 30), vid("b", 30)], [], seed=4)
    assert w == []
    by_clip = {}
    for s in _videos(segs):
        by_clip.setdefault(s.source.fileId, []).append((s.source.inSec, s.source.outSec))
    for wins in by_clip.values():
        wins.sort()
        assert all(a[1] <= b[0] + 1e-9 for a, b in zip(wins, wins[1:], strict=False))


def test_windows_skip_clip_edges():
    segs, _ = fill_slots(SLOTS, [vid("a", 12), vid("b", 12)], [], seed=1)
    for s in _videos(segs):
        assert s.source.inSec >= 0.5 and s.source.outSec <= 12 - 0.5
        assert s.source.outSec - s.source.inSec == pytest.approx(s.durationSec)


def test_each_photo_once_with_motion():
    segs, _ = fill_slots(SLOTS, [vid("a", 30), vid("b", 30)], [photo("p")], seed=1)
    ph = [s for s in segs if s.type == "photo"]
    assert len(ph) == 1 and ph[0].motion.kind in MOTIONS
    assert segs[0].type == "video"  # never the first slot when there are videos


def test_several_photos_each_used_once():
    photos = [photo(f"p{i}") for i in range(4)]
    segs, w = fill_slots(SLOTS, [vid("a", 30), vid("b", 30)], photos, seed=9)
    used = [s.source.fileId for s in segs if s.type == "photo"]
    assert sorted(used) == ["p0", "p1", "p2", "p3"] and "shots_repeat" not in w


def test_short_photo_slot_merges_next():
    slots = [Slot(i * 0.5, 0.5) for i in range(10)]
    segs, _ = fill_slots(slots, [vid("a", 30), vid("b", 30)], [photo("p")], seed=1)
    ph = [s for s in segs if s.type == "photo"]
    assert len(ph) == 1 and ph[0].durationSec == pytest.approx(1.0)
    assert len(segs) == 9
    assert sum(s.durationSec for s in segs) == pytest.approx(5.0)


def _photo_tail_slots(n):
    return [Slot(i * 0.5, 0.5) for i in range(n)]


@pytest.mark.parametrize("n", [14, 15, 20, 21])
def test_every_photo_placed_once_including_the_tail(n):
    slots = _photo_tail_slots(n)
    photos = [photo(f"p{i}") for i in range(5)]
    for seed in range(40):
        segs, w = fill_slots(slots, [vid("a", 30), vid("b", 30)], photos, seed)
        used = sorted(s.source.fileId for s in segs if s.type == "photo")
        assert used == [f"p{i}" for i in range(5)] and w == []
        assert segs[0].type == "video"
        assert all(s.durationSec >= 1.0 - 1e-6 for s in segs if s.type == "photo")
        assert sum(s.durationSec for s in segs) == pytest.approx(n * 0.5, abs=1e-3)
        t = 0.0
        for s in segs:
            assert s.slotStartSec == pytest.approx(t, abs=1e-3)
            t += s.durationSec


def test_photo_in_last_slot_merges_previous_video():
    slots = _photo_tail_slots(4)  # only the last slot (index 3) can host the photo
    for seed in range(20):
        segs, _ = fill_slots(slots, [vid("a", 30)], [photo("p")], seed)
        photos_ = [s for s in segs if s.type == "photo"]
        assert len(photos_) == 1 and photos_[0].durationSec >= 1.0 - 1e-6
        assert sum(s.durationSec for s in segs) == pytest.approx(2.0, abs=1e-3)


def test_more_photos_than_slots_warns():
    slots = _photo_tail_slots(3)
    segs, w = fill_slots(slots, [vid("a", 30)], [photo("p"), photo("q")], seed=1)
    assert "photos_left_out" in w
    assert sum(s.durationSec for s in segs) == pytest.approx(1.5, abs=1e-3)
    segs, w = fill_slots(SLOTS[:2], [], [photo("p"), photo("q"), photo("r")], seed=1)
    assert "photos_left_out" in w and len(segs) == 1


def test_short_clip_is_not_over_picked_while_long_clip_has_windows():
    slots = [Slot(float(i), 1.0) for i in range(10)]
    segs, w = fill_slots(slots, [vid("a", 6), vid("b", 30)], [], seed=1)
    assert "shots_repeat" not in w and "same_clip_back_to_back" not in w
    taken = {}
    for s in segs:
        taken.setdefault(s.source.fileId, []).append((s.source.inSec, s.source.outSec))
    for wins in taken.values():
        assert len(set(wins)) == len(wins)


def test_photos_only_fill_every_slot_and_warn_on_reuse():
    segs, w = fill_slots(SLOTS, [], [photo("p"), photo("q")], seed=1)
    assert all(s.type == "photo" for s in segs) and "shots_repeat" in w
    ids = [s.source.fileId for s in segs]
    assert all(x != y for x, y in zip(ids, ids[1:], strict=False))


def test_reuse_when_short_on_footage():
    slots = [Slot(float(i), 1.0) for i in range(40)]
    segs, w = fill_slots(slots, [vid("a", 6), vid("b", 30)], [], seed=1)
    assert "shots_repeat" in w and len(segs) == 40


def test_single_clip_relaxes_no_repeat():
    segs, w = fill_slots(SLOTS, [vid("a", 60)], [], seed=1)
    assert "same_clip_back_to_back" in w and len(segs) == 20


def test_clip_shorter_than_slot_is_skipped():  # Review Focus 1
    segs, _ = fill_slots([Slot(0, 2.0), Slot(2.0, 2.0)], [vid("tiny", 0.8), vid("ok", 30)], [],
                         seed=1)
    assert {s.source.fileId for s in segs} == {"ok"}


def test_nothing_fits_is_a_clear_error():  # Review Focus 1
    with pytest.raises(ApiError) as e:
        fill_slots([Slot(0, 2.0)], [vid("tiny", 0.8)], [], seed=1)
    assert e.value.code == "not_enough_footage" and e.value.status == 422


def test_photo_covers_slots_no_clip_can_fill():
    segs, _ = fill_slots([Slot(0, 2.0), Slot(2.0, 2.0)], [vid("tiny", 0.8)], [photo("p")], seed=1)
    assert [s.type for s in segs] == ["photo", "photo"]


def test_segments_are_contiguous_and_snapped():
    plan = build_slots([i * 0.52 for i in range(100)], None, 30, "medium")
    segs, _ = fill_slots(plan.slots, [vid("a", 30), vid("b", 30)], [photo("p")], seed=1)
    _check_timeline(segs, plan)


def _check_timeline(segs, plan):
    t = 0.0
    for s in segs:
        assert s.slotStartSec == pytest.approx(t, abs=1e-3)
        assert snap(s.slotStartSec) == pytest.approx(s.slotStartSec, abs=1e-6)
        assert snap(s.durationSec) == pytest.approx(s.durationSec, abs=1e-6)
        t = s.slotStartSec + s.durationSec
    total = sum(s.duration for s in plan.slots)
    assert t == pytest.approx(total, abs=1e-3)
    assert abs(t - (plan.end_sec - plan.start_sec)) <= FRAME


@pytest.mark.parametrize("pace", ["calm", "medium", "fast"])
@pytest.mark.parametrize("mix", [
    ([("a", 30), ("b", 12), ("c", 7)], 0),
    ([("a", 30), ("b", 12)], 2),
    ([("a", 9)], 1),
    ([], 3),
    ([("a", 20), ("tiny", 0.4)], 5),
    ([("a", 30), ("b", 30)], 12),
])
def test_property_every_draft_is_renderable(pace, mix):
    clips, n_photos = mix
    videos = [vid(i, d) for i, d in clips]
    photos = [photo(f"p{i}") for i in range(n_photos)]
    files = {m.file_id: m for m in videos + photos}
    files["song"] = _meta("song", "audio", 100.0)
    beats = [0.4 + i * 0.47 for i in range(130)]
    for length in (15, 30, None):
        plan = build_slots(beats, None, length, pace)
        for seed in range(15):
            segs, _ = fill_slots(plan.slots, videos, photos, seed)
            _check_timeline(segs, plan)
            el = EditList(
                version=1, project=Project(aspect="9:16", width=1080, height=1920, fps=30),
                music=Music(fileId="song", startSec=plan.start_sec, endSec=plan.end_sec),
                audioMode="music", timeline=segs)
            validate_for_render(el, files)
