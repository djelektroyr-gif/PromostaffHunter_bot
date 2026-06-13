"""Генерация коротких MP4 из PNG для sendLivePhoto в канале."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANNEL_IMAGES_DIR = _PROJECT_ROOT / "assets" / "channel_images"


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def ken_burns_vf(
    *,
    width: int = 1080,
    height: int = 1080,
    fps: int = 25,
    duration_sec: float = 2.5,
    zoom_end: float = 1.06,
) -> str:
    """Фильтр ffmpeg: кроп к квадрату + плавный зум к центру."""
    frames = max(int(duration_sec * fps), 1)
    zoom_step = (zoom_end - 1.0) / frames
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"zoompan=z='min(1+{zoom_step:.6f}*on,{zoom_end})':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={width}x{height}:fps={fps},format=yuv420p"
    )


def build_ffmpeg_args(
    png_path: Path,
    mp4_path: Path,
    *,
    duration_sec: float = 2.5,
    fps: int = 25,
    width: int = 1080,
    height: int = 1080,
    zoom_end: float = 1.06,
) -> list[str]:
    vf = ken_burns_vf(
        width=width,
        height=height,
        fps=fps,
        duration_sec=duration_sec,
        zoom_end=zoom_end,
    )
    return [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(png_path),
        "-vf",
        vf,
        "-t",
        str(duration_sec),
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(mp4_path),
    ]


def should_skip_mp4(png_path: Path, mp4_path: Path, force: bool) -> bool:
    if force:
        return False
    if not mp4_path.is_file():
        return False
    return mp4_path.stat().st_mtime >= png_path.stat().st_mtime


def collect_png_targets(images_dir: Path, names: list[str] | None) -> list[Path]:
    if names:
        out: list[Path] = []
        for name in names:
            path = Path(name)
            if not path.suffix:
                path = path.with_suffix(".png")
            if path.is_file():
                out.append(path.resolve())
            else:
                candidate = images_dir / path.name
                if candidate.is_file():
                    out.append(candidate.resolve())
        return out
    return sorted(images_dir.glob("*.png"))


def generate_mp4_from_png(
    png_path: Path,
    *,
    duration_sec: float = 2.5,
    fps: int = 25,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    mp4_path = png_path.with_suffix(".mp4")
    if should_skip_mp4(png_path, mp4_path, force):
        return False, f"skip (up-to-date): {mp4_path.name}"

    cmd = build_ffmpeg_args(png_path, mp4_path, duration_sec=duration_sec, fps=fps)
    if dry_run:
        return True, "dry-run: " + " ".join(cmd)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, f"ffmpeg error: {exc}"

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        hint = tail[-1] if tail else f"code {proc.returncode}"
        return False, f"fail {png_path.name}: {hint}"

    return True, f"ok: {mp4_path.name}"
