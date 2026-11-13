import pytest

from app.editlist import FRAME, EditList, EmptySegment, Music, Project
from app.errors import ApiError
from app.slots import build_slots, slots_from_template

GRID = [i * 0.5 for i in range(200)]  # 120 BPM, 100 s


@pytest.mark.parametrize("pace,per", [("calm", 4), ("medium", 2), ("fast", 1)])
def test_pace_sets_beats_per_slot(pace, per):
    p = build_slots(GRID, None, 30, pace)
    assert all(abs(s.duration - per * 0.5) <= FRAME for s in p.slots)


def test_ends_on_last_beat_within_length():
    p = build_slots([0.3 + i * 0.52 for i in range(200)], None, 30, "medium")
    assert p.start_sec == 0.3 and p.end_sec <= 30.3
    assert (p.end_sec - 0.3) / 0.52 == pytest.approx(round((p.end_sec - 0.3) / 0.52))


def test_slots_are_contiguous_and_sum_to_length():
    p = build_slots(GRID, None, 15, "fast")
    assert p.slots[0].start == 0
    assert sum(s.duration for s in p.slots) == pytest.approx(p.end_sec - p.start_sec, abs=FRAME)
    for a, b in zip(p.slots, p.slots[1:], strict=False):
        assert b.start == pytest.approx(a.start + a.duration, abs=1e-6)


def test_start_snaps_to_nearest_beat():
    assert build_slots(GRID, 12.3, 15, "fast").start_sec == 12.5


def test_all_uses_whole_song():
    p = build_slots(GRID, None, None, "calm")  # 200 beats, 4 per slot
    assert p.start_sec == 0.0 and p.end_sec == GRID[-1] and len(p.slots) == 50
    assert p.slots[-1].duration == pytest.approx(1.5, abs=FRAME)  # the 3 spare beats
    assert all(s.duration == pytest.approx(2.0, abs=FRAME) for s in p.slots[:-1])


def test_short_song_warns():
    p = build_slots(GRID[:20], None, 30, "medium")
    assert "song_shorter_than_length" in p.warnings
    assert p.end_sec == GRID[19]


def test_long_song_does_not_warn():
    assert build_slots(GRID, None, 30, "medium").warnings == []


def test_slow_song_halves_until_under_5s():
    slow = [i * 1.5 for i in range(60)]  # 40 BPM; calm would be 6 s
    assert all(s.duration <= 5.0 for s in build_slots(slow, None, 30, "calm").slots)


def _template(durations, aspect="9:16"):
    t, segs = 0.0, []
    for d in durations:
        segs.append(EmptySegment(type="empty", slotStartSec=t, durationSec=d))
        t += d
    return EditList(version=1, project=Project(aspect=aspect, width=1080, height=1920, fps=30),
                    music=Music(fileId="old", startSec=5.0, endSec=5.0 + t), audioMode="music",
                    timeline=segs)


def test_template_reuses_timings():
    p = slots_from_template(_template([1.0, 2.0, 1.0]), GRID, None)
    assert [s.duration for s in p.slots] == [1.0, 2.0, 1.0]
    assert [s.start for s in p.slots] == [0.0, 1.0, 3.0]
    assert p.start_sec == 0.0 and p.end_sec == pytest.approx(4.0)


def test_template_start_snaps_to_beat():
    p = slots_from_template(_template([1.0, 1.0]), GRID, 12.3)
    assert p.start_sec == 12.5 and p.end_sec == pytest.approx(14.5)


def test_template_longer_than_song():
    with pytest.raises(ApiError) as e:
        slots_from_template(_template([5.0, 5.0]), GRID[:10], None)  # song ends at 4.5 s
    assert e.value.code == "template_too_long"


def test_cap_holds_for_every_slot_with_irregular_gaps():
    gaps = [0.5, 0.5, 0.5, 0.5, 1.4, 0.5, 0.5, 0.5, 1.6, 0.5, 0.5, 0.5] * 12
    beats = [0.0]
    for g in gaps:
        beats.append(round(beats[-1] + g, 3))
    for pace in ("calm", "medium", "fast"):
        p = build_slots(beats, None, None, pace)
        assert all(s.duration <= 5.0 + FRAME for s in p.slots)
        assert p.end_sec == beats[-1]


def test_duplicate_beats_never_make_empty_slots():
    beats = [0.0, 0.0, 0.5, 0.5, 0.51, 1.0, 1.5, 2.0, 2.0, 2.5, 3.0, 3.0]
    for pace in ("calm", "medium", "fast"):
        p = build_slots(beats, None, None, pace)
        assert all(s.duration > 0 for s in p.slots)
        assert p.slots[0].start == 0
        assert sum(s.duration for s in p.slots) == pytest.approx(3.0, abs=FRAME)
