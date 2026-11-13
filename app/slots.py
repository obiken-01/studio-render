from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Literal

from app.editlist import EditList, snap
from app.errors import ApiError

PER_PACE = {"calm": 4, "medium": 2, "fast": 1}
MAX_SLOT_SEC = 5.0
_EPS = 1e-6


@dataclass
class Slot:
    start: float
    duration: float


@dataclass
class SlotPlan:
    start_sec: float
    end_sec: float
    slots: list[Slot]
    warnings: list[str] = field(default_factory=list)


def _start_index(beats: list[float], start_sec: float | None) -> int:
    if start_sec is None:
        return 0
    return min(range(len(beats)), key=lambda i: abs(beats[i] - start_sec))


def build_slots(
    beats: list[float],
    start_sec: float | None,
    length_sec: float | None,
    pace: Literal["calm", "medium", "fast"],
) -> SlotPlan:
    i0 = _start_index(beats, start_sec)
    start = beats[i0]
    if length_sec is None:
        i_end = len(beats) - 1
    else:
        i_end = bisect_right(beats, start + length_sec + _EPS) - 1
    n = i_end - i0
    if n < 1:
        raise ApiError(
            422, "no_beats",
            "There are not enough beats after the chosen start. "
            "Pick an earlier start or another track.")
    interval = (beats[i_end] - start) / n

    warnings: list[str] = []
    if (length_sec is not None and i_end == len(beats) - 1
            and start + length_sec - beats[i_end] >= interval):
        warnings.append("song_shorter_than_length")

    per = PER_PACE[pace]
    while per > 1 and per * interval > MAX_SLOT_SEC:
        per //= 2

    count = max(1, n // per)
    cuts = [i0 + k * per for k in range(count)] + [i_end]  # last slot absorbs spare beats
    times = [snap(beats[c] - start) for c in cuts]
    slots = [Slot(times[k], snap(times[k + 1] - times[k])) for k in range(count)]
    return SlotPlan(start, beats[i_end], slots, warnings)


def slots_from_template(
    template: EditList, beats: list[float], start_sec: float | None
) -> SlotPlan:
    i0 = _start_index(beats, start_sec)
    start = beats[i0]
    slots: list[Slot] = []
    cur = 0.0
    for seg in template.timeline:
        d = snap(seg.durationSec)
        slots.append(Slot(cur, d))
        cur = snap(cur + d)
    if cur > beats[-1] - start + _EPS:
        raise ApiError(
            422, "template_too_long",
            "This song is too short for the chosen template. "
            "Pick a longer song or an earlier start.",
            {"templateSec": round(cur, 3), "songSec": round(beats[-1] - start, 3)})
    return SlotPlan(start, start + cur, slots, [])
