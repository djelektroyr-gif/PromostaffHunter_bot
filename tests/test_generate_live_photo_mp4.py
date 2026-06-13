"""Тесты генерации MP4 для живых фото канала (без запуска ffmpeg)."""

from pathlib import Path

from services.channel_live_video import (
    build_ffmpeg_args,
    collect_png_targets,
    ken_burns_vf,
    should_skip_mp4,
)


def test_ken_burns_vf_contains_zoompan():
    vf = ken_burns_vf(duration_sec=2.5, fps=25)
    assert "zoompan" in vf
    assert "1080x1080" in vf


def test_build_ffmpeg_args_paths(tmp_path):
    png = tmp_path / "vacancy-promoter-1.png"
    mp4 = tmp_path / "vacancy-promoter-1.mp4"
    png.write_bytes(b"x")
    cmd = build_ffmpeg_args(png, mp4, duration_sec=2.0)
    assert cmd[0] == "ffmpeg"
    assert str(png) in cmd
    assert str(mp4) in cmd
    assert "-an" in cmd


def test_should_skip_when_mp4_newer(tmp_path):
    png = tmp_path / "a.png"
    mp4 = tmp_path / "a.mp4"
    png.write_bytes(b"png")
    mp4.write_bytes(b"mp4")
    assert should_skip_mp4(png, mp4, force=False) is True


def test_should_not_skip_when_force(tmp_path):
    png = tmp_path / "a.png"
    mp4 = tmp_path / "a.mp4"
    png.write_bytes(b"png")
    mp4.write_bytes(b"mp4")
    assert should_skip_mp4(png, mp4, force=True) is False


def test_collect_png_targets_by_name(tmp_path):
    (tmp_path / "vacancy-loader-1.png").write_bytes(b"x")
    found = collect_png_targets(tmp_path, ["vacancy-loader-1.png"])
    assert len(found) == 1
    assert found[0].name == "vacancy-loader-1.png"
