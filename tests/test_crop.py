from app.render.crop import cover_crop


def test_16x9_into_9x16_centered():
    assert cover_crop(3840, 2160, 1080, 1920, 0.5, 0.5) == (3414, 1920, 1167, 0)


def test_focus_clamps_at_edges():
    sw, sh, x, y = cover_crop(3840, 2160, 1080, 1920, 0.0, 0.5)
    assert x == 0
    sw, sh, x, y = cover_crop(3840, 2160, 1080, 1920, 1.0, 0.5)
    assert x == sw - 1080


def test_same_aspect_is_plain_scale():
    assert cover_crop(3840, 2160, 1920, 1080, 0.5, 0.5) == (1920, 1080, 0, 0)


def test_portrait_into_4x5():
    assert cover_crop(1080, 1920, 1080, 1350, 0.5, 0.5) == (1080, 1920, 0, 285)
