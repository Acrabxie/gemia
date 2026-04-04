"""gemia.audio — Audio primitive operations.

All functions work with float32 ndarrays [-1, 1].
"""
from gemia.audio.basics import load, save, trim, concat, mix
from gemia.audio.dynamics import normalize, compress, adjust_gain
from gemia.audio.frequency import eq, highpass, lowpass
from gemia.audio.time_pitch import time_stretch, pitch_shift, detect_bpm

__all__ = [
    "load", "save", "trim", "concat", "mix",
    "normalize", "compress", "adjust_gain",
    "eq", "highpass", "lowpass",
    "time_stretch", "pitch_shift", "detect_bpm",
]
