import random

from app.editlist import (
    Crop,
    Motion,
    PhotoSegment,
    PhotoSource,
    Segment,
    VideoSegment,
    VideoSource,
    snap,
)
from app.errors import ApiError
from app.models import FileMeta
from app.slots import Slot

MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right")
EDGE_SEC = 0.5  # skipped at both ends of every clip
MIN_PHOTO_SLOT = 1.5
_EPS = 1e-9


def _windows(meta: FileMeta, d: float) -> list[tuple[float, float]]:
    """Back-to-back windows of length d, clear of the clip's first and last 0.5 s."""
    usable = (meta.duration_sec or 0.0) - 2 * EDGE_SEC
    count = int(usable / d + _EPS)
    return [(round(EDGE_SEC + w * d, 6), round(EDGE_SEC + (w + 1) * d, 6)) for w in range(count)]


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] - _EPS and b[0] < a[1] - _EPS


def _photo_groups(
    slots: list[Slot], photos: int, has_videos: bool, rng: random.Random
) -> dict[int, int]:
    """Decide up front which runs of slots show a photo: {first_index: last_index}.

    A photo slot shorter than 1.5 s absorbs the next slot, or the previous one when it is the
    last slot. Groups never overlap, and slot 0 stays a video when there are videos.
    """
    n = len(slots)
    if not photos:
        return {}

    def group(i: int) -> tuple[int, int]:
        if slots[i].duration >= MIN_PHOTO_SLOT or n == 1:
            return i, i
        return (i, i + 1) if i + 1 < n else (i - 1, i)

    groups: dict[int, int] = {}
    if not has_videos:  # photos fill everything: tile the timeline left to right
        i = 0
        while i < n:
            first, last = group(i)
            if first < i:  # short final slot: extend the previous photo instead
                prev = max(groups) if groups else i
                groups[prev] = n - 1
                break
            groups[first] = last
            i = last + 1
        return groups

    k = min(photos, n - 1)
    if k <= 0:
        return groups
    claimed: set[int] = set()
    for i in range(k):
        lo = i * (n - 1) // k
        span = (i + 1) * (n - 1) // k - lo
        target = 1 + lo + rng.randrange(span)
        for c in [*range(target, n), *range(target - 1, 0, -1)]:
            first, last = group(c)
            idxs = set(range(first, last + 1))
            if first == 0 or idxs & claimed:
                continue
            claimed |= idxs
            groups[first] = last
            break
    return groups


def fill_slots(
    slots: list[Slot], videos: list[FileMeta], photos: list[FileMeta], seed: int
) -> tuple[list[Segment], list[str]]:
    rng = random.Random(seed)
    # Sorted first so the order the caller listed files in never changes the result.
    videos = sorted(videos, key=lambda m: m.file_id)
    photos = sorted(photos, key=lambda m: m.file_id)
    n = len(slots)
    groups = _photo_groups(slots, len(photos), bool(videos), rng)

    warnings: dict[str, None] = {}
    clip_uses = {v.file_id: 0 for v in videos}
    clip_taken: dict[str, list[tuple[float, float]]] = {v.file_id: [] for v in videos}
    photo_uses = {p.file_id: 0 for p in photos}
    prev_id: str | None = None
    segments: list[Segment] = []

    i = 0
    while i < n:
        slot = slots[i]
        candidates = [(v, w) for v in videos if (w := _windows(v, slot.duration))]
        if i in groups or not candidates:
            if not photos:
                raise ApiError(
                    422, "not_enough_footage",
                    "There is not enough footage to fill every slot. "
                    "Add a longer clip or some photos and try again.",
                    {"slotIndex": i})
            last = groups.get(i, i)
            if (i not in groups and slot.duration < MIN_PHOTO_SLOT and i + 1 < n
                    and i + 1 not in groups):
                last = i + 1  # a stand-in photo for a short slot absorbs the next one
            end = slots[last].start + slots[last].duration
            pool = [p for p in photos if p.file_id != prev_id]
            if not pool:
                pool = photos
                warnings["same_clip_back_to_back"] = None
            low = min(photo_uses[p.file_id] for p in pool)
            pick = rng.choice([p for p in pool if photo_uses[p.file_id] == low])
            if photo_uses[pick.file_id]:
                warnings["shots_repeat"] = None
            photo_uses[pick.file_id] += 1
            segments.append(PhotoSegment(
                type="photo", slotStartSec=slot.start,
                durationSec=snap(end - slot.start),
                source=PhotoSource(fileId=pick.file_id),
                motion=Motion(kind=rng.choice(MOTIONS)), crop=Crop()))
            prev_id = pick.file_id
            i = last + 1
            continue

        pool = [(v, w) for v, w in candidates if v.file_id != prev_id]
        if not pool:
            pool = candidates
            warnings["same_clip_back_to_back"] = None
        # Prefer clips that still have an unused window; reuse only when none has one.
        with_free = []
        for v, w in pool:
            free = [x for x in w if not any(_overlaps(x, t) for t in clip_taken[v.file_id])]
            if free:
                with_free.append((v, free))
        if with_free:
            options = with_free
        else:
            options = pool
            warnings["shots_repeat"] = None
        low = min(clip_uses[v.file_id] for v, _ in options)
        meta, wins = rng.choice([o for o in options if clip_uses[o[0].file_id] == low])
        in_sec, out_sec = rng.choice(wins)
        clip_taken[meta.file_id].append((in_sec, out_sec))
        clip_uses[meta.file_id] += 1
        segments.append(VideoSegment(
            type="video", slotStartSec=slot.start, durationSec=slot.duration,
            source=VideoSource(fileId=meta.file_id, inSec=in_sec, outSec=out_sec),
            crop=Crop(), transitionOut="cut"))
        prev_id = meta.file_id
        i += 1

    if photos and any(c == 0 for c in photo_uses.values()):
        warnings["photos_left_out"] = None
    return segments, list(warnings)
