#!/usr/bin/env python3
"""
Генерация коротких MP4 из PNG для sendLivePhoto в канале.

Парное имя: vacancy-promoter-1.png → vacancy-promoter-1.mp4

Требуется ffmpeg в PATH. Примеры:
  python scripts/generate_live_photo_mp4.py
  python scripts/generate_live_photo_mp4.py --force
  python scripts/generate_live_photo_mp4.py vacancy-promoter-1.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.channel_live_video import (  # noqa: E402
    DEFAULT_CHANNEL_IMAGES_DIR,
    collect_png_targets,
    find_ffmpeg,
    generate_mp4_from_png,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MP4 для живых фото канала (sendLivePhoto) из PNG обложек.",
    )
    parser.add_argument(
        "png_names",
        nargs="*",
        help="Конкретные файлы (имя или путь). Без аргументов — все *.png в каталоге.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_CHANNEL_IMAGES_DIR,
        help=f"Каталог PNG (по умолчанию assets/channel_images)",
    )
    parser.add_argument("--duration", type=float, default=2.5, help="Длина, сек (2.5)")
    parser.add_argument("--fps", type=int, default=25, help="FPS (25)")
    parser.add_argument("--force", action="store_true", help="Пересоздать существующие MP4")
    parser.add_argument("--dry-run", action="store_true", help="Показать ffmpeg без запуска")
    args = parser.parse_args()

    if not find_ffmpeg() and not args.dry_run:
        print("ffmpeg not found in PATH. Установите ffmpeg и повторите.", file=sys.stderr)
        return 1

    images_dir = args.dir.resolve()
    if not images_dir.is_dir():
        print(f"Directory missing: {images_dir}", file=sys.stderr)
        return 1

    targets = collect_png_targets(images_dir, args.png_names or None)
    if not targets:
        print(f"No PNG files in {images_dir}", file=sys.stderr)
        return 1

    ok_count = 0
    skip_count = 0
    fail_count = 0

    for png_path in targets:
        ok, msg = generate_mp4_from_png(
            png_path,
            duration_sec=args.duration,
            fps=args.fps,
            force=args.force,
            dry_run=args.dry_run,
        )
        print(msg)
        if ok:
            ok_count += 1
        elif msg.startswith("skip"):
            skip_count += 1
        else:
            fail_count += 1

    print(
        f"Done: {ok_count} generated, {skip_count} skipped, {fail_count} failed "
        f"({len(targets)} png, dir={images_dir})",
    )
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
