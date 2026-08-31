"""Unit tests for barge-in speaker scoreboard helpers (no TitaNet)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tools.eval_barge_in_speaker import (
    Clip,
    annotate_clip_length,
    classify_channel,
    classify_length,
    expand_short_prefixes,
    mix_near_far,
    pcm_prefix,
    scoreboard_key,
)


def _tone(duration_s: float, *, hz: float = 220.0, amplitude: float = 0.2) -> bytes:
    samples = int(16000 * duration_s)
    t = np.linspace(0, duration_s, samples, endpoint=False, dtype=np.float32)
    wave = (np.sin(2 * np.pi * hz * t) * amplitude).astype(np.float32)
    return (wave * 32767.0).astype(np.int16).tobytes()


def test_classify_channel_from_path_and_tags(tmp_path: Path) -> None:
    laptop = tmp_path / "owner" / "stop.wav"
    satellite = tmp_path / "owner" / "satellite" / "jarvis.wav"
    assert classify_channel(laptop) == "laptop"
    assert classify_channel(satellite) == "satellite"
    assert classify_channel(laptop, ("far-field",)) == "satellite"


def test_classify_length_short_folder_and_duration(tmp_path: Path) -> None:
    short_path = tmp_path / "owner" / "short" / "yes.wav"
    phrase_path = tmp_path / "owner" / "stop.wav"
    assert classify_length(_tone(1.2), short_path) == "short"
    assert classify_length(_tone(0.4), phrase_path) == "short"
    assert classify_length(_tone(1.2), phrase_path) == "phrase"
    assert classify_length(_tone(1.2), phrase_path, ("short",)) == "short"


def test_scoreboard_key_and_prefix() -> None:
    assert scoreboard_key("owner", "short", "satellite") == "owner/short/satellite"
    pcm = _tone(2.0)
    prefix = pcm_prefix(pcm, seconds=0.5)
    assert len(prefix) == int(0.5 * 16000) * 2


def test_expand_short_prefixes_skips_already_short(tmp_path: Path) -> None:
    phrase = Clip(
        id="stop",
        path=tmp_path / "owner" / "stop.wav",
        speaker="owner",
        role="near_end",
        channel="laptop",
        length="phrase",
    )
    short = Clip(
        id="yes",
        path=tmp_path / "owner" / "short" / "yes.wav",
        speaker="owner",
        role="near_end",
        channel="laptop",
        length="short",
    )
    pcm_by_id = {"stop": _tone(1.5), "yes": _tone(0.3)}
    clips = [annotate_clip_length(phrase, pcm_by_id["stop"]), annotate_clip_length(short, pcm_by_id["yes"])]
    expanded = expand_short_prefixes(clips, pcm_by_id)
    ids = {clip.id for clip in expanded}
    assert "stop__short_prefix" in ids
    assert "yes__short_prefix" not in ids
    assert pcm_by_id["stop__short_prefix"] == pcm_prefix(pcm_by_id["stop"], seconds=0.5)


def test_mix_near_far_clean_is_identity() -> None:
    near = _tone(0.8, hz=220)
    far = _tone(0.8, hz=440)
    assert mix_near_far(near, far, ser_db=float("inf")) == near
    mixed = mix_near_far(near, far, ser_db=5.0)
    assert mixed != near
    assert len(mixed) == len(near)
    with pytest.raises(ValueError):
        mix_near_far(b"", far, ser_db=0.0)
