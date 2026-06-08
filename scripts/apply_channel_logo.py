#!/usr/bin/env python3
"""Наложить promostaff-hunter-logo.png только на PNG без надписи PROMOSTAFF."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.channel_image_logo import (  # noqa: E402
    LOGO_FILENAME,
    LOGO_OVERLAY_FILENAMES,
    apply_logo_to_directory,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Склейка логотипа только для promo-maintenance, promo-update и т.п.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Наложить на все PNG (не рекомендуется — дублирует PROMOSTAFF на форме)",
    )
    args = parser.parse_args()

    images_dir = ROOT / "assets" / "channel_images"
    logo_path = images_dir / LOGO_FILENAME
    if not logo_path.is_file():
        print(f"Logo missing: {logo_path}", file=sys.stderr)
        return 1

    if not args.force:
        print("Whitelist:", ", ".join(sorted(LOGO_OVERLAY_FILENAMES)))

    ok, skipped = apply_logo_to_directory(images_dir, logo_path=logo_path, force=args.force)
    print(f"Done: {ok} updated, {skipped} skipped ({images_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
