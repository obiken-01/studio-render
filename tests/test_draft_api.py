import pytest

from app.editlist import EditList, EmptySegment, Music, Project, validate_for_render
from tests.media import make_click_track, make_photo, make_video


def _put(store, path, kind):
    data = path.read_bytes()
    up = store.create_upload(path.name, len(data), kind)
    for n in range(up.total_chunks):
        chunk = data[n * up.chunk_size:(n + 1) * up.chunk_size]
        store.write_chunk(up.upload_id, n, [chunk])
    return store.complete_upload(up.upload_id).file_id


@pytest.fixture
def uploaded(store, tmp_path):
    clips = [_put(store, make_video(tmp_path / f"v{i}.mp4", w=320, h=180, dur=8), "video")
             for i in range(3)]
    photos = [_put(store, make_photo(tmp_path / "p.jpg", w=800, h=600), "photo")]
    music = _put(store, make_click_track(tmp_path / "m.wav", bpm=120, dur=40), "audio")
    return {"clips": clips, "photos": photos, "music": music}


def _body(u, **over):
    body = {"fileIds": u["clips"] + u["photos"], "musicFileId": u["music"], "aspect": "9:16",
            "pace": "medium", "length": 15, "seed": 1, "audioMode": "music"}
    body.update(over)
    return body


def _draft(client, auth, u, **over):
    r = client.post("/projects/draft", headers=auth, json=_body(u, **over))
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_draft_is_renderable(client, auth, store, uploaded):
    data = _draft(client, auth, uploaded)
    el = EditList.model_validate(data["editList"])
    validate_for_render(el, store.files())
    assert (el.project.width, el.project.height) == (1080, 1920)
    assert el.music.fileId == uploaded["music"] and el.music.beats
    assert el.music.beats[0] == el.music.startSec
    assert isinstance(data["warnings"], list)


@pytest.mark.parametrize("aspect,size", [("16:9", (1920, 1080)), ("4:5", (1080, 1350))])
def test_draft_aspect(client, auth, store, uploaded, aspect, size):
    el = EditList.model_validate(_draft(client, auth, uploaded, aspect=aspect)["editList"])
    assert (el.project.width, el.project.height) == size


def test_draft_all_length(client, auth, store, uploaded):
    el = EditList.model_validate(_draft(client, auth, uploaded, length="all", pace="calm")
                                 ["editList"])
    validate_for_render(el, store.files())
    assert el.music.endSec - el.music.startSec > 30


def test_draft_with_template_fills_empty_slots(client, auth, store, uploaded):
    template = EditList(
        version=1, project=Project(aspect="9:16", width=1080, height=1920, fps=30),
        music=Music(fileId="old", startSec=0, endSec=4.0), audioMode="music",
        timeline=[EmptySegment(type="empty", slotStartSec=t, durationSec=d)
                  for t, d in ((0.0, 1.0), (1.0, 2.0), (3.0, 1.0))])
    data = _draft(client, auth, uploaded, aspect="4:5", template=template.model_dump())
    el = EditList.model_validate(data["editList"])
    validate_for_render(el, store.files())
    assert all(s.type != "empty" for s in el.timeline)
    assert sum(s.durationSec for s in el.timeline) == pytest.approx(4.0)
    assert (el.project.aspect, el.project.width, el.project.height) == ("4:5", 1080, 1350)


def test_template_too_long_is_an_error(client, auth, uploaded):
    template = EditList(
        version=1, project=Project(aspect="9:16", width=1080, height=1920, fps=30),
        music=Music(fileId="old", startSec=0, endSec=120.0), audioMode="music",
        timeline=[EmptySegment(type="empty", slotStartSec=0, durationSec=120.0)])
    r = client.post("/projects/draft", headers=auth,
                    json=_body(uploaded, template=template.model_dump()))
    assert r.status_code == 422 and r.json()["code"] == "template_too_long"


def test_reshuffle_changes_draft(client, auth, uploaded):
    one = _draft(client, auth, uploaded, seed=1)["editList"]["timeline"]
    again = _draft(client, auth, uploaded, seed=1)["editList"]["timeline"]
    two = _draft(client, auth, uploaded, seed=2)["editList"]["timeline"]
    assert one == again and one != two


def test_file_id_order_does_not_change_draft(client, auth, uploaded):
    ids = uploaded["clips"] + uploaded["photos"]
    one = _draft(client, auth, uploaded, fileIds=ids)["editList"]
    two = _draft(client, auth, uploaded, fileIds=ids[::-1])["editList"]
    assert one == two


def test_draft_rejects_unknown_file(client, auth, uploaded):
    r = client.post("/projects/draft", headers=auth,
                    json=_body(uploaded, fileIds=["up_nope"]))
    assert r.status_code == 422 and r.json()["code"] == "unknown_file"
    r = client.post("/projects/draft", headers=auth,
                    json=_body(uploaded, musicFileId="up_nope"))
    assert r.status_code == 422 and r.json()["code"] == "unknown_file"


def test_draft_rejects_wrong_kinds(client, auth, uploaded):
    r = client.post("/projects/draft", headers=auth,
                    json=_body(uploaded, musicFileId=uploaded["clips"][0]))
    assert r.status_code == 422 and r.json()["code"] == "wrong_file_kind"
    r = client.post("/projects/draft", headers=auth,
                    json=_body(uploaded, fileIds=[uploaded["music"]]))
    assert r.status_code == 422 and r.json()["code"] == "wrong_file_kind"


def test_draft_rejects_bad_length(client, auth, uploaded):
    r = client.post("/projects/draft", headers=auth, json=_body(uploaded, length=20))
    assert r.status_code == 422 and r.json()["code"] == "invalid_request"


def test_analyze_returns_beats(client, auth, uploaded):
    r = client.post("/projects/analyze", headers=auth, json={"musicFileId": uploaded["music"]})
    d = r.json()["data"]
    assert r.status_code == 200 and abs(d["tempo"] - 120) <= 2 and len(d["beats"]) > 50
    assert abs(d["durationSec"] - 40) < 0.1


def test_analyze_rejects_non_audio(client, auth, uploaded):
    r = client.post("/projects/analyze", headers=auth, json={"musicFileId": uploaded["clips"][0]})
    assert r.status_code == 422 and r.json()["code"] == "wrong_file_kind"


def test_routes_require_user(client, uploaded):
    assert client.post("/projects/draft", json=_body(uploaded)).status_code == 401
    assert client.post("/projects/analyze", json={"musicFileId": "x"}).status_code == 401
