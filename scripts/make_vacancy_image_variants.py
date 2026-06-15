#!/usr/bin/env python3
"""Варианты -2/-3 из vacancy-*-1.png: лёгкий зум/сдвиг для ротации в канале."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "assets" / "channel_images"


def _center_crop_zoom(img: Image.Image, scale: float) -> Image.Image:
    w, h = img.size
    nw, nh = int(w * scale), int(h * scale)
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return resized.crop((left, top, left + w, top + h))


def make_variants(base: Path, *, force: bool = False) -> list[Path]:
    if not base.is_file():
        raise FileNotFoundError(base)
    stem = base.stem
    if not stem.endswith("-1"):
        raise ValueError(f"Expected *-1.png, got {base.name}")

    out_paths: list[Path] = []
    img = Image.open(base).convert("RGB")

    variant_ops = [
        ("-2", lambda im: _center_crop_zoom(im, 1.04)),
        ("-3", lambda im: ImageEnhance.Color(_center_crop_zoom(im, 1.02)).enhance(1.08)),
    ]
    for suffix, fn in variant_ops:
        out = base.with_name(stem.replace("-1", suffix) + ".png")
        if out.is_file() and not force:
            out_paths.append(out)
            continue
        fn(img).save(out, format="PNG", optimize=True)
        out_paths.append(out)
    return out_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать vacancy-*-2/3.png из *-1.png")
    parser.add_argument("names", nargs="*", help="Имена файлов *-1.png (иначе все новые категории)")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    images_dir = args.dir.resolve()
    if args.names:
        bases = [images_dir / n for n in args.names]
    else:
        bases = sorted(images_dir.glob("vacancy-*-1.png"))
        bases = [
            p for p in bases
            if p.stem.rsplit("-", 1)[0].replace("vacancy-", "")
            in {"booth", "merchandiser", "host_mc", "dj", "electrician"}
        ]

    if not bases:
        print("No base images found", file=sys.stderr)
        return 1

    for base in bases:
        try:
            outs = make_variants(base, force=args.force)
            print(f"{base.name} -> {', '.join(p.name for p in outs)}")
        except Exception as exc:
            print(f"fail {base.name}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
