"""gemia.picture — Image primitive operations.

All functions accept float32 [0, 1] BGR ndarrays and return the same.
Every function supports batch input via the @batchable decorator:
pass a single image or a list of images.
"""
from gemia.picture.geometry import resize, crop, rotate, perspective_transform
from gemia.picture.pixel import blur, sharpen, denoise, add_grain, convolve
from gemia.picture.color import color_grade, adjust_exposure, adjust_temperature, apply_lut
from gemia.picture.analysis import histogram, dominant_colors, edge_detect
from gemia.picture.composite import create_mask, blend, composite
from gemia.picture.generative import generate_image, edit_image, style_transfer, blend_images

__all__ = [
    # geometry
    "resize", "crop", "rotate", "perspective_transform",
    # pixel
    "blur", "sharpen", "denoise", "add_grain", "convolve",
    # color
    "color_grade", "adjust_exposure", "adjust_temperature", "apply_lut",
    # analysis
    "histogram", "dominant_colors", "edge_detect",
    # composite
    "create_mask", "blend", "composite",
    # generative (Nano Banana)
    "generate_image", "edit_image", "style_transfer", "blend_images",
]
