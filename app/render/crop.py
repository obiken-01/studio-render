import math


def _even_up(value: float) -> int:
    n = math.ceil(round(value, 6))
    return n + (n % 2)


def cover_crop(src_w: int, src_h: int, out_w: int, out_h: int,
               focus_x: float, focus_y: float) -> tuple[int, int, int, int]:
    """Scale so the source covers the output, then crop around the focus point.

    Returns (scaled_w, scaled_h, x, y); scaled dimensions are rounded up to even numbers.
    """
    s = max(out_w / src_w, out_h / src_h)
    sw = max(_even_up(src_w * s), out_w)
    sh = max(_even_up(src_h * s), out_h)
    x = min(max(round(focus_x * sw - out_w / 2), 0), sw - out_w)
    y = min(max(round(focus_y * sh - out_h / 2), 0), sh - out_h)
    return sw, sh, x, y
