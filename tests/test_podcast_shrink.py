import os
import stat

import pytest

pytest.importorskip("notebooklm")

import podcast_gen as pg


def _fake_encoder(bin_dir, name, out_bytes):
    """A stand-in encoder that writes `out_bytes` to its last argument."""
    exe = bin_dir / name
    exe.write_text(f'#!/bin/sh\nprintf \'%s\' "{out_bytes}" > "${{@: -1}}"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))          # no real encoders visible
    monkeypatch.setattr(pg._cr, "cfg", lambda k: "")
    audio = tmp_path / "260917 MP Podcast.m4a"
    audio.write_bytes(b"X" * 1000)
    return bin_dir, audio


def test_no_encoder_leaves_file_alone(sandbox):
    bin_dir, audio = sandbox
    pg.shrink_for_speech(audio)
    assert audio.read_bytes() == b"X" * 1000


def test_ffmpeg_replaces_with_smaller_output(sandbox, capsys):
    bin_dir, audio = sandbox
    _fake_encoder(bin_dir, "ffmpeg", "small")
    pg.shrink_for_speech(audio)
    assert audio.read_bytes() == b"small"
    assert not audio.with_suffix(".tmp.m4a").exists()
    assert "Re-encoded for speech" in capsys.readouterr().out


def test_afconvert_fallback_when_no_ffmpeg(sandbox):
    bin_dir, audio = sandbox
    _fake_encoder(bin_dir, "afconvert", "tiny")
    pg.shrink_for_speech(audio)
    assert audio.read_bytes() == b"tiny"


def test_larger_output_is_discarded(sandbox):
    bin_dir, audio = sandbox
    _fake_encoder(bin_dir, "ffmpeg", "Y" * 5000)
    pg.shrink_for_speech(audio)
    assert audio.read_bytes() == b"X" * 1000
    assert not audio.with_suffix(".tmp.m4a").exists()


def test_bitrate_zero_disables(sandbox, monkeypatch):
    bin_dir, audio = sandbox
    _fake_encoder(bin_dir, "ffmpeg", "small")
    monkeypatch.setattr(pg._cr, "cfg", lambda k: "0" if k == "PODCAST_BITRATE" else "")
    pg.shrink_for_speech(audio)
    assert audio.read_bytes() == b"X" * 1000
