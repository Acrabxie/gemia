"""gemia.picture — Image primitive operations.

All functions accept float32 [0, 1] BGR ndarrays and return the same.
Every function supports batch input via the @batchable decorator:
pass a single image or a list of images.
"""
from gemia.picture.geometry import resize, crop, rotate, perspective_transform
from gemia.picture.pixel import blur, sharpen, denoise, add_grain, convolve
from gemia.picture.color import color_grade, adjust_exposure, adjust_temperature, apply_lut, colorslice_grade
from gemia.picture.analysis import histogram, dominant_colors, edge_detect
from gemia.picture.composite import create_mask, blend, composite
from gemia.picture.generative import generate_image, edit_image, style_transfer, blend_images
from gemia.picture.enhance import super_scale, match_color, skin_tone_protect, hdr_grade, film_grain_organic, defocus_background, relight, motion_blur, color_balance, image_flip, image_rotate, image_crop, image_resize_to_fit, image_add_border, image_grayscale, image_invert, image_posterize, image_solarize, image_pixelate, image_emboss, image_find_edges, image_smooth, image_auto_enhance, image_tint, image_watermark_text, image_rounded_corners, image_composite_alpha, image_adjust_hsl, image_resize_canvas, image_collage, image_sketch, image_oil_paint, image_cartoon, image_sepia, image_hdr_simulate, image_lens_blur, image_cross_process, image_halftone, image_noise, image_dither, image_clahe, image_palette_swap, image_channel_split, image_channel_merge, image_blend_overlay, image_blend_multiply, image_blend_screen, image_pixelate_region, image_text_overlay, image_draw_rect, image_histogram_equalize, image_mosaic, image_perspective_warp, image_normalize_brightness, image_split_quadrants, image_stitch_horizontal, image_stitch_vertical, image_radial_gradient, image_linear_gradient, image_detect_faces, image_grid_overlay, image_color_map, image_frames_to_gif, image_gif_to_frames, image_save_as, image_compare, image_mean_color, image_make_transparent, image_sobel, image_laplacian, image_canny, image_bilateral_blur, image_morphology, image_threshold, image_warp_fisheye, image_vignette, image_chromatic_aberration, image_focus_region, image_anaglyph, image_pixelate_mosaic, image_pencil_sketch, image_watercolor, image_stained_glass, image_ascii_art, image_noise_reduction, image_hue_shift, image_split_tone, image_color_burn, image_dodge, image_map_to_palette, image_lens_flare, image_duotone, image_pixelate_faces, image_simulate_print, image_glitch_datamosh, image_cartoon_cel, image_bump_map, image_color_quantize_dither, image_cross_hatch, image_soft_light, image_double_exposure, image_bokeh_blur, image_fog_effect, image_infrared, image_neon_glow, image_mirror_quad, image_color_dodge, image_sunbeams, image_pencil_color, image_selective_blur, image_light_leak, image_pixelate_grid, image_frost, image_color_halftone, image_relief, image_rainbow_gradient, image_tilt_shift, image_diffuse_glow, image_stipple, image_color_burn_blend, image_noise_stipple, image_gradient_map, image_cross_process, image_lomo, image_pixel_sort, image_mosaic_portrait, image_watermark_logo, image_orton_effect, image_scanline_art, image_color_overlay, image_warp_swirl, image_sketch_color, image_neon_outline, image_texture_overlay, image_color_shift_channels, image_glamour_glow, image_kaleidoscope, image_vintage_photo, image_paint_strokes, image_morning_haze, image_color_relief, image_glitter, image_watercolor_light, image_solarize_color, image_pixel_wave, image_crystallize, image_comic_dots, image_thermal, image_pastel_wash, image_prism_burst, image_blueprint_edges, image_paper_cutout, image_charcoal_smudge, image_cyanotype, image_noir_silhouette, image_risograph_dual, image_metallic_sheen, image_velvet_shadow, image_porcelain_glow, image_ember_bloom

__all__ = [
    # geometry
    "resize", "crop", "rotate", "perspective_transform",
    # pixel
    "blur", "sharpen", "denoise", "add_grain", "convolve",
    # color
    "color_grade", "adjust_exposure", "adjust_temperature", "apply_lut", "colorslice_grade",
    # analysis
    "histogram", "dominant_colors", "edge_detect",
    # composite
    "create_mask", "blend", "composite",
    # generative (GPT Image 2)
    "generate_image", "edit_image", "style_transfer", "blend_images",
    # enhance
    "super_scale", "match_color", "skin_tone_protect", "hdr_grade", "film_grain_organic",
    "defocus_background", "relight", "motion_blur", "color_balance", "image_flip", "image_rotate", "image_crop",
    "image_resize_to_fit", "image_add_border", "image_grayscale", "image_invert", "image_posterize",
    "image_solarize", "image_pixelate", "image_emboss", "image_find_edges", "image_smooth",
    "image_auto_enhance", "image_tint", "image_watermark_text", "image_rounded_corners",
    "image_composite_alpha", "image_adjust_hsl", "image_resize_canvas", "image_collage",
    "image_sketch", "image_oil_paint",
    "image_cartoon", "image_sepia",
    "image_hdr_simulate", "image_lens_blur",
    "image_cross_process", "image_halftone",
    "image_noise", "image_dither",
    "image_clahe", "image_palette_swap",
    "image_channel_split", "image_channel_merge",
    "image_blend_overlay", "image_blend_multiply",
    "image_blend_screen", "image_pixelate_region",
    "image_text_overlay", "image_draw_rect",
    "image_histogram_equalize", "image_mosaic",
    "image_perspective_warp", "image_normalize_brightness",
    "image_split_quadrants", "image_stitch_horizontal",
    "image_stitch_vertical", "image_radial_gradient",
    "image_linear_gradient", "image_detect_faces",
    "image_grid_overlay", "image_color_map",
    "image_frames_to_gif", "image_gif_to_frames",
    "image_save_as", "image_compare",
    "image_mean_color", "image_make_transparent",
    "image_sobel", "image_laplacian",
    "image_canny", "image_bilateral_blur",
    "image_morphology", "image_threshold",
    "image_warp_fisheye", "image_vignette",
    "image_chromatic_aberration", "image_focus_region",
    "image_anaglyph", "image_pixelate_mosaic",
    "image_pencil_sketch", "image_watercolor",
    "image_stained_glass", "image_ascii_art",
    "image_noise_reduction", "image_hue_shift",
    "image_split_tone", "image_color_burn",
    "image_dodge", "image_map_to_palette",
    "image_lens_flare", "image_duotone",
    "image_pixelate_faces", "image_simulate_print",
    "image_glitch_datamosh", "image_cartoon_cel",
    "image_bump_map", "image_color_quantize_dither",
    "image_cross_hatch", "image_soft_light",
    "image_double_exposure", "image_bokeh_blur",
    "image_fog_effect", "image_infrared",
    "image_neon_glow", "image_mirror_quad",
    "image_color_dodge", "image_sunbeams",
    "image_pencil_color", "image_selective_blur",
    "image_light_leak", "image_pixelate_grid",
    "image_frost", "image_color_halftone",
    "image_relief", "image_rainbow_gradient",
    "image_tilt_shift", "image_diffuse_glow",
    "image_stipple", "image_color_burn_blend",
    "image_noise_stipple", "image_gradient_map",
    "image_cross_process", "image_lomo",
    "image_pixel_sort", "image_mosaic_portrait",
    "image_watermark_logo", "image_orton_effect",
    "image_scanline_art", "image_color_overlay",
    "image_warp_swirl", "image_sketch_color",
    "image_neon_outline", "image_texture_overlay",
    "image_color_shift_channels", "image_glamour_glow",
    "image_kaleidoscope", "image_vintage_photo",
    "image_paint_strokes", "image_morning_haze",
    "image_color_relief", "image_glitter",
    "image_watercolor_light", "image_solarize_color",
    "image_pixel_wave", "image_crystallize",
    "image_comic_dots", "image_thermal",
    "image_pastel_wash", "image_prism_burst",
    "image_blueprint_edges", "image_paper_cutout",
    "image_charcoal_smudge", "image_cyanotype",
    "image_noir_silhouette", "image_risograph_dual",
    "image_metallic_sheen", "image_velvet_shadow",
    "image_porcelain_glow", "image_ember_bloom",
]

from gemia.picture.enhance import image_opaline_glow, image_graphite_tint

__all__.extend([
    "image_opaline_glow",
    "image_graphite_tint",
])

from gemia.picture.enhance import image_copper_patina, image_lucid_dream

__all__.extend([
    "image_copper_patina",
    "image_lucid_dream",
])

from gemia.picture.enhance import image_liquid_chrome, image_celestial_haze

__all__.extend([
    "image_liquid_chrome",
    "image_celestial_haze",
])

from gemia.picture.enhance import image_ink_bleed, image_arctic_glow

__all__.extend([
    "image_ink_bleed",
    "image_arctic_glow",
])

from gemia.picture.enhance import image_saffron_bloom, image_glacier_mist

__all__.extend([
    "image_saffron_bloom",
    "image_glacier_mist",
])

from gemia.picture.enhance import image_rose_quartz, image_shadow_teal

__all__.extend([
    "image_rose_quartz",
    "image_shadow_teal",
])
