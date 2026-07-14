"""gemia.picture.matting — real subject/portrait matting (background removal).

This is the ML-backed replacement for the old honest-failure ``remove_background``.
It produces a genuine alpha matte for an arbitrary background, not a chroma/luma
key that only works on green screens or clean luminance.

Two tiers, chosen automatically at runtime:

1. **U2Net human-segmentation ONNX** (primary). A 320×320 saliency network
   whose coarse mask is refined edge-aware with a guided filter so hair and
   soft edges snap to the real image boundary. Runs on CPU via onnxruntime;
   no GPU or torch required. This is what CapCut/remove.bg-class cutouts use.

2. **GrabCut + face-anchored fallback** (no onnxruntime / model missing). A
   classical OpenCV pipeline seeded from a detected face box. Lower quality on
   busy backgrounds but fully offline and always available — so the tool never
   hard-fails again.

Public API:

    alpha = compute_alpha(image_bgr)          -> float32 [0,1], HxW
    rgba  = cutout_rgba(image_bgr)            -> uint8 HxWx4 (straight alpha)
    info  = describe_backend()               -> dict (which tier is live)

All heavy state (the ONNX session) is created once and cached at module level.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np

# --- ImageNet-style normalization used by the U2Net family (rembg-compatible)
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_NET_SIZE = 320


# --------------------------------------------------------------------------- #
# Model resolution                                                            #
# --------------------------------------------------------------------------- #
def _model_candidates() -> list[Path]:
    env = os.environ.get("LUMERI_MATTING_MODEL")
    cands: list[Path] = []
    if env:
        cands.append(Path(env).expanduser())
    repo_root = Path(__file__).resolve().parents[2]
    cands.append(repo_root / "models" / "u2net_human_seg.onnx")
    cands.append(repo_root / "models" / "u2net.onnx")
    cands.append(Path.home() / ".cache" / "lumeri" / "u2net_human_seg.onnx")
    cands.append(Path.home() / ".u2net" / "u2net_human_seg.onnx")  # rembg default
    return cands


def _find_model() -> Path | None:
    for p in _model_candidates():
        try:
            if p.is_file() and p.stat().st_size > 1_000_000:
                return p
        except OSError:
            continue
    return None


@lru_cache(maxsize=1)
def _session() -> Any | None:
    """Build (once) and cache the onnxruntime session, or None if unavailable."""
    model = _find_model()
    if model is None:
        return None
    try:
        import onnxruntime as ort  # noqa: PLC0415
    except Exception:
        return None
    providers = ["CPUExecutionProvider"]
    if os.environ.get("LUMERI_MATTING_COREML") == "1":
        avail = set(ort.get_available_providers())
        if "CoreMLExecutionProvider" in avail:
            providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    try:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("LUMERI_MATTING_THREADS", "0")) or 0
        return ort.InferenceSession(str(model), sess_options=opts, providers=providers)
    except Exception:
        return None


def describe_backend() -> dict[str, Any]:
    sess = _session()
    if sess is not None:
        return {"backend": "u2net_onnx", "model": str(_find_model()), "ml": True}
    return {"backend": "grabcut_fallback", "model": None, "ml": False}


# --------------------------------------------------------------------------- #
# Guided filter (edge-aware alpha refinement) — He et al. 2010, cv2-only       #
# --------------------------------------------------------------------------- #
def _guided_filter(guide: np.ndarray, src: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """Edge-preserving refine ``src`` using single-channel ``guide`` (both float32 [0,1])."""
    g = guide.astype(np.float32)
    p = src.astype(np.float32)
    d = (radius, radius)
    mean_g = cv2.boxFilter(g, cv2.CV_32F, d)
    mean_p = cv2.boxFilter(p, cv2.CV_32F, d)
    corr_gg = cv2.boxFilter(g * g, cv2.CV_32F, d)
    corr_gp = cv2.boxFilter(g * p, cv2.CV_32F, d)
    var_g = corr_gg - mean_g * mean_g
    cov_gp = corr_gp - mean_g * mean_p
    a = cov_gp / (var_g + eps)
    b = mean_p - a * mean_g
    mean_a = cv2.boxFilter(a, cv2.CV_32F, d)
    mean_b = cv2.boxFilter(b, cv2.CV_32F, d)
    return mean_a * g + mean_b


# --------------------------------------------------------------------------- #
# U2Net inference                                                             #
# --------------------------------------------------------------------------- #
def _u2net_alpha(image_bgr: np.ndarray, sess: Any) -> np.ndarray:
    h, w = image_bgr.shape[:2]
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    small = cv2.resize(rgb, (_NET_SIZE, _NET_SIZE), interpolation=cv2.INTER_LANCZOS4)
    x = small.astype(np.float32)
    mx = float(x.max())
    x = x / (mx if mx > 0 else 1.0)          # rembg divides by max pixel, not 255
    x = (x - _MEAN) / _STD
    x = np.transpose(x, (2, 0, 1))[None].astype(np.float32)  # 1x3xHxW

    in_name = sess.get_inputs()[0].name
    pred = sess.run(None, {in_name: x})[0]   # main map d0: 1x1xHxW
    pred = pred[0, 0]
    mn, mx = float(pred.min()), float(pred.max())
    pred = (pred - mn) / (mx - mn) if mx > mn else pred * 0.0

    coarse = cv2.resize(pred, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(coarse, 0.0, 1.0).astype(np.float32)


def _refine_alpha(image_bgr: np.ndarray, coarse: np.ndarray) -> np.ndarray:
    """Snap the coarse ML alpha to real image edges (hair, soft boundaries)."""
    h, w = coarse.shape[:2]
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    # radius scales with image size so refinement is resolution-independent.
    radius = max(4, int(round(min(h, w) * 0.012)))
    refined = _guided_filter(gray, coarse, radius=radius, eps=1e-4)
    refined = np.clip(refined, 0.0, 1.0)
    # Firm up the transition: push confident fg/bg toward 0/1, keep the band soft.
    # The 0.52 pivot (vs 0.5) trims the milky low-alpha skirt that reads as a
    # halo on contrasting backgrounds, without eating genuine hair (which the
    # coarse net scores well above the pivot).
    refined = np.clip((refined - 0.52) * 1.5 + 0.5, 0.0, 1.0)
    return refined.astype(np.float32)


def _decontaminate(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Remove residual background color from the partial-alpha edge band.

    Foreground-color estimation via normalized (push-pull) blur: diffuse the
    color of near-opaque pixels into the uncertain band so a keyed edge no
    longer carries the old background's tint (green spill, dark/white halo).
    This is the defringe/decontamination step pro matting tools apply.
    """
    h, w = alpha.shape[:2]
    rgb_f = rgb.astype(np.float32)
    conf = np.clip((alpha - 0.80) / 0.20, 0.0, 1.0)  # 1 where solidly foreground
    est = rgb_f.copy()
    base = max(2.0, min(h, w) * 0.006)
    for sigma in (base, base * 2.5, base * 6.0):     # coarse-to-fine fill
        num = cv2.GaussianBlur(est * conf[..., None], (0, 0), sigmaX=sigma)
        den = cv2.GaussianBlur(conf, (0, 0), sigmaX=sigma)[..., None] + 1e-6
        filled = num / den
        # only trust the fill where we don't already have a confident colour
        take = (1.0 - conf)[..., None]
        est = est * (1.0 - take) + filled * take
    # blend estimate in only across the uncertain band; opaque core untouched.
    band = ((alpha > 0.02) & (alpha < 0.98)).astype(np.float32)[..., None]
    mix = band * (1.0 - conf[..., None])
    out = rgb_f * (1.0 - mix) + est * mix
    return np.clip(out, 0.0, 255.0).astype(np.uint8)


# --------------------------------------------------------------------------- #
# GrabCut fallback (no ML model)                                             #
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _face_cascade() -> Any | None:
    try:
        path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        c = cv2.CascadeClassifier(path)
        return c if not c.empty() else None
    except Exception:
        return None


def _grabcut_alpha(image_bgr: np.ndarray) -> np.ndarray:
    h, w = image_bgr.shape[:2]
    mask = np.full((h, w), cv2.GC_PR_BGD, np.uint8)

    # Seed a probable-foreground rectangle: face-anchored if we find a face,
    # else a centered portrait column.
    cascade = _face_cascade()
    rect = None
    if cascade is not None:
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(int(w * 0.06), int(h * 0.06)))
        if len(faces):
            fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])
            cx = fx + fw / 2.0
            # torso spreads below/around the face; widen and extend downward.
            bx0 = int(max(0, cx - fw * 1.9))
            bx1 = int(min(w, cx + fw * 1.9))
            by0 = int(max(0, fy - fh * 0.9))
            by1 = h  # portraits run to the bottom edge
            rect = (bx0, by0, max(1, bx1 - bx0), max(1, by1 - by0))
    if rect is None:
        rect = (int(w * 0.14), int(h * 0.06), int(w * 0.72), int(h * 0.94))

    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(image_bgr, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        a = np.zeros((h, w), np.float32)
        x, y, rw, rh = rect
        a[y:y + rh, x:x + rw] = 1.0
        return a
    alpha = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 1.0, 0.0).astype(np.float32)
    # Keep the largest connected component (drops stray background blobs).
    alpha = _largest_component(alpha)
    alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=max(1.0, min(h, w) * 0.004))
    return np.clip(alpha, 0.0, 1.0)


def _largest_component(alpha: np.ndarray) -> np.ndarray:
    binary = (alpha > 0.5).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    if n <= 1:
        return alpha
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    keep = (labels == largest).astype(np.float32)
    return alpha * keep


# --------------------------------------------------------------------------- #
# Public entry points                                                        #
# --------------------------------------------------------------------------- #
def compute_alpha(image_bgr: np.ndarray) -> np.ndarray:
    """Return a float32 [0,1] alpha matte the same HxW as ``image_bgr``."""
    if image_bgr is None or image_bgr.size == 0:
        raise ValueError("compute_alpha: empty image")
    if image_bgr.ndim == 2:
        image_bgr = cv2.cvtColor(image_bgr, cv2.COLOR_GRAY2BGR)
    if image_bgr.shape[2] == 4:
        image_bgr = cv2.cvtColor(image_bgr, cv2.COLOR_BGRA2BGR)

    sess = _session()
    if sess is not None:
        coarse = _u2net_alpha(image_bgr, sess)
        return _refine_alpha(image_bgr, coarse)
    return _grabcut_alpha(image_bgr)


def _read_bgr(path: str | Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        # cv2 can choke on some formats; fall back to PIL.
        from PIL import Image  # noqa: PLC0415
        img = cv2.cvtColor(np.array(Image.open(path).convert("RGB")), cv2.COLOR_RGB2BGR)
    return img


def cutout_rgba(image_bgr: np.ndarray, *, feather: float = 0.0) -> np.ndarray:
    """Straight-alpha RGBA uint8 cutout (RGB order for PNG writers)."""
    alpha = compute_alpha(image_bgr)
    if feather and feather > 0:
        alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=float(feather))
        alpha = np.clip(alpha, 0.0, 1.0)
    rgb = _decontaminate(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB), alpha)
    a = (alpha * 255.0 + 0.5).astype(np.uint8)
    return np.dstack([rgb, a])


def _resolve_background(spec: Any, h: int, w: int) -> np.ndarray | None:
    """Return an HxWx3 RGB background, or None for transparent."""
    if spec is None:
        return None
    if isinstance(spec, (list, tuple)) and len(spec) == 3:
        r, g, b = (int(v) for v in spec)
        return np.full((h, w, 3), (r, g, b), np.uint8)
    if isinstance(spec, str):
        named = {
            "white": (255, 255, 255), "black": (0, 0, 0),
            "green": (0, 177, 64), "blue": (0, 71, 187), "gray": (128, 128, 128),
        }
        if spec.lower() in named:
            return np.full((h, w, 3), named[spec.lower()], np.uint8)
        p = Path(spec).expanduser()
        if p.is_file():
            bg = _read_bgr(p)
            bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_AREA)
            return cv2.cvtColor(bg, cv2.COLOR_BGR2RGB)
    return None


def remove_background(
    src_path: str | Path,
    out_path: str | Path,
    *,
    background: Any = None,
    feather: float = 0.0,
    matte_only: bool = False,
) -> dict[str, Any]:
    """Cut the subject out of ``src_path`` and write the result to ``out_path``.

    ``background`` None -> transparent PNG; a ``[r,g,b]`` / named color / image
    path -> composite the subject over it. ``matte_only`` writes the grayscale
    alpha instead of the cutout. Returns quality/backend metadata.
    """
    image_bgr = _read_bgr(src_path)
    h, w = image_bgr.shape[:2]
    alpha = compute_alpha(image_bgr)
    if feather and feather > 0:
        alpha = np.clip(cv2.GaussianBlur(alpha, (0, 0), sigmaX=float(feather)), 0.0, 1.0)

    out_path = Path(out_path)
    if matte_only:
        cv2.imwrite(str(out_path), (alpha * 255.0 + 0.5).astype(np.uint8))
    else:
        rgb = _decontaminate(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB), alpha)
        bg = _resolve_background(background, h, w)
        if bg is None:
            a = (alpha * 255.0 + 0.5).astype(np.uint8)
            rgba = np.dstack([rgb, a])
            # PNG expects RGBA; cv2.imwrite wants BGRA.
            cv2.imwrite(str(out_path), cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
        else:
            a3 = alpha[..., None]
            comp = (rgb.astype(np.float32) * a3 + bg.astype(np.float32) * (1.0 - a3))
            comp = np.clip(comp, 0, 255).astype(np.uint8)
            cv2.imwrite(str(out_path), cv2.cvtColor(comp, cv2.COLOR_RGB2BGR))

    coverage = float(alpha.mean())
    ys, xs = np.where(alpha > 0.5)
    bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())] if xs.size else None
    # Edge softness: fraction of pixels in the semi-transparent band — a proxy
    # for how much fine (hair) detail the matte preserved.
    band = float(np.mean((alpha > 0.05) & (alpha < 0.95)))
    info = describe_backend()
    return {
        "backend": info["backend"],
        "ml": info["ml"],
        "coverage": round(coverage, 4),
        "edge_band": round(band, 4),
        "bbox": bbox,
        "width": w,
        "height": h,
        "transparent": background is None and not matte_only,
    }


__all__ = ["compute_alpha", "cutout_rgba", "remove_background", "describe_backend"]
