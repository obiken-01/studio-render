import hashlib
import json
import os
import time
from types import SimpleNamespace

import pytest
from PIL import Image, JpegImagePlugin
from pillow_heif import register_heif_opener

from app.errors import ApiError
from app.storage import CHUNK_SIZE, Store
from tests.media import make_click_track, make_photo, make_silence, make_video

MB = 1024 * 1024


def _small_store(settings, **kw):
    return Store(settings.studio_data_dir, ttl_hours=24, max_file_bytes=3 * 1024**3,
                 max_project_bytes=10 * 1024**3, **kw)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _upload_all(store, path, name, kind):
    data = path.read_bytes()
    up = store.create_upload(name, len(data), kind)
    for n in range(up.total_chunks):
        store.write_chunk(up.upload_id, n, [data[n * up.chunk_size:(n + 1) * up.chunk_size]])
    return up


def test_chunks_out_of_order_assemble_to_identical_bytes(settings, tmp_path):
    original = make_video(tmp_path / "src.mp4", w=1280, h=720, dur=4)
    size = original.stat().st_size
    store = _small_store(settings, chunk_size=-(-size // 3))  # exactly three chunks
    data = original.read_bytes()
    up = store.create_upload("clip.mp4", size, "video")
    assert up.total_chunks == 3
    for n in (2, 0, 1):
        store.write_chunk(up.upload_id, n, [data[n * up.chunk_size:(n + 1) * up.chunk_size]])
    meta = store.complete_upload(up.upload_id)
    assert _sha(meta.path) == _sha(original)
    assert meta.width == 1280 and meta.height == 720
    assert meta.file_id == up.upload_id


def test_resume_reports_missing(store):
    up = store.create_upload("clip.mp4", CHUNK_SIZE * 3, "video")
    store.write_chunk(up.upload_id, 0, [b"a" * CHUNK_SIZE])
    store.write_chunk(up.upload_id, 2, [b"c" * CHUNK_SIZE])
    assert store.get_upload(up.upload_id).received == {0, 2}


def test_complete_with_missing_chunk(store):
    up = store.create_upload("clip.mp4", CHUNK_SIZE * 3, "video")
    store.write_chunk(up.upload_id, 0, [b"a" * CHUNK_SIZE])
    store.write_chunk(up.upload_id, 2, [b"c" * CHUNK_SIZE])
    with pytest.raises(ApiError) as e:
        store.complete_upload(up.upload_id)
    assert e.value.code == "upload_incomplete"
    assert e.value.details == {"missing": [1]}
    # the upload is still there so the client can send the missing part
    assert store.get_upload(up.upload_id).received == {0, 2}


def test_complete_twice(store, tmp_path):
    clip = make_video(tmp_path / "c.mp4", w=320, h=240, dur=1)
    up = _upload_all(store, clip, "c.mp4", "video")
    store.complete_upload(up.upload_id)
    with pytest.raises(ApiError) as e:
        store.complete_upload(up.upload_id)
    assert e.value.status == 404
    assert e.value.code == "unknown_upload"


def test_oversized_chunk_rejected(store):
    up = store.create_upload("clip.mp4", CHUNK_SIZE * 2, "video")
    with pytest.raises(ApiError) as e:
        store.write_chunk(up.upload_id, 0, [b"x" * (CHUNK_SIZE + 1)])
    assert e.value.status == 413
    assert e.value.code == "chunk_too_large"
    assert store.get_upload(up.upload_id).received == set()


def test_oversized_chunk_rejected_when_streamed_in_pieces(store):
    up = store.create_upload("clip.mp4", CHUNK_SIZE * 2, "video")
    pieces = (b"x" * MB for _ in range(9))
    with pytest.raises(ApiError) as e:
        store.write_chunk(up.upload_id, 0, pieces)
    assert e.value.code == "chunk_too_large"


def test_wrong_sized_chunk_rejected(store):
    up = store.create_upload("clip.mp4", CHUNK_SIZE + 10, "video")
    with pytest.raises(ApiError) as e:
        store.write_chunk(up.upload_id, 1, [b"x" * 11])  # last chunk must be exactly 10 bytes
    assert e.value.code == "bad_chunk"
    assert store.get_upload(up.upload_id).received == set()


def test_chunk_index_out_of_range(store):
    up = store.create_upload("clip.mp4", CHUNK_SIZE * 2, "video")
    for n in (2, -1):
        with pytest.raises(ApiError) as e:
            store.write_chunk(up.upload_id, n, [b"x"])
        assert e.value.code == "bad_chunk"


def test_unknown_upload(store):
    with pytest.raises(ApiError) as e:
        store.get_upload("up_nope")
    assert e.value.status == 404 and e.value.code == "unknown_upload"


def test_file_too_large(settings):
    store = Store(settings.studio_data_dir, ttl_hours=24, max_file_bytes=3 * 1024**3,
                  max_project_bytes=10 * 1024**3)
    with pytest.raises(ApiError) as e:
        store.create_upload("big.mp4", 3 * 1024**3 + 1, "video")
    assert e.value.status == 413
    assert e.value.code == "file_too_large"
    assert "3 GB" in e.value.message
    assert not any((settings.studio_data_dir / "uploads").iterdir())


def test_project_limit(settings):
    store = Store(settings.studio_data_dir, ttl_hours=24, max_file_bytes=1000,
                  max_project_bytes=1000)
    store.create_upload("a.mp4", 600, "video")
    with pytest.raises(ApiError) as e:
        store.create_upload("b.mp4", 600, "video")
    assert e.value.status == 413
    assert e.value.code == "project_too_large"


def test_bad_extension(store):
    with pytest.raises(ApiError) as e:
        store.create_upload("notes.txt", 10, "video")
    assert e.value.status == 415 and e.value.code == "unsupported_type"


def test_kind_must_match_extension(store):
    with pytest.raises(ApiError) as e:
        store.create_upload("a.mp4", 10, "audio")
    assert e.value.status == 415 and e.value.code == "unsupported_type"


def test_upper_case_extension_ok(store):
    up = store.create_upload("CLIP.MP4", 10, "video")
    assert up.ext == "mp4"


def test_hostile_filename_never_touches_path(store):
    up = store.create_upload("../../etc/passwd.mp4", 10, "video")
    root = store.data_dir.resolve()
    created = [p for p in store.data_dir.rglob("*") if p.is_file()]
    assert created
    for p in created:
        assert root in p.resolve().parents
        assert p.name == "state.json" or p.name.startswith(up.upload_id)
    assert not (root.parent / "etc").exists()


def test_audio_only_mp4_as_video_is_rejected_and_removed(store, tmp_path):
    clip = make_silence(tmp_path / "audio_only.mp4", dur=1)
    up = _upload_all(store, clip, "a.mp4", "video")
    with pytest.raises(ApiError) as e:
        store.complete_upload(up.upload_id)
    assert e.value.code == "unreadable_file"
    left = [p.name for p in store.data_dir.rglob("*") if p.is_file() and p.name != "state.json"]
    assert left == []
    assert store.files() == {}


@pytest.mark.parametrize("ext", ["wav", "mp3", "m4a"])
def test_song_formats_upload(store, tmp_path, ext):
    song = make_click_track(tmp_path / f"s.{ext}", bpm=120, dur=3)
    meta = store.complete_upload(_upload_all(store, song, f"s.{ext}", "audio").upload_id)
    assert meta.duration_sec == pytest.approx(3.0, abs=0.1)


def test_song_over_limit_is_rejected_at_upload(store, tmp_path, monkeypatch):
    monkeypatch.setattr("app.storage.MAX_AUDIO_SEC", 5)
    song = make_silence(tmp_path / "long.m4a", dur=8)
    up = _upload_all(store, song, "long.m4a", "audio")
    with pytest.raises(ApiError) as e:
        store.complete_upload(up.upload_id)
    assert (e.value.status, e.value.code) == (422, "audio_too_long")
    assert e.value.message == "This song is too long. Use a track under 15 minutes."
    assert store.files() == {}
    assert list(store.files_dir.iterdir()) == []


def test_song_limit_is_fifteen_minutes():
    from app.storage import MAX_AUDIO_SEC
    assert MAX_AUDIO_SEC == 15 * 60


def test_heic_photo_becomes_jpeg(store, tmp_path):
    register_heif_opener()
    heic = tmp_path / "shot.heic"
    Image.new("RGB", (640, 480), (10, 120, 200)).save(heic, format="HEIF")
    up = _upload_all(store, heic, "IMG_0001.HEIC", "photo")
    meta = store.complete_upload(up.upload_id)
    assert meta.path.suffix == ".jpg"
    assert (meta.width, meta.height) == (640, 480)
    assert not list(store.files_dir.glob("*.heic"))
    with Image.open(meta.path) as img:
        assert img.format == "JPEG"


def test_plain_photo_keeps_extension(store, tmp_path):
    photo = make_photo(tmp_path / "p.jpg", w=400, h=300)
    meta = store.complete_upload(_upload_all(store, photo, "p.JPG", "photo").upload_id)
    assert meta.path.suffix == ".jpg"
    assert meta.public()["width"] == 400


def _only_files(store):
    return sorted(p.name for p in store.files_dir.iterdir())


def test_big_rotated_photo_is_stored_upright_and_downscaled(store, tmp_path):
    photo = make_photo(tmp_path / "big.jpg", w=6000, h=4000, exif_orientation=6)
    meta = store.complete_upload(_upload_all(store, photo, "big.jpg", "photo").upload_id)
    assert (meta.width, meta.height) == (2880, 4320)
    assert meta.size == meta.path.stat().st_size
    assert _only_files(store) == [meta.path.name]
    with Image.open(meta.path) as img:
        assert img.format == "JPEG" and img.mode == "RGB"
        assert img.size == (2880, 4320)
        assert img.getexif().get(0x0112, 1) == 1
        # The red block drawn top-left on the sensor is top-right once turned upright.
        r, g, b = img.getpixel((int(2880 * 0.9), int(4320 * 0.1)))
        assert r > 150 and g < 120 and b < 120
        r, g, b = img.getpixel((int(2880 * 0.1), int(4320 * 0.1)))
        assert b > 120 and r < 100


def test_very_big_jpeg_is_decoded_reduced_and_downscaled(store, tmp_path, monkeypatch):
    seen = []
    real_draft = JpegImagePlugin.JpegImageFile.draft
    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft",
                        lambda self, mode, size: seen.append(size) or real_draft(self, mode, size))
    photo = make_photo(tmp_path / "huge.jpg", w=9000, h=6000)
    meta = store.complete_upload(_upload_all(store, photo, "huge.jpg", "photo").upload_id)
    assert seen == [(4320, 2880)]
    assert (meta.width, meta.height) == (4320, 2880)


def test_small_photo_is_not_upscaled(store, tmp_path):
    photo = make_photo(tmp_path / "small.jpg", w=400, h=300, exif_orientation=8)
    meta = store.complete_upload(_upload_all(store, photo, "small.jpg", "photo").upload_id)
    assert (meta.width, meta.height) == (300, 400)
    with Image.open(meta.path) as img:
        assert img.size == (300, 400)


def test_png_photo_becomes_jpeg(store, tmp_path):
    png = tmp_path / "shot.png"
    Image.new("RGBA", (5000, 1000), (10, 120, 200, 128)).save(png)
    meta = store.complete_upload(_upload_all(store, png, "shot.png", "photo").upload_id)
    assert meta.path.suffix == ".jpg" and (meta.width, meta.height) == (4320, 864)
    assert _only_files(store) == [meta.path.name]
    with Image.open(meta.path) as img:
        assert img.format == "JPEG" and img.mode == "RGB"


def test_decompression_bomb_is_unreadable(store, tmp_path, monkeypatch):
    photo = make_photo(tmp_path / "p.jpg", w=400, h=300)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # 120000 px is far past 2x the limit
    up = _upload_all(store, photo, "p.jpg", "photo")
    with pytest.raises(ApiError) as e:
        store.complete_upload(up.upload_id)
    assert e.value.code == "unreadable_file"
    assert _only_files(store) == [] and store.files() == {}


def test_photo_just_over_the_pixel_limit_is_unreadable(store, tmp_path, monkeypatch):
    photo = make_photo(tmp_path / "p.jpg", w=400, h=300)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100_000)  # over the limit, under 2x
    up = _upload_all(store, photo, "p.jpg", "photo")
    with pytest.warns(Image.DecompressionBombWarning), pytest.raises(ApiError) as e:
        store.complete_upload(up.upload_id)
    assert e.value.code == "unreadable_file"
    assert _only_files(store) == []


def test_pixel_limit_is_sane():
    import app.probe  # noqa: F401  (sets the limit)
    assert Image.MAX_IMAGE_PIXELS == 200_000_000


def test_sweep_removes_expired(store, tmp_path):
    clip = make_video(tmp_path / "c.mp4", w=320, h=240, dur=1)
    meta = store.complete_upload(_upload_all(store, clip, "c.mp4", "video").upload_id)
    t0 = meta.created_at
    assert store.sweep(t0 + 60) == 0
    assert meta.path.exists()
    assert store.sweep(t0 + 24 * 3600 + 1) == 1
    assert not meta.path.exists()
    assert store.files() == {}


def test_sweep_removes_stale_unfinished_uploads(store):
    up = store.create_upload("clip.mp4", 100, "video")
    part = next(store.uploads_dir.iterdir())
    assert store.sweep(up.created_at + 60) == 0
    assert part.exists()
    assert store.sweep(up.created_at + 24 * 3600 + 1) == 1
    assert not part.exists()
    with pytest.raises(ApiError):
        store.get_upload(up.upload_id)


def test_state_survives_restart(store, settings, tmp_path):
    clip = make_video(tmp_path / "c.mp4", w=320, h=240, dur=1)
    meta = store.complete_upload(_upload_all(store, clip, "c.mp4", "video").upload_id)
    pending = store.create_upload("later.mp4", CHUNK_SIZE * 2, "video")
    store.write_chunk(pending.upload_id, 1, [b"z" * CHUNK_SIZE])
    again = _small_store(settings)
    assert again.get_file(meta.file_id) == meta
    assert again.get_upload(pending.upload_id).received == {1}


def test_no_disk_space(store, monkeypatch):
    monkeypatch.setattr("app.storage.shutil.disk_usage",
                        lambda _p: SimpleNamespace(total=100, used=99, free=5))
    with pytest.raises(ApiError) as e:
        store.create_upload("clip.mp4", 1000, "video")
    assert e.value.status == 507 and e.value.code == "disk_full"
    assert not any(store.uploads_dir.iterdir())


def test_delete_file_and_clear(store, tmp_path):
    clip = make_video(tmp_path / "c.mp4", w=320, h=240, dur=1)
    a = store.complete_upload(_upload_all(store, clip, "c.mp4", "video").upload_id)
    b = store.complete_upload(_upload_all(store, clip, "d.mp4", "video").upload_id)
    store.create_upload("pending.mp4", 50, "video")
    store.delete_file(a.file_id)
    assert not a.path.exists()
    with pytest.raises(ApiError) as e:
        store.get_file(a.file_id)
    assert e.value.code == "unknown_file"
    assert store.total_bytes() == b.size + 50
    store.clear()
    assert store.files() == {} and store.total_bytes() == 0
    assert not any(store.files_dir.iterdir()) and not any(store.uploads_dir.iterdir())


def test_oversized_last_chunk_never_grows_the_file(settings):
    store = _small_store(settings, chunk_size=100)
    up = store.create_upload("clip.mp4", 250, "video")  # chunks of 100, 100, 50
    part = next(store.uploads_dir.iterdir())
    with pytest.raises(ApiError) as e:
        store.write_chunk(up.upload_id, 2, [b"x" * 100])
    assert e.value.code == "bad_chunk"
    assert part.stat().st_size == 250
    assert 2 not in store.get_upload(up.upload_id).received
    with pytest.raises(ApiError) as e:
        store.write_chunk(up.upload_id, 0, [b"x" * 101])
    assert e.value.code == "chunk_too_large"
    assert part.stat().st_size == 250


def test_failed_resend_unmarks_chunk_and_good_resend_completes(settings, tmp_path):
    original = make_video(tmp_path / "src.mp4", w=640, h=360, dur=2)
    data = original.read_bytes()
    store = _small_store(settings, chunk_size=-(-len(data) // 2))
    up = store.create_upload("clip.mp4", len(data), "video")
    cs = up.chunk_size
    chunks = [data[:cs], data[cs:]]
    for n in (0, 1):
        store.write_chunk(up.upload_id, n, [chunks[n]])
    with pytest.raises(ApiError):
        store.write_chunk(up.upload_id, 0, [b"garbage"])
    assert store.get_upload(up.upload_id).received == {1}
    with pytest.raises(ApiError) as e:
        store.complete_upload(up.upload_id)
    assert e.value.code == "upload_incomplete" and e.value.details == {"missing": [0]}
    store.write_chunk(up.upload_id, 0, [chunks[0]])
    meta = store.complete_upload(up.upload_id)
    assert _sha(meta.path) == _sha(original)


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_sweep_deletes_old_orphans_but_keeps_young_and_registered(store, tmp_path):
    clip = make_video(tmp_path / "c.mp4", w=320, h=240, dur=1)
    meta = store.complete_upload(_upload_all(store, clip, "c.mp4", "video").upload_id)
    pending = store.create_upload("p.mp4", 10, "video")
    old = store.files_dir / "up_old.mp4"
    old_part = store.uploads_dir / "up_oldpart.mp4.part"
    young = store.files_dir / "up_young.mp4"
    for p in (old, old_part, young):
        p.write_bytes(b"x")
    _age(old, 25 * 3600)
    _age(old_part, 25 * 3600)
    assert store.sweep(time.time()) == 2
    assert not old.exists() and not old_part.exists()
    assert young.exists() and meta.path.exists()
    assert (store.data_dir / "state.json").exists()
    assert store.get_upload(pending.upload_id)


def _good_file_entry(store, file_id="up_good"):
    path = store.files_dir / f"{file_id}.mp4"
    path.write_bytes(b"x")
    return {"file_id": file_id, "kind": "video", "path": str(path), "filename": "a.mp4",
            "size": 1, "duration_sec": 1.0, "width": 320, "height": 240, "fps": 30.0,
            "rotation": 0, "created_at": time.time()}


@pytest.mark.parametrize("shape", [
    "list", "files_not_list", "non_dict_entries", "missing_key", "extra_key", "bad_upload",
])
def test_bad_state_file_does_not_stop_startup(store, settings, shape):
    good = _good_file_entry(store)
    bad = _good_file_entry(store, "up_bad")
    state = {"uploads": [], "files": [good]}
    if shape == "list":
        state = [good]
    elif shape == "files_not_list":
        state = {"uploads": 3, "files": "nope"}
    elif shape == "non_dict_entries":
        state["files"] = [5, None, "x", good]
        state["uploads"] = [7]
    elif shape == "missing_key":
        del bad["path"]
        state["files"] = [bad, good]
    elif shape == "extra_key":
        bad["colour"] = "blue"
        state["files"] = [bad, good]
    elif shape == "bad_upload":
        state["uploads"] = [{"upload_id": "up_u", "received": 5}]
    (settings.studio_data_dir / "state.json").write_text(json.dumps(state))
    again = _small_store(settings)
    with pytest.raises(ApiError):
        again.get_file("up_bad")
    if shape in ("non_dict_entries", "missing_key", "extra_key", "bad_upload"):
        assert again.get_file("up_good").size == 1
    with pytest.raises(ApiError):
        again.get_upload("up_u")


def test_corrupt_state_then_sweep_removes_old_files(store, settings, tmp_path):
    clip = make_video(tmp_path / "c.mp4", w=320, h=240, dur=1)
    meta = store.complete_upload(_upload_all(store, clip, "c.mp4", "video").upload_id)
    (settings.studio_data_dir / "state.json").write_text("{not json")
    again = _small_store(settings)
    assert again.files() == {}
    assert meta.path.exists()
    assert again.sweep(time.time() + 24 * 3600 + 1) == 1
    assert not meta.path.exists()
    assert (settings.studio_data_dir / "state.json").exists()
