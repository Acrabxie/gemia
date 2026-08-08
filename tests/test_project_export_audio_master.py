from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gemia import project_export
from gemia.project_export import ProjectExportError

_LOUDNORM_JSON = """
noise before
{
  "input_i": "-18.25",
  "input_tp": "-2.40",
  "input_lra": "6.10",
  "input_thresh": "-28.75",
  "output_i": "-15.20",
  "output_tp": "-3.00",
  "target_offset": "-0.80"
}
noise after
"""


def test_loudnorm_measurement_parser_requires_finite_complete_stats() -> None:
    parsed = project_export._parse_loudnorm_payload(_LOUDNORM_JSON)
    assert parsed == {
        "input_i": -18.25,
        "input_tp": -2.4,
        "input_lra": 6.1,
        "input_thresh": -28.75,
        "target_offset": -0.8,
    }

    with pytest.raises(ValueError, match="non-finite"):
        project_export._parse_loudnorm_payload(
            _LOUDNORM_JSON.replace('"-18.25"', '"-inf"')
        )
    with pytest.raises(ValueError, match="unusable|invalid|did not emit"):
        project_export._parse_loudnorm_payload("{}")


def test_premaster_measurement_timeout_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    premaster = tmp_path / "premaster.wav"
    premaster.write_bytes(b"not-read-before-timeout")

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=3)

    monkeypatch.setattr(project_export.subprocess, "run", raise_timeout)
    with pytest.raises(ProjectExportError) as exc:
        project_export._measure_loudnorm_pass(
            premaster,
            integrated_lufs=-16.0,
            true_peak_dbtp=-3.0,
            loudness_range_lu=11.0,
            timeout_sec=3,
        )

    assert exc.value.code == "audio_master_measurement_failed"


def test_second_pass_filter_uses_measured_values_and_encode_headroom() -> None:
    value = project_export._loudnorm_second_pass_filter(
        {
            "input_i": -18.25,
            "input_tp": -2.4,
            "input_lra": 6.1,
            "input_thresh": -28.75,
            "target_offset": -0.8,
        }
    )
    assert "I=-16" in value
    assert "TP=-3" in value
    assert "measured_I=-18.250000" in value
    assert "measured_TP=-2.400000" in value
    assert "offset=-0.800000" in value
    assert "linear=true" in value
    assert "sample_rates=48000" in value
    assert "channel_layouts=stereo" in value


def test_audio_source_beyond_master_duration_is_rejected_before_ffmpeg(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProjectExportError) as exc:
        project_export._mux_audio_onto_video(
            tmp_path / "video.mp4",
            [
                {
                    "path": tmp_path / "audio.wav",
                    "track_id": "A1",
                    "start": 0.0,
                    "source_in": 0.0,
                    "source_out": 4.0,
                    "gain_db": 0.0,
                    "fade_in": 0.0,
                    "fade_out": 0.0,
                }
            ],
            output=tmp_path / "output.mp4",
            work_dir=tmp_path,
            timeout_sec=10,
            master_duration=3.0,
        )

    assert exc.value.code == "audio_source_exceeds_master_duration"
