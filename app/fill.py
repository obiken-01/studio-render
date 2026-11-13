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


def _plan_photo_slots(n: int, photos: int, has_videos: bool, rng: random.Random) -> set[int]:
    """Slot indexes that should show a photo, spread evenly through the timeline."""
    if not photos:
        return set()
    if not has_videos:
        return set(range(n))
    k = min(photos, n - 1)  # slot 0 always shows a video
    out: set[int] = set()
    for i in range(k):
        lo = i * (n - 1) // k
        span = (i + 1) * (n - 1) // k - lo
        out.add(1 + lo + rng.randrange(span))
    return out


def fill_slots(
    slots: list[Slot], videos: list[FileMeta], photos: list[FileMeta], seed: int
) -> tuple[list[Segment], list[str]]:
    rng = random.Random(seed)
    # Sorted first so the order the caller listed files in never changes the result.
    videos = sorted(videos, key=lambda m: m.file_id)
    photos = sorted(photos, key=lambda m: m.file_id)
    n = len(slots)
    planned = _plan_photo_slots(n, len(photos), bool(videos), rng)

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
        use_photo = i in planned or not candidates
        if use_photo and not photos:
            raise ApiError(
                422, "not_enough_footage",
                "There is not enough footage to fill every slot. "
                "Add a longer clip or some photos and try again.",
                {"slotIndex": i})

        if use_photo:
            last = i
            if slot.duration < MIN_PHOTO_SLOT and i + 1 < n:
                last = i + 1  # a short photo slot absorbs the next slot
                if last in planned:  # that slot's photo moves to the next free slot
                    planned.discard(last)
                    k = last + 1
                    while k < n and k in planned:
                        k += 1
                    if k < n:
                        planned.add(k)
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
        low = min(clip_uses[v.file_id] for v, _ in pool)
        meta, wins = rng.choice([c for c in pool if clip_uses[c[0].file_id] == low])
        free = [w for w in wins if not any(_overlaps(w, t) for t in clip_taken[meta.file_id])]
        if not free:
            free = wins
            warnings["shots_repeat"] = None
        in_sec, out_sec = rng.choice(free)
        clip_taken[meta.file_id].append((in_sec, out_sec))
        clip_uses[meta.file_id] += 1
        segments.append(VideoSegment(
            type="video", slotStartSec=slot.start, durationSec=slot.duration,
            source=VideoSource(fileId=meta.file_id, inSec=in_sec, outSec=out_sec),
            crop=Crop(), transitionOut="cut"))
        prev_id = meta.file_id
        i += 1

    return segments, list(warnings)
