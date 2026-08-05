"""Pure vector math for the motion engine — no I/O, no renderer, no randomness.

Everything downstream (scene IR, behaviours, the SVG compiler) speaks the
geometry defined here:

* a **path** is a list of segment tuples in absolute coordinates, a direct
  mirror of SVG path commands: ``("M", x, y)``, ``("L", x, y)``,
  ``("Q", cx, cy, x, y)``, ``("C", c1x, c1y, c2x, c2y, x, y)``, ``("Z",)``.
* shape generators (rect / ellipse / polygon / star / ring …) return paths.
* :func:`resample_path` normalises any path to N cubic segments so two
  arbitrary shapes become morph-compatible (same command list, only numbers
  differ — exactly what CSS ``d: path()`` interpolation requires).
* :func:`point_at` / :func:`path_length` support motion paths and draw-on.

Determinism: the only stochastic helper is :func:`scatter`, and it takes an
explicit ``random.Random`` — nothing in this module ever seeds itself.
"""
from __future__ import annotations

import math
import random
from typing import Iterable, Sequence

Vec = tuple[float, float]
Segment = tuple  # ("M",x,y) | ("L",x,y) | ("Q",cx,cy,x,y) | ("C",...) | ("Z",)
Path = list

#: Decimal places kept when serialising path numbers (enough for sub-pixel
#: fidelity at 4K while keeping documents byte-stable and diff-friendly).
NDIGITS = 3


# ── basic vector ops ─────────────────────────────────────────────────────


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def vlerp(a: Vec, b: Vec, t: float) -> Vec:
    return (lerp(a[0], b[0], t), lerp(a[1], b[1], t))


def vadd(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1])


def vsub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1])


def vscale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s)


def vlen(a: Vec) -> float:
    return math.hypot(a[0], a[1])


def vnorm(a: Vec) -> Vec:
    n = vlen(a)
    if n <= 1e-12:
        return (0.0, 0.0)
    return (a[0] / n, a[1] / n)


def vrot(a: Vec, degrees: float) -> Vec:
    r = math.radians(degrees)
    c, s = math.cos(r), math.sin(r)
    return (a[0] * c - a[1] * s, a[0] * s + a[1] * c)


# ── bezier evaluation ────────────────────────────────────────────────────


def cubic_point(p0: Vec, p1: Vec, p2: Vec, p3: Vec, t: float) -> Vec:
    """Point on a cubic bezier at parameter ``t`` ∈ [0, 1] (de Casteljau)."""
    a, b, c = vlerp(p0, p1, t), vlerp(p1, p2, t), vlerp(p2, p3, t)
    d, e = vlerp(a, b, t), vlerp(b, c, t)
    return vlerp(d, e, t)


def quad_point(p0: Vec, p1: Vec, p2: Vec, t: float) -> Vec:
    a, b = vlerp(p0, p1, t), vlerp(p1, p2, t)
    return vlerp(a, b, t)


def quad_to_cubic(p0: Vec, p1: Vec, p2: Vec) -> tuple[Vec, Vec, Vec, Vec]:
    """Exact degree elevation of a quadratic bezier to a cubic."""
    c1 = vadd(p0, vscale(vsub(p1, p0), 2.0 / 3.0))
    c2 = vadd(p2, vscale(vsub(p1, p2), 2.0 / 3.0))
    return p0, c1, c2, p2


def split_cubic(
    p0: Vec, p1: Vec, p2: Vec, p3: Vec, t: float
) -> tuple[tuple[Vec, Vec, Vec, Vec], tuple[Vec, Vec, Vec, Vec]]:
    """Split one cubic into two at ``t`` (de Casteljau), exactly."""
    a, b, c = vlerp(p0, p1, t), vlerp(p1, p2, t), vlerp(p2, p3, t)
    d, e = vlerp(a, b, t), vlerp(b, c, t)
    f = vlerp(d, e, t)
    return (p0, a, d, f), (f, e, c, p3)


# ── path structure ───────────────────────────────────────────────────────


def _seg_points(seg: Segment, cursor: Vec, start: Vec) -> list[Vec]:
    """Control polygon of a segment, cursor included (for flattening)."""
    kind = seg[0]
    if kind == "M" or kind == "L":
        return [cursor, (seg[1], seg[2])]
    if kind == "Q":
        p0, c1, c2, p1 = quad_to_cubic(cursor, (seg[1], seg[2]), (seg[3], seg[4]))
        return [p0, c1, c2, p1]
    if kind == "C":
        return [cursor, (seg[1], seg[2]), (seg[3], seg[4]), (seg[5], seg[6])]
    if kind == "Z":
        return [cursor, start]
    raise ValueError(f"unknown path segment {kind!r}")


def iter_cubics(path: Path) -> Iterable[tuple[Vec, Vec, Vec, Vec]]:
    """Every drawable segment of ``path`` as an absolute cubic bezier.

    ``M`` moves the cursor (emits nothing); ``L`` / ``Z`` become straight-line
    cubics (control points on the line at 1/3 and 2/3) so every consumer deals
    with exactly one primitive.
    """
    cursor: Vec = (0.0, 0.0)
    start: Vec = (0.0, 0.0)
    for seg in path:
        kind = seg[0]
        if kind == "M":
            cursor = (seg[1], seg[2])
            start = cursor
            continue
        pts = _seg_points(seg, cursor, start)
        if len(pts) == 2:
            p0, p3 = pts
            if vlen(vsub(p3, p0)) <= 1e-12 and kind == "Z":
                cursor = start
                continue
            c1 = vlerp(p0, p3, 1.0 / 3.0)
            c2 = vlerp(p0, p3, 2.0 / 3.0)
            yield (p0, c1, c2, p3)
            cursor = p3 if kind != "Z" else start
        else:
            yield tuple(pts)  # type: ignore[misc]
            cursor = pts[3]
        if kind == "Z":
            cursor = start


def is_closed(path: Path) -> bool:
    return any(seg[0] == "Z" for seg in path)


def path_length(path: Path, *, samples_per_seg: int = 24) -> float:
    """Approximate arc length by uniform flattening (stable, deterministic)."""
    total = 0.0
    for p0, c1, c2, p3 in iter_cubics(path):
        prev = p0
        for i in range(1, samples_per_seg + 1):
            pt = cubic_point(p0, c1, c2, p3, i / samples_per_seg)
            total += vlen(vsub(pt, prev))
            prev = pt
    return total


def point_at(path: Path, t: float, *, samples_per_seg: int = 24) -> Vec:
    """Point at normalised arc-length position ``t`` ∈ [0, 1] along ``path``."""
    t = min(max(t, 0.0), 1.0)
    # Flatten once, walk to the target distance.
    pts: list[Vec] = []
    for p0, c1, c2, p3 in iter_cubics(path):
        if not pts:
            pts.append(p0)
        for i in range(1, samples_per_seg + 1):
            pts.append(cubic_point(p0, c1, c2, p3, i / samples_per_seg))
    if not pts:
        return (0.0, 0.0)
    if len(pts) == 1 or t <= 0.0:
        return pts[0]
    lengths = [vlen(vsub(b, a)) for a, b in zip(pts, pts[1:])]
    total = sum(lengths)
    if total <= 1e-12:
        return pts[0]
    target = t * total
    acc = 0.0
    for (a, b), L in zip(zip(pts, pts[1:]), lengths):
        if acc + L >= target:
            f = 0.0 if L <= 1e-12 else (target - acc) / L
            return vlerp(a, b, f)
        acc += L
    return pts[-1]


def bbox(path: Path, *, samples_per_seg: int = 12) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y) of the flattened path."""
    xs: list[float] = []
    ys: list[float] = []
    for p0, c1, c2, p3 in iter_cubics(path):
        for i in range(samples_per_seg + 1):
            x, y = cubic_point(p0, c1, c2, p3, i / samples_per_seg)
            xs.append(x)
            ys.append(y)
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def centroid(path: Path) -> Vec:
    x0, y0, x1, y1 = bbox(path)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def translate_path(path: Path, dx: float, dy: float) -> Path:
    out: Path = []
    for seg in path:
        kind = seg[0]
        if kind == "Z":
            out.append(("Z",))
            continue
        nums = list(seg[1:])
        for i in range(0, len(nums), 2):
            nums[i] += dx
            nums[i + 1] += dy
        out.append((kind, *nums))
    return out


def scale_path(path: Path, sx: float, sy: float | None = None, *, origin: Vec = (0.0, 0.0)) -> Path:
    sy = sx if sy is None else sy
    ox, oy = origin
    out: Path = []
    for seg in path:
        kind = seg[0]
        if kind == "Z":
            out.append(("Z",))
            continue
        nums = list(seg[1:])
        for i in range(0, len(nums), 2):
            nums[i] = ox + (nums[i] - ox) * sx
            nums[i + 1] = oy + (nums[i + 1] - oy) * sy
        out.append((kind, *nums))
    return out


def rotate_path(path: Path, degrees: float, *, origin: Vec = (0.0, 0.0)) -> Path:
    out: Path = []
    for seg in path:
        kind = seg[0]
        if kind == "Z":
            out.append(("Z",))
            continue
        nums = list(seg[1:])
        for i in range(0, len(nums), 2):
            x, y = vrot((nums[i] - origin[0], nums[i + 1] - origin[1]), degrees)
            nums[i] = x + origin[0]
            nums[i + 1] = y + origin[1]
        out.append((kind, *nums))
    return out


# ── morph preparation ────────────────────────────────────────────────────


def resample_path(path: Path, n_segments: int) -> Path:
    """Rebuild ``path`` as exactly ``n_segments`` cubic segments.

    Segments are allocated across the existing cubics proportionally to arc
    length, then produced by exact de Casteljau splitting — the resampled
    path traces the *same* curve (no flattening error), it just has a
    normalised command list. Two paths resampled to the same ``n_segments``
    are structurally interpolation-compatible: identical command sequence
    ``M`` + n×``C`` (+ ``Z`` when the source was closed), only numbers differ.
    """
    if n_segments < 1:
        raise ValueError("n_segments must be >= 1")
    # A path with more than one ``M`` is multiple subpaths; resampling treats
    # them as one contour and would paint a bridge segment across the gap.
    # Callers that need morphable multi-subpath geometry must split first.
    if sum(1 for seg in path if seg and seg[0] == "M") > 1:
        raise ValueError(
            "resample_path does not support multi-subpath geometry "
            "(more than one 'M'); split into subpaths first"
        )
    cubics = list(iter_cubics(path))
    if not cubics:
        raise ValueError("cannot resample an empty path")

    lengths = [path_length([("M", c[0][0], c[0][1]), ("C", c[1][0], c[1][1], c[2][0], c[2][1], c[3][0], c[3][1])]) for c in cubics]
    total = sum(lengths) or 1.0

    # How many output segments each input cubic yields (>= 1 where possible).
    counts = [0] * len(cubics)
    if n_segments >= len(cubics):
        counts = [1] * len(cubics)
        remaining = n_segments - len(cubics)
        # Largest-remainder allocation of the extra segments by length share.
        shares = [L / total * remaining for L in lengths]
        base = [int(s) for s in shares]
        remaining -= sum(base)
        order = sorted(range(len(cubics)), key=lambda i: shares[i] - base[i], reverse=True)
        for i in range(len(cubics)):
            counts[i] += base[i]
        for i in order[:remaining]:
            counts[i] += 1
    else:
        # Fewer output segments than input cubics: merge by even index blocks.
        # (Rare — callers normally resample UP. Approximate by sampling points.)
        pts = [point_at(path, i / n_segments) for i in range(n_segments + 1)]
        out: Path = [("M", pts[0][0], pts[0][1])]
        for a, b in zip(pts, pts[1:]):
            c1 = vlerp(a, b, 1.0 / 3.0)
            c2 = vlerp(a, b, 2.0 / 3.0)
            out.append(("C", c1[0], c1[1], c2[0], c2[1], b[0], b[1]))
        if is_closed(path):
            out.append(("Z",))
        return out

    first = cubics[0][0]
    out2: Path = [("M", first[0], first[1])]
    for cubic, count in zip(cubics, counts):
        pieces = [cubic]
        for k in range(count - 1, 0, -1):
            # Split the remaining tail so pieces come out equal in parameter.
            head, tail = split_cubic(*pieces.pop(), 1.0 / (k + 1))
            pieces.append(head)
            pieces.append(tail)
        for p0, c1, c2, p3 in pieces:
            out2.append(("C", c1[0], c1[1], c2[0], c2[1], p3[0], p3[1]))
    if is_closed(path):
        out2.append(("Z",))
    return out2


def align_for_morph(a: Path, b: Path, *, max_segments: int = 64) -> tuple[Path, Path]:
    """Return ``(a', b')`` resampled to a shared segment count for morphing.

    The shared count is the larger of the two paths' drawable segment counts
    (capped at ``max_segments``), so neither shape loses detail. Closedness
    must match for a clean morph; when it differs, both are treated as open
    (the closing edge is baked in as a segment before resampling).
    """
    ca, cb = list(iter_cubics(a)), list(iter_cubics(b))
    n = max(len(ca), len(cb), 1)
    n = min(n, max_segments)
    a2, b2 = a, b
    if is_closed(a) != is_closed(b):
        a2 = bake_close(a)
        b2 = bake_close(b)
    ra, rb = resample_path(a2, n), resample_path(b2, n)
    # Structural belt-and-braces: closedness must now agree.
    if is_closed(ra) != is_closed(rb):
        ra = [s for s in ra if s[0] != "Z"]
        rb = [s for s in rb if s[0] != "Z"]
    return ra, rb


def bake_close(path: Path) -> Path:
    """Replace a trailing ``Z`` with an explicit line back to the start."""
    if not is_closed(path):
        return list(path)
    out: Path = []
    start: Vec | None = None
    cursor: Vec = (0.0, 0.0)
    for seg in path:
        if seg[0] == "M":
            start = (seg[1], seg[2])
            cursor = start
            out.append(seg)
        elif seg[0] == "Z":
            if start is not None and vlen(vsub(cursor, start)) > 1e-9:
                out.append(("L", start[0], start[1]))
            cursor = start or cursor
        else:
            out.append(seg)
            cursor = (seg[-2], seg[-1])
    return out


# ── shape generators (all return paths centred on ``center``) ────────────


def line(a: Vec, b: Vec) -> Path:
    return [("M", a[0], a[1]), ("L", b[0], b[1])]


def polyline(points: Sequence[Vec], *, closed: bool = False) -> Path:
    if len(points) < 2:
        raise ValueError("polyline needs >= 2 points")
    path: Path = [("M", points[0][0], points[0][1])]
    for p in points[1:]:
        path.append(("L", p[0], p[1]))
    if closed:
        path.append(("Z",))
    return path


def rect(center: Vec = (0.0, 0.0), width: float = 100.0, height: float = 100.0, *, radius: float = 0.0) -> Path:
    cx, cy = center
    w2, h2 = width / 2.0, height / 2.0
    r = min(max(radius, 0.0), w2, h2)
    if r <= 1e-9:
        return polyline(
            [(cx - w2, cy - h2), (cx + w2, cy - h2), (cx + w2, cy + h2), (cx - w2, cy + h2)],
            closed=True,
        )
    # Rounded corners as quarter-circle cubics (kappa approximation).
    k = 0.5522847498307936 * r
    x0, y0, x1, y1 = cx - w2, cy - h2, cx + w2, cy + h2
    return [
        ("M", x0 + r, y0),
        ("L", x1 - r, y0),
        ("C", x1 - r + k, y0, x1, y0 + r - k, x1, y0 + r),
        ("L", x1, y1 - r),
        ("C", x1, y1 - r + k, x1 - r + k, y1, x1 - r, y1),
        ("L", x0 + r, y1),
        ("C", x0 + r - k, y1, x0, y1 - r + k, x0, y1 - r),
        ("L", x0, y0 + r),
        ("C", x0, y0 + r - k, x0 + r - k, y0, x0 + r, y0),
        ("Z",),
    ]


def ellipse(center: Vec = (0.0, 0.0), rx: float = 50.0, ry: float | None = None) -> Path:
    """Ellipse as four kappa cubics — morph- and draw-on-friendly."""
    ry = rx if ry is None else ry
    cx, cy = center
    kx, ky = 0.5522847498307936 * rx, 0.5522847498307936 * ry
    return [
        ("M", cx, cy - ry),
        ("C", cx + kx, cy - ry, cx + rx, cy - ky, cx + rx, cy),
        ("C", cx + rx, cy + ky, cx + kx, cy + ry, cx, cy + ry),
        ("C", cx - kx, cy + ry, cx - rx, cy + ky, cx - rx, cy),
        ("C", cx - rx, cy - ky, cx - kx, cy - ry, cx, cy - ry),
        ("Z",),
    ]


def circle(center: Vec = (0.0, 0.0), r: float = 50.0) -> Path:
    return ellipse(center, r, r)


def polygon(center: Vec = (0.0, 0.0), r: float = 50.0, sides: int = 6, *, rotation: float = 0.0) -> Path:
    if sides < 3:
        raise ValueError("polygon needs >= 3 sides")
    pts = []
    for i in range(sides):
        ang = math.radians(rotation - 90.0 + 360.0 * i / sides)
        pts.append((center[0] + r * math.cos(ang), center[1] + r * math.sin(ang)))
    return polyline(pts, closed=True)


def star(
    center: Vec = (0.0, 0.0),
    outer: float = 50.0,
    inner: float | None = None,
    points: int = 5,
    *,
    rotation: float = 0.0,
) -> Path:
    if points < 3:
        raise ValueError("star needs >= 3 points")
    inner = outer * 0.42 if inner is None else inner
    pts = []
    for i in range(points * 2):
        r = outer if i % 2 == 0 else inner
        ang = math.radians(rotation - 90.0 + 180.0 * i / points)
        pts.append((center[0] + r * math.cos(ang), center[1] + r * math.sin(ang)))
    return polyline(pts, closed=True)


def arc(center: Vec, r: float, start_deg: float, end_deg: float) -> Path:
    """Open circular arc as cubic segments (≤ 90° per segment)."""
    sweep = end_deg - start_deg
    if abs(sweep) < 1e-9:
        raise ValueError("arc sweep must be non-zero")
    n = max(1, math.ceil(abs(sweep) / 90.0))
    step = sweep / n
    cx, cy = center

    def pt(deg: float) -> Vec:
        a = math.radians(deg)
        return (cx + r * math.cos(a), cy + r * math.sin(a))

    path: Path = [("M", *pt(start_deg))]
    for i in range(n):
        a0 = start_deg + step * i
        a1 = a0 + step
        # Standard arc-to-cubic control distance.
        alpha = math.radians(step) / 2.0
        k = (4.0 / 3.0) * math.tan(alpha / 2.0) * r
        p0, p3 = pt(a0), pt(a1)
        t0 = math.radians(a0) + math.pi / 2.0
        t1 = math.radians(a1) - math.pi / 2.0
        sgn = 1.0 if step >= 0 else -1.0
        c1 = (p0[0] + sgn * k * math.cos(t0), p0[1] + sgn * k * math.sin(t0))
        c2 = (p3[0] + sgn * k * math.cos(t1), p3[1] + sgn * k * math.sin(t1))
        path.append(("C", c1[0], c1[1], c2[0], c2[1], p3[0], p3[1]))
    return path


def smooth_through(points: Sequence[Vec], *, closed: bool = False, tension: float = 0.5) -> Path:
    """Smooth Catmull-Rom-style curve through ``points``, as cubics.

    ``tension`` 0 → straight polyline feel, 1 → very loose/organic. The
    default 0.5 matches the classic Catmull-Rom look. This is the workhorse
    for organic / liquid / wave shapes.
    """
    n = len(points)
    if n < 2:
        raise ValueError("smooth_through needs >= 2 points")
    pts = list(points)
    path: Path = [("M", pts[0][0], pts[0][1])]
    count = n if closed else n - 1
    for i in range(count):
        p1 = pts[i % n]
        p2 = pts[(i + 1) % n]
        p0 = pts[(i - 1) % n] if (closed or i > 0) else p1
        p3 = pts[(i + 2) % n] if (closed or i + 2 < n) else p2
        s = tension / 3.0
        c1 = vadd(p1, vscale(vsub(p2, p0), s))
        c2 = vsub(p2, vscale(vsub(p3, p1), s))
        path.append(("C", c1[0], c1[1], c2[0], c2[1], p2[0], p2[1]))
    if closed:
        path.append(("Z",))
    return path


def blob(center: Vec, r: float, *, wobble: float, lobes: int, rng: random.Random) -> Path:
    """Organic closed blob: a circle whose radius wobbles per lobe.

    ``wobble`` ∈ [0, 1] scales how far lobes deviate from the base radius.
    Deterministic for a given ``rng`` state — pass ``random.Random(seed)``.
    """
    lobes = max(3, int(lobes))
    pts: list[Vec] = []
    for i in range(lobes):
        ang = 2.0 * math.pi * i / lobes
        rr = r * (1.0 + wobble * (rng.random() * 2.0 - 1.0) * 0.5)
        pts.append((center[0] + rr * math.cos(ang), center[1] + rr * math.sin(ang)))
    return smooth_through(pts, closed=True, tension=0.9)


# ── sampling / scattering ────────────────────────────────────────────────


def sample_on_path(path: Path, count: int) -> list[Vec]:
    """``count`` points spread evenly (by arc length) along the path."""
    if count < 1:
        return []
    if count == 1:
        return [point_at(path, 0.5)]
    closed = is_closed(path)
    span = count if closed else count - 1
    return [point_at(path, i / span) for i in range(count)]


def scatter(
    count: int,
    rng: random.Random,
    *,
    center: Vec = (0.0, 0.0),
    radius: float = 100.0,
) -> list[Vec]:
    """``count`` points uniformly scattered in a disc (deterministic per rng)."""
    pts: list[Vec] = []
    for _ in range(max(0, int(count))):
        ang = rng.random() * 2.0 * math.pi
        rr = radius * math.sqrt(rng.random())
        pts.append((center[0] + rr * math.cos(ang), center[1] + rr * math.sin(ang)))
    return pts


# ── serialisation ────────────────────────────────────────────────────────


def _fmt(v: float) -> str:
    r = round(float(v), NDIGITS)
    if r == int(r):
        return str(int(r))
    return f"{r:.{NDIGITS}f}".rstrip("0").rstrip(".")


def to_svg_d(path: Path) -> str:
    """Serialise a path to an SVG ``d`` attribute string."""
    parts: list[str] = []
    for seg in path:
        kind = seg[0]
        if kind == "Z":
            parts.append("Z")
        else:
            parts.append(kind + " " + " ".join(_fmt(v) for v in seg[1:]))
    return " ".join(parts)
