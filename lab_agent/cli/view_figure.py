"""Prepare a fetched figure for vision reading: a size-safe copy, or a zoomed crop.

Two problems make raw figures hard to read: the vision API rejects images over ~2000 px
on a side in multi-image requests, and fine plot detail (e.g. individual points in a slide-
embedded scatter) is illegible at full-page resolution. This tool solves both without losing
information — the full-resolution originals written by ``fetch_page_images`` are left intact
(they are the best source for a precise crop), and this writes a *derived* image next to them:

  * a downsized ``<name>_view.<ext>`` copy of an oversized figure (readable in one glance), or
  * a ``<name>_crop.<ext>`` sub-region cropped from the full-res original and upscaled, so small
    details become legible for a precise quantitative read.

Usage:
  # size-safe viewable copy (only rewrites if the original exceeds --max):
  python -m lab_agent.cli.view_figure <path> [--max 1500]

  # zoom into a fractional bounding box (coords 0..1 of width/height), upscaled:
  python -m lab_agent.cli.view_figure <path> --crop X0 Y0 X1 Y1 [--scale 2] [--max 1500]

Prints the path of the prepared image (and its size) to Read.
"""
from __future__ import annotations

import sys
from pathlib import Path


def prepare(path: str, crop: tuple | None = None, scale: float = 2.0,
            max_dim: int = 1500) -> tuple[Path, tuple[int, int]]:
    """Return (output_path, (w, h)). ``crop`` is a fractional (x0, y0, x1, y1) box."""
    from PIL import Image

    # Zooming a figure is the strongest possible signal that it is under active
    # discussion, so mark it before anything else — retention must not reclaim a
    # figure someone is reading right now.
    from ..retention import touch_used
    touch_used(path)

    im = Image.open(path)
    w, h = im.size
    suffix = "_view"
    if crop:
        x0, y0, x1, y1 = crop
        box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
        im = im.crop(box)
        if scale and scale != 1.0:
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                           Image.LANCZOS)
        suffix = "_crop"
    if max(im.size) > max_dim:
        im.thumbnail((max_dim, max_dim), Image.LANCZOS)

    p = Path(path)
    out = p.with_name(f"{p.stem}{suffix}{p.suffix}")
    im.save(out)
    return out, im.size


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)

    path = args[0]
    crop = None
    scale = 2.0
    max_dim = 1500
    if "--crop" in args:
        i = args.index("--crop")
        try:
            crop = tuple(float(args[i + k]) for k in range(1, 5))
        except (IndexError, ValueError):
            print("--crop needs 4 numbers 0..1: X0 Y0 X1 Y1")
            sys.exit(2)
    if "--scale" in args:
        scale = float(args[args.index("--scale") + 1])
    if "--max" in args:
        max_dim = int(args[args.index("--max") + 1])

    out, size = prepare(path, crop=crop, scale=scale, max_dim=max_dim)
    print(f"{out}  ({size[0]}x{size[1]})")
    print("Read this path with the Read tool.")


if __name__ == "__main__":
    main()
