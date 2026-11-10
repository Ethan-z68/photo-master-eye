#!/usr/bin/env python3
"""photo_lab.py - deterministic, fully local photo editing for the photo-master-eye skill.

Subcommands: info | grid | candidates | develop | compare | presets |
             formats | raw-preview | raw-export | map-crop | crop-plan | xmp-crop
No network access, no API keys, no writes to the source file.
Requires Python 3.9+ and Pillow 10+, macOS `sips` for RAW decoding.

v1.2.1: EXIF orientation is normalized at export time so preview metadata,
grid, candidates and crop-plan all share ONE canonical coordinate system.
v1.3.0: user's standing default is NO CROP + source untouched. This is a
workflow policy in SKILL.md, not a script behavior; the script still supports
explicit crops whenever the user asks for one in that session.
v1.4.0: RAW probes read the TIFF header instead of decoding; `finish` goes
RAW -> deliverable in one pass with no full-size intermediate; candidates build
the contact sheet in memory.
v1.4.1: deliverable placement. `finish` writes next to the original
(`<stem>_final.<ext>`, never an `<stem>_out/` subfolder); every process artefact
(preview / grid / candidates / full decode / compare / crop.json) defaults to
`$TMPDIR/pme_work_<stem>/`. Only `<stem>.xmp` stays beside the RAW, because a
Camera Raw sidecar is only found there.
v1.5.0: `--rotate` (degrees, positive = clockwise) for crop candidates and
`finish`, plus `--zoom` on `finish`. Order is crop -> rotate -> trim to the
largest inscribed rectangle, so the ratio never changes and `--crop` coordinates
stay in decode space. Rotating AFTER the crop is deliberate: it keeps preview
coordinates valid without a second remapping.
v1.5.1: `rotate_fit()` quantises the inscribed rectangle to the source ratio as
an exact fraction (8064x6048 -> 4:3 -> multiples of 4x3). Without this the two
dimensions were floored independently and the output ratio drifted ~0.03%
(5607x4204 instead of 5600x4200).
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

try:
    from PIL import __version__ as PILLOW_VERSION
except ImportError:  # pragma: no cover
    PILLOW_VERSION = "unknown"

# ---------------------------------------------------------------------------
# Presets: a starting point, not "master taste". Explicit CLI flags override.
# ---------------------------------------------------------------------------
PRESETS = {
    "none": {},
    "airy-japan": {"exposure": 0.12, "temp": -0.08, "shadows": 0.12,
                   "highlights": 0.10, "contrast": -0.18, "saturation": -0.12,
                   "sharpen": 0.6},
    "film-warm": {"exposure": 0.06, "temp": 0.18, "contrast": -0.10,
                  "saturation": -0.08, "grain": 0.35, "vignette": 0.30,
                  "sharpen": 0.5},
    "cinema-teal-orange": {"temp": 0.10, "highlights": 0.08, "shadows": 0.15,
                           "contrast": 0.22, "saturation": 0.08,
                           "sharpen": 0.9, "vignette": 0.18},
    "landscape-crisp": {"highlights": 0.28, "shadows": 0.22, "contrast": 0.20,
                        "saturation": 0.06, "sharpen": 1.2, "temp": 0.04},
    "mono-contrast": {"mono": True, "contrast": 0.30, "shadows": 0.10,
                      "highlights": 0.12, "sharpen": 1.0},
    "portrait-soft": {"exposure": 0.10, "contrast": -0.12, "saturation": -0.06,
                      "sharpen": 0.35, "temp": 0.05},
}

DEFAULTS = {"exposure": 0.0, "temp": 0.0, "shadows": 0.0, "highlights": 0.0,
           "contrast": 0.0, "saturation": 0.0, "sharpen": 0.0,
           "grain": 0.0, "vignette": 0.0, "mono": False}

RATIO_MAP = {"1:1": 1.0, "4:5": 0.8, "5:4": 1.25, "3:2": 1.5, "2:3": 2 / 3,
             "4:3": 4 / 3, "16:9": 16 / 9, "9:16": 9 / 16, "3:4": 0.75}
ANCHORS = ("center", "third-left", "third-right", "top", "bottom", "golden")


def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(msg: str) -> None:
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def parse_ratio(text: str) -> float:
    text = text.strip()
    if text in RATIO_MAP:
        return RATIO_MAP[text]
    if ":" in text:
        a, b = text.split(":", 1)
        return float(a) / float(b)
    return float(text)


def ratio_tag(text: str) -> str:
    return text.replace(":", "x").replace(".", "p")


def open_photo(path: Path):
    """Return (RGB-capable image, cleaned EXIF bytes, source format)."""
    if not path.is_file():
        die(f"file not found: {path}")
    raw = Image.open(path)
    fmt = raw.format or "?"
    try:
        img = ImageOps.exif_transpose(raw)
    finally:
        raw.close()
    exif = img.getexif()
    exif.pop(274, None)  # orientation already applied
    try:
        exif_bytes = exif.tobytes() if len(exif) else b""
    except Exception:
        exif_bytes = b""
    return img, exif_bytes, fmt


def to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return img.convert("RGB")


def save_photo(img: Image.Image, out: Path, quality: int, exif_bytes: bytes) -> str:
    """Save to a NEW file. Returns a note about the JPEG encoding path used."""
    out.parent.mkdir(parents=True, exist_ok=True)
    ext = out.suffix.lower()
    if ext in (".png", ".tif", ".tiff"):
        img.save(out, exif=exif_bytes)
        return "lossless"
    if ext == ".webp":
        img.save(out, "WEBP", quality=quality, exif=exif_bytes)
        return "webp"
    # Pillow/libjpeg raises "Suspension not allowed here / broken data stream"
    # on large images when optimize=True is combined with subsampling=0 (4:4:4).
    # Skip that path for big images (also avoids noise on stderr); keep the
    # try/except as a safety net for smaller images that still trip the bug.
    if img.width * img.height <= 4_000_000:
        try:
            img.save(out, "JPEG", quality=quality, subsampling=0,
                    optimize=True, exif=exif_bytes)
            return "jpeg optimize=True, subsampling=0"
        except OSError:
            pass
    img.save(out, "JPEG", quality=quality, subsampling=0,
            optimize=False, exif=exif_bytes)
    return "jpeg optimize=False (large image), subsampling=0"


def lut(fn) -> list[int]:
    return [max(0, min(255, round(fn(i / 255.0) * 255))) for i in range(256)]


def font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------
def cmd_info(args) -> None:
    src = Path(args.image).expanduser().resolve()
    if not src.is_file():
        die(f"file not found: {src}")
    if src.suffix.lower() in RAW_EXTS:
        (w, h), orient, stored, probe = canonical_raw_size(src)
        rotated = orient in (5, 6, 7, 8)
        emit({"source": str(src), "width": w, "height": h,
              "width_height_basis": ("display-oriented (EXIF applied)"
                                     if rotated else "stored sensor"),
              "orientation": orient, "stored_size": stored, "probe": probe,
              "current_ratio": round(w / h, 4), "megapixels": round(w * h / 1e6, 2),
              "format": "RAW (read via sips)",
              "note": "Pillow cannot decode RAW. Use `raw-preview` for a small "
                      "viewing copy and `raw-export` for a full-size working copy. "
                      + ("This RAW carries a 90-degree EXIF orientation: sips "
                         "reports STORED dims but viewers show them swapped. "
                         "raw-preview bakes the rotation into the pixels."
                         if rotated else "")})
        return
    raw = Image.open(src)
    exif = dict(raw.getexif())
    from PIL import ExifTags
    named = {ExifTags.TAGS.get(k, str(k)): str(v)[:80]
             for k, v in exif.items()
             if ExifTags.TAGS.get(k) in ("Make", "Model", "LensModel", "DateTimeOriginal",
                                         "ExposureTime", "FNumber", "ISOSpeedRatings",
                                         "FocalLength", "Orientation")}
    w, h = raw.size
    emit({"source": str(src), "width": w, "height": h, "mode": raw.mode,
          "format": raw.format, "orientation": exif.get(274, 1),
          "current_ratio": round(w / h, 4), "megapixels": round(w * h / 1e6, 2),
          "exif": named, "pillow": PILLOW_VERSION})
    raw.close()


# ---------------------------------------------------------------------------
# grid overlay
# ---------------------------------------------------------------------------
def render_grid(base: Image.Image) -> Image.Image:
    """Return a copy of `base` with thirds / golden / centre guides drawn."""
    g = base.convert("RGBA")
    w, h = g.size
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    lw = max(2, round(min(w, h) / 480))

    for f in (1 / 3, 2 / 3):
        d.line([(w * f, 0), (w * f, h)], fill=(255, 255, 255, 165), width=lw)
        d.line([(0, h * f), (w, h * f)], fill=(255, 255, 255, 165), width=lw)
    for f in (0.382, 0.618):
        d.line([(w * f, 0), (w * f, h)], fill=(255, 170, 60, 130), width=lw)
        d.line([(0, h * f), (w, h * f)], fill=(255, 170, 60, 130), width=lw)
    d.line([(w / 2, 0), (w / 2, h)], fill=(80, 220, 255, 90), width=lw)
    d.line([(0, h / 2), (w, h / 2)], fill=(80, 220, 255, 90), width=lw)

    fnt = font(max(13, min(w, h) // 42))
    d.text((12, 10), "WHITE = thirds   ORANGE = golden   CYAN = center",
           fill=(0, 0, 0, 210), font=fnt, stroke_width=2,
           stroke_fill=(255, 255, 255, 200))
    return Image.alpha_composite(g, ov).convert("RGB")


def cmd_grid(args) -> None:
    src = Path(args.image).expanduser().resolve()
    img, _, _ = open_photo(src)
    out = Path(args.out).expanduser().resolve() if args.out else \
        work_dir_for(src) / f"{src.stem}_grid.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    render_grid(to_rgb(img)).save(out, "JPEG", quality=92, subsampling=0)
    emit({"output": str(out), "note": "Open this with ViewImage before judging the crop."})


# ---------------------------------------------------------------------------
# crop maths + candidates
# ---------------------------------------------------------------------------
def crop_box(w: int, h: int, ratio: float, anchor: str, zoom: float = 1.0):
    """Largest inscribed box with the target ratio, positioned by anchor.

    zoom < 1 shrinks the box (ratio preserved) so BOTH axes have slack. This
    matters for tall/narrow images: with the largest inscribed box dx == 0,
    so third-left / center / third-right silently produce IDENTICAL crops.
    """
    zoom = max(0.1, min(1.0, zoom))
    if w / h > ratio:
        nw, nh = round(h * ratio), h
    else:
        nw, nh = w, round(w / ratio)
    nw, nh = round(nw * zoom), round(nh * zoom)
    nw, nh = max(1, min(nw, w)), max(1, min(nh, h))
    dx, dy = w - nw, h - nh
    fx, fy = 0.5, 0.5
    # Anchor names describe WHERE THE SUBJECT LANDS, not where the frame sits.
    # Shifting the frame right puts a centered subject near the left third.
    if anchor == "third-left":        # subject near left third
        fx = 0.75
    elif anchor == "third-right":     # subject near right third
        fx = 0.25
    elif anchor == "top":             # keep upper part (sky), drop foreground
        fy = 0.15
    elif anchor == "bottom":          # keep lower part, drop empty sky
        fy = 0.85
    elif anchor == "golden":          # centered subject near the 0.618 line
        fx = fy = 0.382
    x = max(0, min(dx, round(dx * fx)))
    y = max(0, min(dy, round(dy * fy)))
    return (x, y, x + nw, y + nh)


def rotate_fit(img: Image.Image, angle_deg: float):
    """Rotate, then crop back to the ORIGINAL aspect ratio.

    A rotated frame has four empty wedges. Cropping to the largest inscribed
    rectangle of the original ratio keeps the frame proportional (the user's
    rule is "don't change the ratio", not "don't touch the frame") and throws
    away the blank corners. Positive `angle_deg` turns the picture clockwise,
    i.e. it corrects a camera that was rolled anticlockwise.

    Returns (image, box_in_rotated_canvas, crop_factor) where crop_factor is
    cropped_width / original_width - use it to warn when the rotation eats a
    lot of pixels.
    """
    if not angle_deg:
        return img, (0, 0, img.width, img.height), 1.0
    w, h = img.size
    ratio = w / h
    # Quantise to the source's exact ratio as a fraction (8064x6048 -> 4:3), so
    # the result is a multiple of 4x3 and the ratio is bit-exact instead of
    # drifting by ~0.03% from independent flooring of both dimensions.
    frac = Fraction(w, h)
    th = math.radians(abs(angle_deg))
    c, s = math.cos(th), math.sin(th)
    # Bounding-box condition: an axis-aligned (nw, nh) rect fits inside the
    # rotated original iff nw*cos + nh*sin <= w and nw*sin + nh*cos <= h.
    nw = min(w / (c + s / ratio), h / (s + c / ratio))
    k = max(1, math.floor(nw / frac.numerator) - 1)
    nw, nh = k * frac.numerator, k * frac.denominator
    if nw < 16 or nh < 16:
        die(f"rotation {angle_deg} deg leaves {nw}x{nh}px - too small")
    rot = img.rotate(-angle_deg, resample=Image.BICUBIC, expand=True)
    cw, ch = rot.size
    left, top = (cw - nw) // 2, (ch - nh) // 2
    return rot.crop((left, top, left + nw, top + nh)), (left, top, left + nw, top + nh), nw / w


def parse_explicit_crops(text: str):
    """Parse 'label:x,y,w,h;label2:x,y,w,h' in PREVIEW pixel coordinates.

    The label is optional: 'x,y,w,h' alone is auto-named A, B, C ... in order.
    Coordinates never contain ':', so the separator is unambiguous, and forcing
    a label the model does not care about is one more chance to die on a typo.
    """
    out = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            label, coords = part.split(":", 1)
            label = label.strip() or chr(65 + len(out))
        else:
            label, coords = chr(65 + len(out)), part
        vals = [int(float(v)) for v in coords.split(",")]
        if len(vals) != 4 or vals[2] <= 0 or vals[3] <= 0:
            die(f"invalid crop in --crops: {part!r} (need x,y,w,h with w,h>0)")
        x, y, cw, ch = vals
        out.append((label, (x, y, x + cw, y + ch)))
    if not out:
        die("--crops parsed to zero boxes")
    return out


def cmd_candidates(args) -> None:
    """Crop candidates. Accepts a RAW directly (decoded once, then cached).

    v1.6.0: two round trips disappear. (1) `candidates IMG.DNG` used to die with
    UnidentifiedImageError, costing a wasted turn; it now probes and decodes
    internally. (2) every record carries `crop_source`, an exact-ratio box in
    SOURCE pixels that can be pasted straight into `finish --crop`, so the
    preview -> source `map-crop` step is no longer part of the loop.
    """
    src = Path(args.image).expanduser().resolve()
    max_side = getattr(args, "max", 0) or (1600 if is_raw(src) else 0)
    meta = load_analysis(src, max_side=max_side, quality=args.quality)
    base = meta["img"]
    ratio_frac = nominal_ratio(int(meta["source_size"][0]),
                               int(meta["source_size"][1]))
    rotate_info = None
    if getattr(args, "rotate", 0.0):
        base, _, rfactor = rotate_fit(base, args.rotate)
        rotate_info = {"angle_deg": args.rotate,
                       "kept_fraction": round(rfactor, 4),
                       "ratio_preserved": True}
    w, h = base.size
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else \
        work_dir_for(src) / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)

    items = []
    idx = 0
    duplicates = []
    annotate = bool(getattr(args, "annotate", False))
    # Bigger cells when annotating, so ONE look can judge 3-6 frames. Five
    # separate ViewImage calls cost 5 agent round trips (~100 s wall clock)
    # and ~5k permanent vision tokens; one legible sheet costs one.
    cell_w, cell_h, bar = (440, 380, 64) if annotate else (360, 300, 30)

    def add_candidate(tag: str, box, extra: dict) -> None:
        """Crop + thumbnail in memory; write a JPEG only on --keep-crops.

        Policy (v1.4.0) keeps only the original and the final deliverable, so
        writing N candidate files just to delete them later is wasted I/O.
        """
        crop_img = base.crop(box)
        rec = {"id": tag[:2], "label": tag, "crop": list(box),
               "size": [box[2] - box[0], box[3] - box[1]],
               "thumb": ImageOps.contain(crop_img, (cell_w - 12, cell_h - 12)),
               **extra}
        # Source-pixel box, snapped to the exact source ratio. `finish --crop`
        # consumes this verbatim; no map-crop step needed.
        if rotate_info is None:
            rec["crop_source"] = snap_box(
                work_to_source(meta, (box[0], box[1],
                                      box[2] - box[0], box[3] - box[1])),
                ratio_frac)
            rec["finish"] = ('--crop "%d,%d,%d,%d"'
                             % tuple(rec["crop_source"]))
        if args.keep_crops:
            fp = out_dir / f"cand_{tag}.jpg"
            crop_img.save(fp, "JPEG", quality=args.quality, subsampling=0)
            rec["file"] = str(fp)
        items.append(rec)

    explicit = getattr(args, "crops_src", None) or args.crops
    if explicit:
        # Shore-style: decide where attention should travel FIRST, then write
        # the boundaries by hand. No anchor guessing, no subject detection.
        mode = "explicit-source" if getattr(args, "crops_src", None) else "explicit"
        boxes = parse_explicit_crops(explicit)
        if getattr(args, "crops_src", None):
            # survey emits source px; candidates works in the decoded space.
            sx, sy = meta["scale_x"], meta["scale_y"]
            boxes = [(lb, (b[0] / sx, b[1] / sy, b[2] / sx, b[3] / sy))
                     for lb, b in boxes]
        for label, raw_box in boxes:
            idx += 1
            box = (max(0, raw_box[0]), max(0, raw_box[1]),
                   min(w, raw_box[2]), min(h, raw_box[3]))
            if box[2] <= box[0] or box[3] <= box[1]:
                die(f"crop '{label}' falls outside the {w}x{h} preview: {raw_box}")
            add_candidate(f"{idx:02d}_{label}", box,
                          {"ratio": round((box[2] - box[0]) / (box[3] - box[1]), 4),
                           "anchor": "explicit",
                           "clipped": list(box) != list(raw_box)})
    else:
        mode = "geometric"
        ratios = [r.strip() for r in args.ratios.split(",") if r.strip()]
        anchors = [a.strip() for a in args.anchors.split(",") if a.strip()]
        for a in anchors:
            if a not in ANCHORS:
                die(f"unknown anchor '{a}', choose from {', '.join(ANCHORS)}")
        for rtext in ratios:
            r = parse_ratio(rtext)
            seen = set()
            for anchor in anchors:
                idx += 1
                box = crop_box(w, h, r, anchor, args.zoom)
                # A horizontal anchor is meaningless when dx == 0. Report it
                # instead of silently emitting three identical JPEGs.
                effective = box not in seen
                seen.add(box)
                tag = f"{idx:02d}_{ratio_tag(rtext)}_{anchor}"
                extra = {"ratio": rtext, "anchor": anchor,
                         "anchor_effective": effective}
                if not effective:
                    extra["note"] = ("anchor had no slack on this axis -> crop is "
                                     "identical to an earlier one. Use --zoom < 1 "
                                     "or --crops with explicit boxes.")
                    duplicates.append(tag)
                add_candidate(tag, box, extra)

    # contact sheet: built from in-memory thumbnails, no re-reads from disk
    cols = min(3, len(items)) or 1
    rows = math.ceil(len(items) / cols)
    sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + bar)), (28, 29, 32))
    d = ImageDraw.Draw(sheet)
    fnt = font(16)
    for i, it in enumerate(items):
        cx, cy = (i % cols) * cell_w, (i // cols) * (cell_h + bar)
        thumb = it.pop("thumb")
        px = cx + (cell_w - thumb.width) // 2
        py = cy + (cell_h - thumb.height) // 2
        sheet.paste(thumb, (px, py))
        d.rectangle([px, py, px + thumb.width - 1, py + thumb.height - 1],
                    outline=(90, 92, 100), width=1)
        # Caption is anchored to the THUMB's left edge, not the cell's: a 3:4
        # thumb is much narrower than a 440px cell, so cell-anchored text drifts
        # ~70px away from the picture it describes.
        d.text((px, cy + cell_h + 5),
               f"{it['id']} | {it['label'][3:]}",
               fill=(240, 240, 240), font=fnt)
        if annotate:
            cs = it.get("crop_source")
            if cs:
                sub = (f"SRC {cs[0]},{cs[1]}  {cs[2]}x{cs[3]}  "
                       f"r={cs[2] / cs[3]:.4f}")
            else:
                sub = (f"WORK {it['crop'][0]},{it['crop'][1]}  "
                       f"{it['size'][0]}x{it['size'][1]}")
            d.text((px, cy + cell_h + 28), sub,
                   fill=(150, 205, 150), font=font(13))
            # Where this box sits inside the whole frame. Parked at the right
            # end of the caption bar, i.e. below the picture, so it can never
            # land on top of the crop or float outside its own cell.
            bx, by, bw2, bh2 = cx + cell_w - 50, cy + cell_h + 6, 38, 48
            d.rectangle([bx, by, bx + bw2, by + bh2],
                        outline=(110, 112, 120), width=1)
            fx0 = bx + bw2 * it["crop"][0] / w
            fy0 = by + bh2 * it["crop"][1] / h
            fx1 = bx + bw2 * it["crop"][2] / w
            fy1 = by + bh2 * it["crop"][3] / h
            d.rectangle([fx0, fy0, fx1, fy1], outline=(0, 220, 120), width=1)
    sheet_path = out_dir / "contact_sheet.jpg"
    sheet.save(sheet_path, "JPEG", quality=90)
    emit({"artifacts": {"contact_sheet": str(sheet_path),
                        "candidate_files": [it["file"] for it in items
                                            if it.get("file")]},
          "source": str(src),
          "source_size": meta["source_size"],
          "analysis_size": [w, h],
          "scale": round(meta["scale_x"], 4),
          "decoded_cache": meta.get("decoded_cache"),
          "mode": mode, "zoom": args.zoom, "rotation": rotate_info,
          "count": len(items),
          "duplicate_crops": duplicates,
          "candidates": items,
          "next": "copy a candidate's `finish` string verbatim into "
                  "`finish <image> --crop ...`; crop_source is already the "
                  "exact source-pixel box at the locked ratio.",
          "note": "Candidates stay in memory unless --keep-crops. contact_sheet "
                  "is a process artefact - delete it when the task ends."})


# ---------------------------------------------------------------------------
# develop: crop + grade
# ---------------------------------------------------------------------------
def grade(img: Image.Image, p: dict) -> Image.Image:
    img = to_rgb(img)

    if p["exposure"]:
        gamma = 2 ** (-p["exposure"])
        img = img.point(lut(lambda x: x ** gamma) * 3)

    if p["temp"]:
        rg, bg = 1 + 0.22 * p["temp"], 1 - 0.22 * p["temp"]
        lr = lut(lambda x: x * rg)
        lg = lut(lambda x: x)
        lb = lut(lambda x: x * bg)
        img = img.point(lr + lg + lb)

    if p["shadows"] or p["highlights"]:
        s, hi = p["shadows"], p["highlights"]
        img = img.point(lut(
            lambda x: x + s * 0.35 * (1 - x) ** 2 - hi * 0.30 * x ** 2) * 3)

    if p["contrast"]:
        c, wgt = p["contrast"], min(1.0, abs(p["contrast"]))
        if c > 0:
            k = 4 + 4 * c
            lo = 1 / (1 + math.exp(k * 0.5))
            hi = 1 / (1 + math.exp(-k * 0.5))

            def scurve(x):
                y = 1 / (1 + math.exp(-k * (x - 0.5)))
                return (y - lo) / (hi - lo)
        else:
            def scurve(x):
                return 0.5 + (x - 0.5) * (1 + 0.8 * c)
        img = img.point(lut(lambda x: x * (1 - wgt) + scurve(x) * wgt) * 3)

    if p["saturation"]:
        img = ImageEnhance.Color(img).enhance(max(0.0, 1 + p["saturation"]))

    if p["mono"]:
        img = ImageOps.grayscale(img).convert("RGB")

    if p["sharpen"] > 0:
        img = img.filter(ImageFilter.UnsharpMask(
            radius=1.2, percent=int(110 * p["sharpen"]), threshold=3))

    if p["grain"] > 0:
        noise = Image.effect_noise(img.size, 18).convert("RGB")
        delta = noise.point(lut(lambda x: 0.5 + (x - 0.5) * 0.25 * p["grain"]) * 3)
        img = ImageChops.add(img, delta, scale=1.0, offset=-128)

    if p["vignette"] > 0:
        v = p["vignette"]
        grad = Image.radial_gradient("L").resize(img.size)
        mask = grad.point(lut(lambda x: min(1.0, x ** 1.8 * v)))
        dark = ImageEnhance.Brightness(img).enhance(1 - 0.55 * v)
        img = Image.composite(dark, img, mask)

    return img


def parse_crop(text: str):
    parts = [int(float(v)) for v in text.split(",")]
    if len(parts) != 4:
        die("--crop must be 'x,y,w,h'")
    x, y, cw, ch = parts
    if cw <= 0 or ch <= 0:
        die("crop width/height must be positive")
    return (x, y, x + cw, y + ch)


def cmd_develop(args) -> None:
    src = Path(args.image).expanduser().resolve()
    img, exif_bytes, _ = open_photo(src)
    w, h = img.size

    if args.crop:
        box = parse_crop(args.crop)
    elif args.ratio:
        box = crop_box(w, h, parse_ratio(args.ratio), args.anchor)
    else:
        box = (0, 0, w, h)
    box = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
    if box[2] <= box[0] or box[3] <= box[1]:
        die(f"invalid crop box {box} for image {w}x{h}")
    img = img.crop(box)

    # v1.2.0: color grading removed from the default workflow. Pixel edits are
    # crop-only unless the caller explicitly opts into the legacy path with --grade.
    grade_keys = ("exposure", "temp", "shadows", "highlights", "contrast",
                  "saturation", "sharpen", "grain", "vignette")
    requested = (args.preset != "none" or bool(args.mono) or
                 any(getattr(args, k, None) is not None for k in grade_keys))
    warning = None
    p = dict(DEFAULTS)  # all zero
    if args.grade:
        p.update(PRESETS.get(args.preset, {}))
        for key in DEFAULTS:
            val = getattr(args, key, None)
            if val is not None:
                p[key] = val
        img = grade(img, p)
        grading = "legacy --grade explicitly enabled"
    else:
        grading = "disabled (default since v1.2.0, crop-only)"
        if requested:
            warning = ("color parameters were IGNORED: the grading module is removed "
                       "by default. Pass --grade explicitly to use the legacy path.")

    out = Path(args.out).expanduser().resolve() if args.out else \
        work_dir_for(src) / f"{src.stem}_edited.png"
    if out == src:
        die("refusing to overwrite the source image; choose a different --out")
    encoding = save_photo(img, out, args.quality, exif_bytes)

    sidecar = out.parent / f"{out.stem}.json"
    record = {"source": str(src), "output": str(out), "source_size": [w, h],
              "crop_box": list(box), "output_size": list(img.size),
              "color_grading": grading,
              "preset": args.preset if args.grade else "none", "params": p,
              "quality": args.quality, "encoding": encoding,
              "warning": warning, "pillow": PILLOW_VERSION}
    sidecar.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    emit({"output": str(out), "sidecar": str(sidecar), "crop_box": list(box),
          "color_grading": grading, "params": p, "encoding": encoding,
          "warning": warning,
          "note": "Source untouched. Prefer crop-plan + xmp-crop for "
                  "a non-destructive delivery."})


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------
def cmd_compare(args) -> None:
    a, _, _ = open_photo(Path(args.before).expanduser().resolve())
    b, _, _ = open_photo(Path(args.after).expanduser().resolve())
    a, b = to_rgb(a), to_rgb(b)
    cell_w, cell_h, bar = 640, 560, 34
    sheet = Image.new("RGB", (cell_w * 2, cell_h + bar), (28, 29, 32))
    d = ImageDraw.Draw(sheet)
    fnt = font(17)
    for i, (im, label) in enumerate(((a, "BEFORE"), (b, "AFTER"))):
        thumb = ImageOps.contain(im, (cell_w - 12, cell_h - 12))
        sheet.paste(thumb, (i * cell_w + (cell_w - thumb.width) // 2,
                           (cell_h - thumb.height) // 2))
        d.text((i * cell_w + 12, cell_h + 6), label, fill=(240, 240, 240), font=fnt)
    if args.out:
        out = Path(args.out).expanduser().resolve()
    else:
        out = work_dir_for(Path(args.after).expanduser().resolve()) / "compare.jpg"
    sheet.save(out, "JPEG", quality=91)
    emit({"output": str(out), "note": "Check: subject intact, horizon level, no blown highlights."})


def cmd_presets(args) -> None:
    emit({"presets": PRESETS, "defaults": DEFAULTS,
          "note": "Explicit CLI flags override preset values. Order is fixed."})


def cmd_formats(args) -> None:
    """Ask-the-user checklist helper: what can actually be delivered."""
    emit({
        "default_delivery": "non-destructive: crop-plan (JSON) + xmp-crop (XMP sidecar)",
        "pixel_export_formats": {
            "png": {"bit_depth": 8, "lossy": False, "default": True,
                    "use": "无损压缩，默认；只要实体成片就用它"},
            "tiff": {"bit_depth": 8, "lossy": False, "use": "归档 / 印刷前中间稿"},
            "jpg": {"bit_depth": 8, "lossy": True, "default_quality": 92,
                    "use": "分享 / 社媒，体积最小"},
            "webp": {"bit_depth": 8, "lossy": True, "use": "网页"},
        },
        "how": "由 develop/raw-export --out 的文件后缀决定格式",
        "dng_as_output": {
            "supported": False,
            "reason": "DNG 是 TIFF 6.0 扩展的 RAW 归档容器（未显影传感器数据 + XMP "
                      "编辑元数据），不是成品图格式。Adobe 明确 RAW 不是图像本身，"
                      "编辑后须导出成另一种 raster 格式。",
            "correct_route": "原 DNG 不动 + XMP sidecar 记录裁剪框（见 xmp-crop）",
            "alternatives": [
                "参数可复现 → crop.json + 原 DNG",
                "要 16-bit 宽容度 → 先用 darktable / RawTherapee 显影",
                "要非破坏性编辑 → Lightroom / darktable",
            ],
        },
        "color_grading": "removed in v1.2.0; develop defaults to zero grading. "
                         "Legacy grade() requires explicit --grade.",
        "note": "sips 解码链路上限 8-bit；PNG/TIFF 的「无损」指压缩无损，不代表 16-bit。",
    })


# ---------------------------------------------------------------------------
# RAW / large-file bridge. Uses macOS `sips` (ImageIO) to decode RAW without
# installing LibRaw/rawpy. Files stay on disk; nothing is uploaded anywhere.
# ---------------------------------------------------------------------------
RAW_EXTS = {".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf",
            ".sr2", ".raf", ".rw2", ".orf", ".pef", ".raw", ".rwl", ".3fr",
            ".fff", ".iiq", ".mos", ".mrw", ".erf", ".dcr", ".srw", ".axr"}


def sips_size(path: Path):
    r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"sips cannot read {path.name}: {r.stderr.strip()}")
    w = re.search(r"pixelWidth:\s*(\d+)", r.stdout)
    h = re.search(r"pixelHeight:\s*(\d+)", r.stdout)
    if not (w and h):
        if path.suffix.lower() in RAW_EXTS:
            die("sips cannot decode this RAW (no dimensions returned). This camera "
                "model is probably not in Apple's supported RAW list. Fallbacks: "
                "export a JPG in-camera, install darktable-cli, or `pip install rawpy`. "
                f"Raw sips output: {r.stdout.strip()}")
        die(f"no dimensions returned by sips for {path.name}: {r.stdout.strip()}")
    return int(w.group(1)), int(h.group(1))


def sips_export(src: Path, out: Path, max_side=None, quality=90, fmt="jpeg") -> Path:
    cmd = ["sips", "-s", "format", fmt]
    if fmt == "jpeg":
        cmd += ["-s", "formatOptions", str(quality)]
    if max_side:
        cmd += ["-Z", str(max_side)]
    cmd += [str(src), "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die("sips failed to decode this file. Most likely this camera model is not in "
            "Apple's supported RAW list. Fallbacks: export a JPG in-camera, install "
            "darktable-cli, or `pip install rawpy`. "
            f"Detail: {r.stderr.strip()}")
    return out


def _base_stem(p: Path) -> str:
    """Strip the artefacts suffix so every step shares ONE work dir.

    `IMG_0862_final.jpg` and `IMG_0862.DNG` must map to the same
    `pme_work_IMG_0862`, otherwise `compare` (which keys off the deliverable)
    leaves a stray `pme_work_IMG_0862_final/` behind after cleanup.
    """
    s = p.stem
    for suf in ("_final", "_edited", "_grid", "_preview", "_full"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def work_dir_for(src: Path) -> Path:
    """Return the temp work dir for a source image, creating it if needed.

    Process artefacts (preview / grid / candidates / full-size decode) must
    never land next to the user's photo: the standing rule is "the original
    plus the final deliverable, nothing else". They live under the system temp
    dir, and each task ends by deleting this one directory.
    """
    d = Path(tempfile.gettempdir()) / f"pme_work_{_base_stem(src)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_orientation(path: Path, quality: int = 92) -> dict:
    """Bake EXIF orientation into the pixels, then remove the tag.

    Problem this fixes: `sips` copies a RAW's orientation tag into the exported
    file but leaves the stored pixels UNROTATED. open_photo() rotates them via
    ImageOps.exif_transpose(), so grid/candidates were 1500x2000 while the
    raw-preview sidecar claimed 2000x1500 -> every mapped crop coordinate was
    on the wrong axis. Normalizing once at export makes all consumers agree.

    Returns stored/canonical sizes, the original orientation and whether the
    file was rewritten. Never touches the source RAW.
    """
    with Image.open(path) as raw:
        stored = list(raw.size)
        orient = int(raw.getexif().get(274, 1) or 1)
        if orient == 1:
            return {"stored_size": stored, "canonical_size": stored,
                    "orientation": 1, "changed": False}
        fixed = to_rgb(ImageOps.exif_transpose(raw)).copy()
    # save_photo() handles the large-JPEG libjpeg bug; pass empty EXIF so no
    # orientation tag can leak back in.
    save_photo(fixed, path, quality, b"")
    return {"stored_size": stored, "canonical_size": [fixed.width, fixed.height],
            "orientation": orient, "changed": True}


def raw_orientation(path: Path) -> int:
    """Read the orientation that `sips` will copy into an export.

    `sips -g orientation` returns <nil> for DNG even when the file carries one,
    so decode a 64px JPEG into a temp dir and read EXIF from that instead.
    Cheap, and avoids pulling the full-size RAW into memory.
    """
    import tempfile
    with tempfile.TemporaryDirectory(prefix="pme_orient_") as td:
        tiny = Path(td) / "o.jpg"
        try:
            sips_export(path, tiny, max_side=64, quality=50)
        except SystemExit:
            return 1
        try:
            with Image.open(tiny) as im:
                return int(im.getexif().get(274, 1) or 1)
        except Exception:
            return 1


def probe_raw(path: Path):
    """Cheap RAW geometry probe: read the TIFF header, do not decode pixels.

    Most DNG files are TIFF containers, so Pillow can return size + orientation
    in ~0.09s where sips needs ~1.1s (it decodes). Some DNGs (certain iPhone
    files) raise UnidentifiedImageError, so fall back to sips there.

    Returns ((stored_w, stored_h), orientation, source).
    """
    try:
        with Image.open(path) as im:
            w, h = im.size
            orient = int(im.getexif().get(274, 1) or 1)
        # Guard against a header that only exposes the embedded thumbnail.
        if w >= 600 and h >= 600:
            return (w, h), orient, "tiff-header"
    except Exception:
        pass
    return sips_size(path), raw_orientation(path), "sips"


def canonical_raw_size(path: Path):
    """Return ((display_w, display_h), orientation, [stored_w, stored_h], source).

    sips/Pillow report STORED sensor dimensions; crop-plan / XMP must use the
    size the human actually sees (90-degree orientations swap the axes).
    """
    (w, h), orient, source = probe_raw(path)
    if orient in (5, 6, 7, 8):
        return (h, w), orient, [w, h], source
    return (w, h), orient, [w, h], source


def cmd_raw_preview(args) -> None:
    src = Path(args.image).expanduser().resolve()
    if not src.is_file():
        die(f"file not found: {src}")
    (rw, rh), orient, stored, probe = canonical_raw_size(src)
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else \
        work_dir_for(src)
    out_dir.mkdir(parents=True, exist_ok=True)
    prev = out_dir / f"{src.stem}_preview.jpg"
    sips_export(src, prev, max_side=args.max, quality=args.quality)
    norm = normalize_orientation(prev, args.quality)
    pw, ph = norm["canonical_size"]
    sx, sy = rw / pw, rh / ph
    warn = None
    if abs(sx - sy) / max(sx, sy) > 0.01:
        warn = "non-uniform scale; use the per-axis scale values"
    meta = {"source": str(src),
            "raw_size": [rw, rh],               # canonical / display-oriented
            "raw_stored_size": stored,          # sensor pixels as reported
            "orientation": orient, "probe": probe,
            "orientation_normalized": norm["changed"],
            "preview": str(prev), "preview_size": [pw, ph],
            "scale_x": round(sx, 6), "scale_y": round(sy, 6),
            "mapping": "crop_on_preview * scale = crop_on_display_oriented_raw",
            "warning": warn,
            "note": "8-bit JPEG preview for composition decisions only. EXIF "
                    "orientation has been baked into the pixels and the tag "
                    "removed, so preview/grid/candidates share one coordinate "
                    "system. If orientation != 1, crop-plan/XMP coordinates are "
                    "in DISPLAY orientation; verify in your RAW editor."}
    grid_path = None
    if args.grid:
        # Same process, same decode: build the grid from the preview we just
        # made instead of paying for a second CLI call.
        grid_path = out_dir / f"{src.stem}_grid.jpg"
        pimg, _, _ = open_photo(prev)
        render_grid(to_rgb(pimg)).save(grid_path, "JPEG", quality=92,
                                       subsampling=0)
    meta_path = out_dir / f"{src.stem}_preview.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    emit({**meta, "meta_file": str(meta_path),
          "grid": str(grid_path) if grid_path else None,
          "preview_kb": round(prev.stat().st_size / 1024, 1),
          "next": "ViewImage the preview (and grid if asked), then `map-crop` to "
                  "convert crop coords, or `finish` to render the deliverable."})


def cmd_raw_export(args) -> None:
    src = Path(args.image).expanduser().resolve()
    if not src.is_file():
        die(f"file not found: {src}")
    (rw, rh), orient, stored, probe = canonical_raw_size(src)
    ext = {"jpeg": "jpg", "png": "png", "tiff": "tiff"}[args.format]
    out = Path(args.out).expanduser().resolve() if args.out else \
        work_dir_for(src) / f"{src.stem}_full.{ext}"
    if out == src:
        die("refusing to overwrite the source RAW")
    sips_export(src, out, max_side=args.max, quality=args.quality, fmt=args.format)
    if args.keep_orientation:
        norm = {"stored_size": list(sips_size(out)),
                "canonical_size": list(sips_size(out)),
                "orientation": orient, "changed": False}
    else:
        # Re-encoding a full-size PNG/TIFF is expensive but keeps the whole
        # pipeline on one coordinate system. Use --keep-orientation to skip.
        norm = normalize_orientation(out, args.quality)
    ew, eh = norm["canonical_size"]
    emit({"source": str(src), "raw_size": [rw, rh], "raw_stored_size": stored,
          "orientation": orient, "orientation_normalized": norm["changed"],
          "output": str(out), "output_size": [ew, eh],
          "scale_vs_raw": round(rw / ew, 6),
          "mb": round(out.stat().st_size / 1024 / 1024, 2),
          "note": "Coordinates are in DISPLAY orientation (EXIF baked in). If "
                  "output_size != raw_size, multiply mapped crop coords by "
                  "(output_size / raw_size). sips exports 8-bit; for a 16-bit "
                  "workflow use darktable/RawTherapee first."})


def cmd_finish(args) -> None:
    """RAW -> final deliverable in one pass, with no full-size intermediate.

    The old path was raw-export then develop, which wrote a 100MB+ PNG/JPEG
    intermediate and read it back. Here sips decodes once into a private temp
    dir, Pillow crops and encodes straight to the deliverable, and the temp dir
    is removed in `finally` - nothing is left behind even on failure.

    Output placement follows the standing rule: the deliverable sits NEXT TO
    the original (`<stem>_final.<ext>`), never in a `<stem>_out/` subfolder.
    Process artefacts go to `work_dir_for()`, i.e. the system temp dir.
    """
    src = Path(args.image).expanduser().resolve()
    if not src.is_file():
        die(f"file not found: {src}")
    (ow, oh), orient, stored, probe = canonical_raw_size(src)
    ext = {"jpeg": "jpg", "png": "png", "tiff": "tiff"}[args.format]
    out = Path(args.out).expanduser().resolve() if args.out else \
        src.parent / f"{src.stem}_final.{ext}"
    if out == src:
        die("refusing to overwrite the source RAW")

    t0 = time.perf_counter()
    tmpdir = tempfile.mkdtemp(prefix="pme_finish_")
    try:
        tmp_ext = {"jpeg": "jpg", "png": "png", "tiff": "tiff"}[args.decode_format]
        tmp = Path(tmpdir) / f"decode.{tmp_ext}"
        # JPEG decode keeps the temp small (5MB vs 100MB+ for PNG). Use
        # --decode-format png if you need a lossless intermediate.
        sips_export(src, tmp, quality=args.decode_quality,
                    fmt=args.decode_format)
        normalize_orientation(tmp, args.decode_quality)
        img, _, _ = open_photo(tmp)
        w0, h0 = img.size
        w, h = img.size
        if args.crop:
            box = parse_crop(args.crop)
        elif args.ratio or getattr(args, "zoom", 1.0) < 1.0:
            ratio = parse_ratio(args.ratio) if args.ratio else w / h
            box = crop_box(w, h, ratio, args.anchor, getattr(args, "zoom", 1.0))
        else:
            box = (0, 0, w, h)
        box = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
        if box[2] <= box[0] or box[3] <= box[1]:
            die(f"invalid crop box {box} for decoded {w}x{h}")
        if box != (0, 0, w, h):
            img = img.crop(box)
        # Rotation comes AFTER the crop on purpose: --crop coordinates stay in
        # decode/preview space (no remapping), and rotate_fit() then trims back
        # to the largest inscribed rectangle, so the crop ratio survives intact.
        rotation = None
        if getattr(args, "rotate", 0.0):
            img, rbox, rfactor = rotate_fit(img, args.rotate)
            rotation = {"angle_deg": args.rotate, "clockwise": True,
                        "kept_fraction": round(rfactor, 4),
                        "ratio_preserved": True,
                        "order": "crop, then rotate, then trim to inscribed rect",
                        "note": "blank corners removed; the cropped ratio is kept"}
        encoding = save_photo(img, out, args.quality, b"")
        decode_mb = tmp.stat().st_size / 1024 / 1024
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    elapsed = time.perf_counter() - t0
    record = {"source": str(src), "output": str(out),
              "output_size": list(img.size), "crop_box": list(box),
              "decoded_size": [w0, h0], "orientation": orient,
              "raw_stored_size": stored, "probe": probe,
              "rotation": rotation,
              "color_grading": "none (crop-only since v1.2.0)",
              "encoding": encoding, "quality": args.quality,
              "decode_temp_mb": round(decode_mb, 1),
              "temp_cleaned": not os.path.exists(tmpdir),
              "seconds": round(elapsed, 2), "pillow": PILLOW_VERSION}
    # Default is NO sidecar: the user's standing policy keeps only the original
    # and the final image. Pass --sidecar if you need reproducible parameters.
    sidecar = None
    if args.sidecar:
        sidecar = out.parent / f"{out.stem}.json"
        sidecar.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    emit({**record, "sidecar": str(sidecar) if sidecar else None,
          "note": "Final deliverable only. The full-size decode lived in a temp "
                  "dir and was deleted; the source RAW was never written."})


def cmd_map_crop(args) -> None:
    pv = Path(args.preview).expanduser().resolve()
    meta_path = Path(args.meta).expanduser().resolve() if args.meta else pv.with_suffix(".json")
    if not meta_path.is_file():
        die(f"metadata not found: {meta_path}. Run raw-preview first, or pass --meta.")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    sx, sy = meta.get("scale_x"), meta.get("scale_y")
    if not sx or not sy:
        die("metadata lacks scale_x/scale_y")
    box = parse_crop(args.crop)  # x, y, x+w, y+h in preview coordinates
    raw = [round(box[0] * sx), round(box[1] * sy),
           round(box[2] * sx), round(box[3] * sy)]
    ow, oh = meta["raw_size"]
    raw = [max(0, min(ow, raw[0])), max(0, min(oh, raw[1])),
           max(0, min(ow, raw[2])), max(0, min(oh, raw[3]))]
    emit({"preview_crop_xywh": [box[0], box[1], box[2] - box[0], box[3] - box[1]],
          "original_crop_xywh": [raw[0], raw[1], raw[2] - raw[0], raw[3] - raw[1]],
          "original_crop_box": raw, "scale": [sx, sy],
          "develop_arg": f'--crop "{raw[0]},{raw[1]},{raw[2]-raw[0]},{raw[3]-raw[1]}"',
          "note": "Assumes the full-size export matches RAW dimensions; otherwise "
                  "rescale by output_size/raw_size."})


# ---------------------------------------------------------------------------
# Non-destructive delivery: crop plan + standard Camera Raw XMP sidecar.
# Neither command writes a single byte to the source image.
# ---------------------------------------------------------------------------
def _resolve_crop(src: Path, args):
    # Coordinates MUST be in the orientation the viewer sees, not the sensor's
    # stored orientation. Otherwise a 90-degree RAW puts every crop on the
    # wrong axis (this bit us on 我与黄浦江.DNG, EXIF Orientation=6).
    (ow, oh), orient, stored, probe = canonical_raw_size(src)
    if getattr(args, "crop", None):
        box = parse_crop(args.crop)
    elif getattr(args, "ratio", None):
        box = crop_box(ow, oh, parse_ratio(args.ratio), args.anchor)
    else:
        box = (0, 0, ow, oh)
    box = (max(0, box[0]), max(0, box[1]), min(ow, box[2]), min(oh, box[3]))
    if box[2] <= box[0] or box[3] <= box[1]:
        die(f"invalid crop box {box} for {ow}x{oh}")
    meta = {"orientation": orient, "raw_stored_size": stored, "probe": probe,
            "coordinate_system": ("display-oriented (EXIF applied)"
                                  if orient in (5, 6, 7, 8) else "stored")}
    return (ow, oh), box, meta


def cmd_crop_plan(args) -> None:
    src = Path(args.image).expanduser().resolve()
    if not src.is_file():
        die(f"file not found: {src}")
    (ow, oh), box, cmeta = _resolve_crop(src, args)
    x0, y0, x1, y1 = box
    plan = {
        "source": str(src), "source_size": [ow, oh],
        **cmeta,
        "crop_box_px": [x0, y0, x1, y1],
        "crop_xywh_px": [x0, y0, x1 - x0, y1 - y0],
        "crop_normalized_0to1": {
            "left": round(x0 / ow, 6), "top": round(y0 / oh, 6),
            "right": round(x1 / ow, 6), "bottom": round(y1 / oh, 6),
        },
        "requested_ratio": args.ratio, "anchor": args.anchor,
        "destructive": False,
        "note": "No image written, no pixel changed, source file untouched. "
                "Feed this to xmp-crop, or apply the pixel box in your editor. "
                "If orientation != 1 these coordinates are in DISPLAY orientation; "
                "verify how your RAW editor maps crs: crop values for rotated files.",
    }
    out = Path(args.out).expanduser().resolve() if args.out else \
        work_dir_for(src) / f"{src.stem}_crop.json"
    if out == src:
        die("refusing to overwrite the source")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    emit({**plan, "output": str(out)})


XMP_TEMPLATE = '''<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="photo-master-eye 1.4.1">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:HasCrop="True"
   crs:CropTop="{top}"
   crs:CropLeft="{left}"
   crs:CropBottom="{bottom}"
   crs:CropRight="{right}"
   crs:CropAngle="0"
   crs:AlreadyApplied="False"
   crs:RawFileName="{rawname}"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
'''


def cmd_xmp_crop(args) -> None:
    src = Path(args.image).expanduser().resolve()
    if not src.is_file():
        die(f"file not found: {src}")
    if args.from_plan:
        pf = Path(args.from_plan).expanduser().resolve()
        if not pf.is_file():
            die(f"plan not found: {pf}")
        plan = json.loads(pf.read_text(encoding="utf-8"))
        n = plan.get("crop_normalized_0to1")
        if not n:
            die("plan lacks crop_normalized_0to1")
        coords = {k: float(n[k]) for k in ("left", "top", "right", "bottom")}
        crop_box_px = plan.get("crop_box_px")
        source_size = plan.get("source_size")
        cmeta = {"orientation": plan.get("orientation", 1),
                 "coordinate_system": plan.get("coordinate_system", "unknown")}
    else:
        if not args.crop:
            die("pass --from-plan, or --crop 'x,y,w,h'")
        (ow, oh), box, cmeta = _resolve_crop(src, args)
        coords = {"left": box[0] / ow, "top": box[1] / oh,
                  "right": box[2] / ow, "bottom": box[3] / oh}
        crop_box_px = list(box)
        source_size = [ow, oh]
    for k, v in coords.items():
        if not 0.0 <= v <= 1.0:
            die(f"normalized {k}={v} out of 0..1")
    # EXCEPTION to the "process artefacts go to temp" rule: a Camera Raw
    # sidecar is only picked up when it sits next to the RAW with the same
    # basename, so this one deliberately lands in the photo directory. It is
    # only produced when the user explicitly asks for a crop.
    out = Path(args.out).expanduser().resolve() if args.out else \
        src.parent / f"{src.stem}.xmp"
    if out == src:
        die("refusing to overwrite the source")
    out.parent.mkdir(parents=True, exist_ok=True)
    body = XMP_TEMPLATE.format(
        top=f"{coords['top']:.6f}", left=f"{coords['left']:.6f}",
        bottom=f"{coords['bottom']:.6f}", right=f"{coords['right']:.6f}",
        rawname=xml_escape(src.name, {'"': "&quot;"}))
    out.write_text(body, encoding="utf-8")
    rotated = cmeta.get("orientation", 1) in (5, 6, 7, 8)
    emit({"output": str(out), "source": str(src), "source_untouched": True,
          "normalized_crop": {k: round(v, 6) for k, v in coords.items()},
          "crop_box_px": crop_box_px, "source_size": source_size, **cmeta,
          "schema": "http://ns.adobe.com/camera-raw-settings/1.0/ (crs:HasCrop, "
                    "crs:CropTop/Left/Bottom/Right, crs:CropAngle, crs:AlreadyApplied)",
          "compatibility_warning": "NOT tested in Lightroom / Adobe Camera Raw "
                                   "(not installed here). Verify in your own editor. "
                                   "darktable uses a different history schema and may "
                                   "ignore this sidecar. If ignored, apply "
                                   "crop_box_px manually."
                                   + (" This RAW has a 90-degree EXIF orientation; "
                                      "crs: crop values are in DISPLAY orientation "
                                      "here, and Adobe's mapping for rotated RAWs "
                                      "has NOT been verified. Check before trusting."
                                      if rotated else "")})


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# v1.6.0 analysis layer: ONE decode shared by everything, plus the three
# commands that used to be hand-written Pillow snippets by the agent.
#
# Why this exists: reframing one 3:4 vertical cost ~48 agent turns, 20 of them
# hand-written measurement code (row profiles, line tracking, RANSAC, ruler
# overlays). Wall clock was 13.8 min against 0.5 s of real pixel work, i.e.
# 96.9% model round-trip. The measurement loop -- not the encoder -- was the
# bottleneck, so the measurements become commands: `survey` answers "where are
# the structure lines, what can I cut, and does the ratio even allow it",
# `level` answers "is the frame rolled", `panels` replaces N separate looks.
# ---------------------------------------------------------------------------
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


def require_numpy() -> None:
    if np is None:
        die("survey/level/panels need numpy: python3 -m pip install --user numpy")


def load_analysis(src: Path, max_side: int = 1600, quality: int = 88,
                  refresh: bool = False) -> dict:
    """Decode ANY supported file (RAW included) once, bounded to `max_side`.

    The decoded copy is cached in the task work dir, so survey -> level ->
    candidates -> panels share ONE decode instead of one per command (a 48MP
    DNG costs ~1.5-2.5 s every time it is re-decoded).

    All geometry returned here is DISPLAY-oriented, the same basis `finish
    --crop` expects. `scale` converts work px -> source px (`crop * scale`).
    """
    if not src.is_file():
        die(f"file not found: {src}")
    wd = work_dir_for(src)
    meta = {"source": str(src),
            "source_name": src.name,
            "work_dir": str(wd)}

    if is_raw(src):
        (sw, sh), orient, stored, probe = canonical_raw_size(src)
        cache = wd / f"_analysis_{int(max_side)}.jpg"
        fresh = (cache.is_file() and not refresh
                 and cache.stat().st_mtime >= src.stat().st_mtime)
        if not fresh:
            sips_export(src, cache, max_side=max_side, quality=quality)
            # sips copies the orientation tag but leaves the pixels unrotated;
            # bake it in so every consumer agrees on one coordinate system.
            norm = normalize_orientation(cache, quality)
            if norm.get("changed"):
                meta["orientation_normalized"] = True
        with Image.open(cache) as im:
            img = to_rgb(im).copy()
        meta.update({"kind": "raw", "source_size": [int(sw), int(sh)],
                     "orientation": orient, "raw_stored_size": stored,
                     "probe": probe, "decoded_cache": str(cache),
                     "cache_reused": bool(fresh)})
    else:
        img, _, fmt = open_photo(src)
        img = to_rgb(img)
        sw, sh = img.size
        if max_side and max(sw, sh) > max_side:
            k = max_side / max(sw, sh)
            img = img.resize((max(1, round(sw * k)), max(1, round(sh * k))),
                             Image.LANCZOS)
        meta.update({"kind": "image", "source_size": [sw, sh],
                     "format": fmt, "decoded_cache": None,
                     "cache_reused": False})

    ww, wh = img.size
    if ww < 32 or wh < 32:
        die(f"analysis image too small: {ww}x{wh}")
    meta.update({"img": img, "work_size": [ww, wh],
                 "scale_x": sw / ww, "scale_y": sh / wh,
                 "scale": sw / ww})
    return meta


def is_raw(p: Path) -> bool:
    return p.suffix.lower() in RAW_EXTS


# --- geometry helpers ------------------------------------------------------
def work_to_source(meta: dict, box) -> list:
    """work-px box (x, y, w, h) -> source-px box, clamped to the source."""
    sx, sy = meta["scale_x"], meta["scale_y"]
    sw, sh = meta["source_size"]
    x = int(round(box[0] * sx))
    y = int(round(box[1] * sy))
    w = int(round(box[2] * sx))
    h = int(round(box[3] * sy))
    x = max(0, min(x, sw - 1))
    y = max(0, min(y, sh - 1))
    w = max(8, min(w, sw - x))
    h = max(8, min(h, sh - y))
    return [x, y, w, h]


NOMINAL_RATIOS = [(1, 1), (4, 3), (3, 4), (3, 2), (2, 3), (16, 9), (9, 16),
                  (5, 4), (4, 5)]


def nominal_ratio(w: int, h: int, tol: float = 0.004) -> Fraction:
    """Aspect as a Fraction, snapped to a standard ratio when within 0.4%.

    Reduced pixel ratios are unusable for cropping: 3783x2838 reduces to
    1261:946, and `snap_box` then only accepts 1x / 2x / 3x of that, so every
    crop collapses onto a third of the frame. Nominal 4:3 keeps quantisation at
    4px / 3px while changing the aspect by 0.02% -- invisible, and far more
    faithful to "lock the original ratio" than the exact-but-coarse fraction.
    """
    r = w / h
    for a, b in NOMINAL_RATIOS:
        if abs(r / (a / b) - 1.0) <= tol:
            return Fraction(a, b)
    return Fraction(int(w), int(h))


def snap_box(box, ratio_frac: Fraction) -> list:
    """Force a source-px box to an EXACT integer aspect ratio.

    The standing rule is "lock the ratio", and a crop whose ratio drifts by
    0.03% still counts as a ratio change. Quantise width to a multiple of the
    reduced numerator, height to the matching multiple of the denominator.
    """
    x, y, w, h = box
    num, den = ratio_frac.numerator, ratio_frac.denominator
    k = max(1, min(w // num, h // den))
    w2, h2 = k * num, k * den
    return [int(x), int(y), int(w2), int(h2)]


def _luma(img: Image.Image):
    return np.asarray(img.convert("L"), dtype=np.float32)


def _profiles(lum):
    """Row/column statistics plus the 2D gradient maps, all in work px."""
    gy2 = np.abs(np.diff(lum, axis=0))
    gx2 = np.abs(np.diff(lum, axis=1))
    gy2 = np.vstack([gy2, gy2[-1:]])
    gx2 = np.hstack([gx2, gx2[:, -1:]])
    return (lum.mean(axis=1), lum.mean(axis=0),
            gy2.mean(axis=1), gx2.mean(axis=0), gy2, gx2)


def _first_run(mask: np.ndarray, k: int, axis: int = 0, last: bool = False):
    """Index of the first (or last) k-consecutive-True run, per line.

    A single noisy pixel is not "content": night sky grain blows past any
    sensible threshold at isolated rows. Requiring a run is what makes the
    detector find the antenna tip (y=305) instead of the first speck at y=0.
    Returns -1 where no run exists.
    """
    m = mask.astype(np.int32)
    if axis == 1:
        m = m.T
    if last:
        m = m[::-1]
    c = np.cumsum(np.vstack([np.zeros((1, m.shape[1]), np.int64), m]), axis=0)
    if m.shape[0] < k:
        base = np.full(m.shape[1], -1)
    else:
        win = c[k:] - c[:-k]
        ok = win >= k
        idx = np.argmax(ok, axis=0)
        base = np.where(ok.any(axis=0), idx, -1)
    if last:
        n = mask.shape[0] if axis == 0 else mask.shape[1]
        base = np.where(base >= 0, n - 1 - base, -1)
    return base


def _content_bounds(energy: np.ndarray, axis: int = 0, run: int = 4,
                    frac: float = 0.15, lo_pct: float = 2.0,
                    hi_pct: float = 98.0) -> tuple:
    """Outermost content position along one axis, robust to grain.

    `energy` is |dL| along the axis of travel. axis=0 scans down (→ top/bottom),
    axis=1 scans across (→ left/right). The percentile (not the min) is the
    point: one thin antenna is a legitimate tallest element, but a single
    stray pixel is not.
    """
    p99 = float(np.percentile(energy, 99)) or 1.0
    thr = max(1.5, frac * p99)
    mask = energy > thr
    firsts = _first_run(mask, run, axis=axis, last=False)
    lasts = _first_run(mask, run, axis=axis, last=True)
    f = firsts[firsts >= 0]
    l = lasts[lasts >= 0]
    n = mask.shape[axis]
    lo = int(round(np.percentile(f, lo_pct))) if len(f) else 0
    hi = int(round(np.percentile(l, hi_pct))) if len(l) else n - 1
    return max(0, lo), min(n - 1, max(hi, lo + 1))


def _peaks(detail: np.ndarray, k: int, mask=None) -> list:
    """Indices of the k strongest local maxima, cheap non-max suppression."""
    d = detail.copy()
    if mask is not None:
        d = np.where(mask, d, -1.0)
    out = []
    for _ in range(k):
        i = int(np.argmax(d))
        if d[i] <= 0:
            break
        out.append((i, float(d[i])))
        d[max(0, i - 3):i + 4] = -1.0
    return out


def _band_edges(row_mean: np.ndarray) -> list:
    """Strong horizontal luminance jumps = material / plane boundaries."""
    if len(row_mean) < 8:
        return []
    d = np.diff(row_mean)
    ad = np.abs(d)
    thr = max(1.2, float(np.percentile(ad, 90)))
    idx = np.where(ad >= thr)[0]
    if len(idx) == 0:
        return []
    groups, cur = [], [idx[0]]
    for i in idx[1:]:
        if i - cur[-1] <= 3:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)
    out = []
    for g in groups:
        j = g[int(np.argmax(ad[g]))]
        out.append({"y": int(j), "jump": round(float(d[j]), 1),
                    "brighter_below": bool(d[j] > 0)})
    out.sort(key=lambda r: -abs(r["jump"]))
    return out[:10]


def _clean_positions(detail: np.ndarray, lo: int, hi: int, k: int = 5) -> list:
    """The k cheapest cut positions inside [lo, hi) (low edge energy)."""
    lo = max(0, lo)
    hi = max(lo + 1, min(hi, len(detail)))
    seg = detail[lo:hi]
    order = np.argsort(seg)
    picked, out = [], []
    for j in order:
        p = lo + int(j)
        if all(abs(p - q) >= max(4, (hi - lo) // 40) for q in picked):
            picked.append(p)
            out.append({"at": int(p), "detail": round(float(seg[j]), 2)})
        if len(out) >= k:
            break
    return out


def _edge_penalty(row_detail, col_detail, p90r, p90c, box) -> float:
    L, T, R, B = box
    hor = (row_detail[T] + row_detail[min(B, len(row_detail) - 1)]) / 2.0 / p90r
    ver = (col_detail[L] + col_detail[min(R, len(col_detail) - 1)]) / 2.0 / p90c
    return float(hor + ver)


def _dead_fraction(detail, a, b, p90) -> float:
    a = max(0, a)
    b = max(a + 1, min(b, len(detail)))
    return float((detail[a:b] < 0.30 * p90).mean())


def _cut_scenarios(w, h, ratio: float, cl, cr, ct, cb,
                   row_detail, col_detail, p90r, p90c,
                   clean_top, clean_bottom, clean_left, clean_right,
                   min_removed: float = 0.03, top_n: int = 6):
    """Enumerate "cut here, pay there" scenarios instead of ranking frames.

    Deliberately NO total score. The skill's own reference doc says a single
    number makes "which framing is better" look computable, when it depends on
    what the viewer should notice. So each row states the trade explicitly:
    which edge moves, what the locked ratio forces on the opposite edge, which
    content gets sacrificed, and how much edge energy the new cut crosses.

    Sampling is on STRUCTURE: candidate edges come from the cheap-cut rows and
    columns plus the content bounds, never from an arbitrary grid.
    """
    out = []
    seen = set()

    def push(label, L, T, W):
        W = min(float(w), max(24.0, W))
        Hh = min(W / ratio, float(h))
        L = max(0.0, min(float(L), w - W))
        T = max(0.0, min(float(T), h - Hh))
        box = (int(round(L)), int(round(T)),
               int(round(L + W)), int(round(T + Hh)))
        if box in seen:
            return
        seen.add(box)
        L2, T2, R2, B2 = box
        bw, bh = R2 - L2, B2 - T2
        if bw < 16 or bh < 16:
            return
        removed = 1.0 - (bw * bh) / float(w * h)
        if removed < min_removed:
            return
        cut = {"left": L2, "top": T2, "right": w - R2, "bottom": h - B2}
        overshoot = (max(0, L2 - cl) + max(0, cr - R2)
                     + max(0, T2 - ct) + max(0, cb - B2))
        out.append({
            "label": label, "box": box,
            "cut_work": cut,
            "area_removed": round(removed, 3),
            "cuts_content_px": int(overshoot),
            "edge_penalty": round(_edge_penalty(row_detail, col_detail,
                                                p90r, p90c, box), 3),
            "keeps": {"content_left": L2 <= cl, "content_right": R2 >= cr,
                      "content_top": T2 <= ct, "content_bottom": B2 >= cb},
            "width": bw, "height": bh,
        })

    Hs = lambda W: W / ratio

    # 1) move the TOP edge, keep the bottom edge -> the ratio forces the width
    for T in sorted({0, ct} | {y for y in clean_top if 0 < y < ct}):
        Wd = (h - T) * ratio
        if Wd > w + 0.5:
            continue
        push(f"cut-top={T}", 0, T, Wd)
        push(f"cut-top={T},keep-right", w - Wd, T, Wd)

    # 2) move the BOTTOM edge, keep the top edge
    for B in sorted({h, cb} | {y for y in clean_bottom if cb < y < h}):
        Hn = B
        Wd = Hn * ratio
        if Wd > w + 0.5:
            continue
        push(f"cut-bottom={h - B}", 0, B - Hn, Wd)
        push(f"cut-bottom={h - B},keep-right", w - Wd, B - Hn, Wd)

    # 3) move the LEFT or RIGHT edge, keep the full height (only helps when
    #    the dead space is lateral, e.g. an empty wall or an off-frame figure)
    for L in sorted({0, cl} | {x for x in clean_left if 0 < x < cl}):
        Wd = w - L
        if Hs(Wd) > h + 0.5:
            continue
        push(f"cut-left={L}", L, 0, Wd)
    for R in sorted({w, cr} | {x for x in clean_right if cr < x < w}):
        Wd = R
        if Hs(Wd) > h + 0.5:
            continue
        push(f"cut-right={w - R}", w - Wd, 0, Wd)

    # 4) the content-tight frame: both dead bands gone, if the width allows
    Hc = float(cb - ct)
    if Hc > 8:
        Wc = Hc * ratio
        if Wc <= w + 0.5:
            push("content-tight,keep-left", 0, ct, Wc)
            push("content-tight,keep-right", w - Wc, ct, Wc)

    # Least damage first, then most decisive. Not a quality score: "damage"
    # here means pixels of content removed, which is a fact, not a taste.
    # Decisive first: a table of timid trims is useless when the frame has no
    # free reframe, and the cost columns are right there for the reader.
    out.sort(key=lambda r: (-r["area_removed"], r["cuts_content_px"]))
    picked, res = [], []
    for rec in out:
        b = rec["box"]
        if any(max(abs(b[i] - p[i]) for i in range(4)) < 16 for p in picked):
            continue
        picked.append(b)
        res.append(rec)
        if len(res) >= top_n:
            break
    return res


# --- survey ---------------------------------------------------------------
def cmd_survey(args) -> None:
    require_numpy()
    src = Path(args.image).expanduser().resolve()
    meta = load_analysis(src, max_side=args.max, quality=args.quality)
    img = meta["img"]
    w, h = meta["work_size"]
    lum = _luma(img)
    row_mean, col_mean, row_detail, col_detail, gy2, gx2 = _profiles(lum)
    p90r = float(np.percentile(row_detail, 90)) or 1.0
    p90c = float(np.percentile(col_detail, 90)) or 1.0

    ct, cb = _content_bounds(gy2, axis=0)
    cl, cr = _content_bounds(gx2, axis=1)

    ratio_frac = nominal_ratio(int(meta["source_size"][0]),
                               int(meta["source_size"][1]))
    ratio = ratio_frac.numerator / ratio_frac.denominator
    if args.ratio:
        rf = args.ratio.strip()
        if ":" in rf:
            a, b = rf.split(":", 1)
            ratio_frac = Fraction(int(a), int(b))
        else:
            ratio_frac = Fraction(float(rf)).limit_denominator(100)
        ratio = ratio_frac.numerator / ratio_frac.denominator

    # cheap cut positions FIRST: the scenario sampler walks structure, not a grid
    ct_rows = [p["at"] for p in _clean_positions(row_detail, 0, max(ct, 8))]
    cb_rows = [p["at"] for p in _clean_positions(row_detail, min(cb, h - 8), h)]
    cl_cols = [p["at"] for p in _clean_positions(col_detail, 0, max(cl, 8))]
    cr_cols = [p["at"] for p in _clean_positions(col_detail, min(cr, w - 8), w)]

    scenarios = _cut_scenarios(w, h, ratio, cl, cr, ct, cb,
                               row_detail, col_detail, p90r, p90c,
                               ct_rows, cb_rows, cl_cols, cr_cols,
                               min_removed=args.min_removed, top_n=args.top)

    scen_out = []
    for i, rec in enumerate(scenarios, 1):
        b = rec["box"]
        src_box = snap_box(work_to_source(
            meta, (b[0], b[1], b[2] - b[0], b[3] - b[1])), ratio_frac)
        sw, sh = meta["source_size"]
        scen_out.append({
            "id": f"S{i}",
            "label": rec["label"],
            "crop": src_box,                       # ready for `finish --crop`
            "crop_work": [b[0], b[1], b[2], b[3]],
            "ratio": f"{ratio_frac.numerator}:{ratio_frac.denominator}",
            "removed_px": {"left": src_box[0], "top": src_box[1],
                           "right": sw - (src_box[0] + src_box[2]),
                           "bottom": sh - (src_box[1] + src_box[3])},
            "area_removed": rec["area_removed"],
            "cuts_content_px": rec["cuts_content_px"],
            "keeps_all_content": rec["cuts_content_px"] == 0,
            "edge_penalty": rec["edge_penalty"],
            "finish": f'--crop "{src_box[0]},{src_box[1]},{src_box[2]},{src_box[3]}"',
        })

    # the trade-off that decides vertical frames
    tight_w = (cb - ct) * ratio
    w_min = max(float(cr - cl), tight_w)
    can_both = (tight_w >= (cr - cl)) and (cb - ct) > 0
    budget = {
        "content_box_work": [cl, ct, cr, cb],
        "content_box_source": work_to_source(meta, (cl, ct, cr - cl, cb - ct)),
        "dead_margins_source": {
            "left": int(round(cl * meta["scale_x"])),
            "top": int(round(ct * meta["scale_y"])),
            "right": int(round((w - cr) * meta["scale_x"])),
            "bottom": int(round((h - cb) * meta["scale_y"]))},
        "min_width_keeping_all_content_source": int(round(w_min * meta["scale_x"])),
        "slack_source": int(round((w - w_min) * meta["scale_x"])),
        "can_cut_both_dead_ends": bool(can_both),
        "width_needed_to_drop_all_dead_source": int(round(tight_w * meta["scale_x"])),
        "width_available_source": meta["source_size"][0],
        "verdict": ("dead space at BOTH ends can be removed and still keep all "
                    "content inside the locked ratio"
                    if can_both else
                    "NOT enough width to drop dead space at both ends: keeping "
                    "the subject span forces the frame taller than the content, "
                    "so you must choose between the top band and the bottom band"),
    }

    clean = {
        "top_band_rows_source": [{"y": int(round(y * meta["scale_y"]))} for y in ct_rows],
        "bottom_band_rows_source": [{"y": int(round(y * meta["scale_y"]))} for y in cb_rows],
        "left_band_cols_source": [{"x": int(round(x * meta["scale_x"]))} for x in cl_cols],
        "right_band_cols_source": [{"x": int(round(x * meta["scale_x"]))} for x in cr_cols],
        "note": "edge_energy is mean |dL| across that line: a low number is a "
                "cheap place to put a frame edge (dark gap, flat surface).",
    }

    artifacts = {}
    if args.sheet:
        artifacts["sheet"] = str(_survey_sheet(
            img, row_mean, row_detail, col_detail, scenarios,
            cl, ct, cr, cb, meta, Path(args.out_dir).expanduser().resolve()
            if args.out_dir else work_dir_for(src)))

    emit({"artifacts": artifacts,
          "source": str(src), "source_size": meta["source_size"],
          "analysis_size": meta["work_size"], "scale": round(meta["scale_x"], 4),
          "ratio_locked": f"{ratio_frac.numerator}:{ratio_frac.denominator}",
          "structure_lines": [
              {"y": int(round(r["y"] * meta["scale_y"])),
               "jump": r["jump"], "brighter_below": r["brighter_below"]}
              for r in _band_edges(row_mean)],
          "content_box_work": [cl, ct, cr, cb],
          "budget": budget,
          "clean_cuts": clean,
          "scenarios": scen_out,
          "how_to_read": "Scenarios are NOT ranked: there is no total score, "
                         "because a number would make 'which framing is better' "
                         "look computable. Read `removed_px` (what each edge "
                         "gives up), `cuts_content_px` (pixels of real content "
                         "destroyed) and `edge_penalty` (how much structure the "
                         "cut crosses, lower is cleaner), then choose by what "
                         "you are willing to sacrifice.",
          "next": "pick a scenario id, look at them together with "
                  "`candidates <image> --crops-src \"label:x,y,w,h;...\" --annotate`, "
                  "then render the chosen one with `finish --crop ...`.",
          "note": "All crop values are DISPLAY-oriented SOURCE pixels and already "
                  "snapped to an exact integer ratio."})


def _survey_sheet(img, row_mean, row_detail, col_detail, scenarios,
                  cl, ct, cr, cb, meta, out_dir: Path) -> Path:
    """One figure: every scenario dimmed outside the frame, with a ruler.

    Sized so a single look is enough: cells are capped in HEIGHT (not width),
    text sits above the image inside the header band, and three columns keep
    the whole thing within one screen of context.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = img.size
    cell_h = 420
    k = min(cell_h / h, 1.0)
    tw, th = max(1, int(w * k)), max(1, int(h * k))
    base = img.resize((tw, th), Image.LANCZOS).convert("RGB")
    sw, sh = meta["source_size"]

    cells = [("ORIGINAL", "content box in yellow", None)] + \
            [(f"S{i + 1}", rec["label"],
              f"{rec['width']}x{rec['height']}w  loses {rec['cuts_content_px']}px "
              f"content  r={rec['width'] / rec['height']:.3f}")
             for i, rec in enumerate(scenarios)]

    cols = 3 if len(cells) > 4 else 2
    pad, hdr = 10, 46
    rows = math.ceil(len(cells) / cols)
    sheet = Image.new("RGB", (cols * (tw + pad) + pad,
                              rows * (th + hdr + pad) + pad), (24, 25, 28))
    d = ImageDraw.Draw(sheet)
    f15, f12 = font(15), font(12)

    for i, (title, label, metrics) in enumerate(cells):
        col, row = i % cols, i // cols
        x = pad + col * (tw + pad)
        y = pad + row * (th + hdr + pad)
        d.text((x, y + 3), f"{title}  {label}", fill=(238, 238, 240), font=f15)
        if metrics:
            d.text((x, y + 24), metrics, fill=(140, 200, 150), font=f12)

        if scenarios and i > 0:
            b = scenarios[i - 1]["box"]
            m = Image.new("L", base.size, 0)
            ImageDraw.Draw(m).rectangle(
                [int(b[0] * k), int(b[1] * k), int(b[2] * k), int(b[3] * k)],
                fill=255)
            dark = ImageEnhance.Brightness(base).enhance(0.18)
            pane = Image.composite(base, dark, m)
            dd = ImageDraw.Draw(pane)
            dd.rectangle([int(b[0] * k), int(b[1] * k),
                          int(b[2] * k), int(b[3] * k)],
                         outline=(0, 235, 120), width=2)
        else:
            pane = base.copy()
            dd = ImageDraw.Draw(pane)
            dd.rectangle([int(cl * k), int(ct * k), int(cr * k), int(cb * k)],
                         outline=(255, 210, 0), width=2)
            for gy_ in np.linspace(0, h, 9)[1:-1]:
                yy = int(gy_ * k)
                dd.line([(0, yy), (tw, yy)], fill=(64, 66, 72), width=1)
                dd.text((3, yy + 1), str(int(round(gy_ * meta["scale_y"]))),
                        fill=(150, 152, 160), font=f12)
        sheet.paste(pane, (x, y + hdr))

    out = out_dir / "survey_sheet.jpg"
    sheet.save(out, "JPEG", quality=90)
    return out


# --- level ----------------------------------------------------------------
def _hough_lines(gmag, gother, axis: str, lo: int, hi: int,
                 max_deg: float = 4.5, step_deg: float = 0.1,
                 support_frac: float = 0.05, top_k: int = 8):
    """Find long, near-axis lines by Hough accumulation, not greedy tracking.

    Greedy continuity tracking works on a ceiling grid (one bright edge, one
    trough) and fails on a night skyline: every row has several vertical edges
    inside the search window, the tracker hops between them, and the inlier
    fraction collapses to ~7% while still returning a plausible-looking angle.
    Votes for (angle, offset) instead reward CONSISTENT lines, because a
    hopping track smears across many offset bins and produces no peak.

    Votes that fall outside the offset range are DISCARDED, never clamped:
    clamping funnels every steep line into the edge bin and manufactures a peak
    at exactly +-max_deg, which is what the first version "measured".
    """
    if axis == "v":
        band = gmag[lo:hi, :].astype(np.float32)
        other = gother[lo:hi, :].astype(np.float32)
        off_hi = band.shape[1] - 1
    else:
        band = gmag[:, lo:hi].astype(np.float32)
        other = gother[:, lo:hi].astype(np.float32)
        off_hi = band.shape[0] - 1

    thr = max(4.0, float(np.percentile(band, 92)))
    r, c = np.nonzero((band > thr) & (band > other))
    if r.size < 200:
        return []
    if axis == "v":
        t = r.astype(np.float64)          # row offset = distance along line
        b = c.astype(np.float64)          # column = position across line
    else:
        t = c.astype(np.float64)          # column = distance along line
        b = r.astype(np.float64)          # row = position across line

    n_ang = int(round(2 * max_deg / step_deg)) + 1
    slopes = np.tan(np.radians(np.linspace(-max_deg, max_deg, n_ang)))
    bin_px = 2.0
    n_off = int(off_hi / bin_px) + 2
    acc = np.zeros((n_ang, n_off), np.int32)
    for i, m in enumerate(slopes):
        oi = np.round((b - m * t) / bin_px).astype(np.int32)
        ok = (oi >= 0) & (oi < n_off)
        if ok.any():
            acc[i] = np.bincount(oi[ok], minlength=n_off)[:n_off]

    span = float(hi - lo)
    need = max(24, int(support_frac * span))
    out, work = [], acc.copy()
    for _ in range(top_k):
        i, j = np.unravel_index(int(np.argmax(work)), work.shape)
        if int(work[i, j]) < need:
            break
        m = slopes[i]
        off0 = j * bin_px
        oi = np.round((b - m * t) / bin_px).astype(np.int32)
        sel = np.abs(oi - j) <= 1
        ext = int(t[sel].max() - t[sel].min()) if sel.any() else 0
        out.append({"offset": int(round(off0)),
                    "offset_at_mid": int(round(off0 + m * span / 2.0)),
                    "angle_deg": round(float(np.degrees(np.arctan(m))), 2),
                    "support": int(work[i, j]),
                    "span_px": ext,
                    "span_frac": round(ext / max(span, 1.0), 2)})
        work[max(0, i - 2):i + 3, max(0, j - 2):j + 3] = 0
    out.sort(key=lambda x: -x["support"])
    return out


def cmd_level(args) -> None:
    require_numpy()
    src = Path(args.image).expanduser().resolve()
    meta = load_analysis(src, max_side=args.max, quality=args.quality)
    img = meta["img"]
    w, h = meta["work_size"]
    lum = _luma(img)
    gy = np.abs(np.diff(lum, axis=0))
    gx = np.abs(np.diff(lum, axis=1))
    gy = np.vstack([gy, gy[-1:]])
    gx = np.hstack([gx, gx[:, -1:]])

    # Vertical edges only exist inside the structure band: the sky above and
    # the blank foreground below carry no line to follow.
    ct, cb = _content_bounds(gy, axis=0)
    y0, y1 = (int(ct), int(cb)) if cb - ct > h * 0.2 else (int(h * 0.15), int(h * 0.92))

    verts = [{"x": int(round(v["offset_at_mid"] * meta["scale_x"])),
              "x_work": v["offset_at_mid"],
              "angle_deg": v["angle_deg"],
              "support": v["support"],
              "span_frac": v["span_frac"]}
             for v in _hough_lines(gx, gy, "v", y0, y1, top_k=args.lines)
             if v["span_frac"] >= 0.20]

    horis = [{"y": int(round(v["offset_at_mid"] * meta["scale_y"])),
              "y_work": v["offset_at_mid"],
              "angle_deg": v["angle_deg"],
              "support": v["support"],
              "span_frac": v["span_frac"]}
             for v in _hough_lines(gy, gx, "h", 0, w, top_k=args.lines)
             if v["span_frac"] >= 0.20]

    roll, verdict, confidence = _decide_roll(verts, horis,
                                             meta["source_size"][0])
    # Empirically calibrated on a synthetic grid: rotating an image by +a
    # (PIL counter-clockwise) makes its vertical edges read +a, and undoing it
    # takes a CLOCKWISE rotation of a -- which is `--rotate +a`, because
    # `--rotate` is positive-clockwise. Hence suggested = +roll, not -roll.
    suggested = None if roll is None else round(roll, 2)

    payload = {
        "artifacts": {},
        "source": str(src), "source_size": meta["source_size"],
        "analysis_size": [w, h], "scale": round(meta["scale_x"], 4),
        "structure_band_work": [y0, y1],
        "roll_deg": roll,
        "suggested_rotate": suggested,
        "confidence": confidence,
        "verdict": verdict,
        "vertical_spread": _spread_summary(verts, meta["source_size"][0]),
        "verticals": verts,
        "horizontals": horis,
        "horizontal_cross_check": (None if roll is None else
                                   f"if this is real roll, horizontals should "
                                   f"read about {-roll:+.2f} deg; lines that "
                                   f"disagree are receding perspective lines"),
        "rules": [
            "ROLL comes from vertical edges only: a roll tilts every vertical "
            "by the same angle, while keystone tilts them symmetrically.",
            "Near-field horizontals (railing, kerb, river bank) are RECEDING "
            "3D lines: they always look tilted, and that tilt is perspective, "
            "never roll. They are listed for structure, not for levelling.",
            "|angle| < 0.3 deg is 0 deg. A pixel-identical export is a bug, "
            "but so is rotating a frame that is already level.",
        ],
        "next": "high -> pass suggested_rotate to `finish --rotate`. medium -> "
                "too few edges agree to be a consensus; open --sheet and "
                "confirm before rotating. low / none -> rotate 0 and say the "
                "frame could not be measured.",
    }
    if args.sheet:
        out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir \
            else work_dir_for(src)
        payload["artifacts"]["sheet"] = str(
            _level_sheet(img, verts, horis, meta, out_dir))
    emit(payload)


def _spread_summary(verts, frame_w):
    """Where the vertical evidence actually sits, in one line.

    Reported next to the verdict because "12/12 agree" reads like overwhelming
    evidence until you see all twelve came from the same 6% of the frame.
    """
    if not verts:
        return None
    xs = np.array([v["x"] for v in verts], float)
    ang = np.array([v["angle_deg"] for v in verts], float)
    left = xs < frame_w / 2.0
    def _med(mask):
        a = ang[mask]
        return None if a.size == 0 else round(float(np.median(a)), 2)
    return {"x_range_pct": [round(float(xs.min()) / frame_w * 100, 1),
                            round(float(xs.max()) / frame_w * 100, 1)],
            "x_span_frac": round(float(xs.max() - xs.min()) / frame_w, 3),
            "left_count": int(left.sum()),
            "right_count": int((~left).sum()),
            "left_median_deg": _med(left),
            "right_median_deg": _med(~left),
            "usable": bool(left.any() and (~left).any()
                           and (xs.max() - xs.min()) / frame_w >= 0.35)}


def _decide_roll(verts, horis, frame_w=None):
    """Consensus roll from vertical edges; refuses to answer on thin evidence.

    v1.6.1: agreement alone is not enough. On IMG_0764 twelve edges agreed on
    -1.80 deg and `level` called it "high" -- but all twelve sat between 16% and
    22% of the frame width, i.e. twelve detections of ONE facade. A single
    facade cannot separate camera roll from keystone: shooting up at a tall
    building tilts that building's verticals by an amount that depends on how
    far it sits from the optical axis. Roll needs verticals on BOTH sides of
    the frame, so a one-sided or narrow-band consensus is now capped at "low".
    """
    if not verts:
        return None, ("no vertical edge survived the support/length test - "
                      "cannot measure roll; treat as 0 unless the eye "
                      "disagrees, and say so"), "none"
    ang = np.array([v["angle_deg"] for v in verts], float)
    med = float(np.median(ang))
    agree = int((np.abs(ang - med) < 0.3).sum())
    roll = 0.0 if abs(med) < 0.3 else round(med, 2)

    if frame_w:
        xs = np.array([v["x"] for v in verts], float)
        x_lo, x_hi = float(xs.min()), float(xs.max())
        spread = (x_hi - x_lo) / frame_w
        left = int((xs < frame_w / 2.0).sum())
        right = len(xs) - left
        if min(left, right) == 0 or spread < 0.35:
            return None, (
                f"median {med:+.2f} deg from {len(verts)} edges, but they all "
                f"lie in one narrow band (x {x_lo / frame_w * 100:.0f}-"
                f"{x_hi / frame_w * 100:.0f}% of the frame; {left} left / "
                f"{right} right). One facade cannot tell camera roll from "
                f"keystone, so this is NOT evidence -- treat as 0 deg and say "
                f"the frame was too one-sided to measure"), "low"

    # "3 of 12 agree" is a plurality, not a consensus. Require the agreeing
    # edges to be a real majority-ish share before calling it high confidence:
    # IMG_0896 (subway corridor) reports 3/12 at +0.95 deg while the grille
    # tracking says 2.2 deg -- the count alone would have called that "high".
    need = max(3, math.ceil(0.4 * len(verts)))
    if agree >= need:
        conf = "high"
    elif agree >= 2:
        conf = "medium"
    else:
        conf = "low"
    if conf == "low":
        return None, (f"vertical edges disagree (median {med:+.2f} deg, only "
                      f"{agree}/{len(verts)} within 0.3 deg) - do NOT rotate on "
                      f"this evidence"), conf
    verdict = (f"{agree}/{len(verts)} vertical edges agree at {med:+.2f} deg"
               + (" -> treat as level (below the 0.3 deg threshold)"
                  if roll == 0.0 else
                  f" -> rotate {roll:+.2f} deg to level"))
    return roll, verdict, conf


def _level_sheet(img, verts, horis, meta, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = img.size
    scale = 1200 / max(w, h)
    im = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS).convert("RGB")
    d = ImageDraw.Draw(im)
    f13 = font(13)
    for v in verts:
        x = v["x"] / meta["scale_x"] * scale
        ang = math.radians(v["lean_deg"])
        d.line([(x - math.tan(ang) * im.height / 2, 0),
                (x + math.tan(ang) * im.height / 2, im.height)],
               fill=(0, 230, 120), width=2)
        d.text((x + 3, 4), f"{v['lean_deg']:+.2f}", fill=(0, 230, 120), font=f13)
    for hz in horis:
        y = hz["y"] / meta["scale_y"] * scale
        d.line([(0, y), (im.width, y)], fill=(255, 120, 120), width=1)
    out = out_dir / "level_sheet.jpg"
    im.save(out, "JPEG", quality=91)
    return out


# --- panels ---------------------------------------------------------------
def cmd_panels(args) -> None:
    """Compose the N close-ups the agent would otherwise ask for one by one."""
    require_numpy()
    src = Path(args.image).expanduser().resolve()
    meta = load_analysis(src, max_side=args.max, quality=args.quality)
    img = meta["img"]
    w, h = img.size
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir \
        else work_dir_for(src)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.regions:
        regions = []
        for part in args.regions.split(";"):
            part = part.strip()
            if not part:
                continue
            label, coords = part.split(":", 1)
            x, y, rw, rh = [float(v) for v in coords.split(",")]
            regions.append((label.strip(),
                            int(x / meta["scale_x"]), int(y / meta["scale_y"]),
                            int(rw / meta["scale_x"]), int(rh / meta["scale_y"])))
    else:
        regions = [("TOP", 0, 0, w, int(h * 0.22)),
                   ("BOTTOM", 0, int(h * 0.78), w, int(h * 0.22)),
                   ("LEFT", 0, int(h * 0.20), int(w * 0.26), int(h * 0.60)),
                   ("RIGHT", int(w * 0.74), int(h * 0.20), int(w * 0.26), int(h * 0.60)),
                   ("MIDDLE", int(w * 0.20), int(h * 0.40), int(w * 0.60), int(h * 0.30))]

    items = []
    for label, x, y, rw, rh in regions:
        x = max(0, min(x, w - 8)); y = max(0, min(y, h - 8))
        rw = max(8, min(rw, w - x)); rh = max(8, min(rh, h - y))
        items.append((label, img.crop((x, y, x + rw, y + rh)), (x, y, rw, rh)))

    # Variable-height rows. A uniform grid sized to the tallest pane wastes most
    # of the sheet on a 3:4 frame (the tall left/right strips dictate the cell
    # height while the top/bottom bands fill 10% of it). So: a wide band spans
    # both columns, a narrow strip gets one column and is capped at maxh.
    cols, pad, hdr = 2, 12, 22
    cw = max(140, int(args.pane))
    full_w = cols * cw + (cols - 1) * pad
    maxh = int(cw * 1.3)

    blocks = []
    for label, crop, (x, y, rw, rh) in items:
        span = 2 if rw / max(rh, 1) >= 1.4 else 1
        box_w = full_w if span == 2 else cw
        k = min(box_w / rw, maxh / rh, 4.0)
        pane = crop.resize((max(16, int(rw * k)), max(16, int(rh * k))),
                           Image.LANCZOS)
        blocks.append((label, pane, (x, y, rw, rh), span))

    rows, cur = [], []
    for blk in blocks:
        if blk[3] == 2:
            if cur:
                rows.append(cur); cur = []
            rows.append([blk])
        else:
            cur.append(blk)
            if len(cur) >= cols:
                rows.append(cur); cur = []
    if cur:
        rows.append(cur)

    total_h = pad + sum(hdr + max(b[1].height for b in row) + pad
                        for row in rows)
    sheet = Image.new("RGB", (full_w + 2 * pad, total_h), (24, 25, 28))
    d = ImageDraw.Draw(sheet)
    f14 = font(14)
    sx, sy = meta["scale_x"], meta["scale_y"]

    cy = pad
    for row in rows:
        row_h = max(b[1].height for b in row)
        cx = pad
        for b in row:
            label, pane = b[0], b[1]
            x, y, rw, rh = b[2]
            d.text((cx, cy + 3),
                   f"{label}  src x{int(x * sx)}..{int((x + rw) * sx)}  "
                   f"y{int(y * sy)}..{int((y + rh) * sy)}",
                   fill=(235, 235, 235), font=f14)
            sheet.paste(pane, (cx, cy + hdr))
            d.rectangle([cx - 1, cy + hdr - 1,
                         cx + pane.width, cy + hdr + pane.height],
                        outline=(72, 74, 80))
            if args.ruler:
                for gx_ in np.linspace(0, rw, 7)[1:-1]:
                    px = cx + int(gx_ * (pane.width / rw))
                    d.line([(px, cy + hdr), (px, cy + hdr + 8)],
                           fill=(255, 210, 0))
                    d.text((px + 1, cy + hdr + 8), str(int((x + gx_) * sx)),
                           fill=(255, 210, 0), font=font(11))
            cx += pane.width + pad
        cy += hdr + row_h + pad

    out = out_dir / "panels.jpg"
    sheet.save(out, "JPEG", quality=91)
    emit({"artifacts": {"panels": str(out)},
          "source": str(src), "source_size": meta["source_size"],
          "analysis_size": [w, h], "scale": round(meta["scale_x"], 4),
          "regions": [{"label": lb, "src_box": [int(b[0] * meta["scale_x"]),
                                                int(b[1] * meta["scale_y"]),
                                                int(b[2] * meta["scale_x"]),
                                                int(b[3] * meta["scale_y"])]}
                      for lb, _, b, _ in blocks],
          "note": "One image instead of N separate looks. Labels are SOURCE px "
                  "so a coordinate read here can go straight into --crop."})



def build_parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="size / format / EXIF")
    p.add_argument("image")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("grid", help="thirds + golden + center overlay")
    p.add_argument("image")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_grid)

    p = sub.add_parser("candidates", help="crop candidates + contact sheet")
    p.add_argument("image")
    p.add_argument("--ratios", default="4:5,1:1,16:9")
    p.add_argument("--anchors", default="center,third-left,third-right")
    p.add_argument("--zoom", type=float, default=1.0,
                   help="shrink the inscribed box (<1) so horizontal anchors "
                        "have slack on tall images")
    p.add_argument("--rotate", type=float, default=0.0,
                   help="degrees, positive = clockwise. Rotates then crops back "
                        "to the original ratio (blank corners removed).")
    p.add_argument("--crops",
                   help="explicit boxes in the LOADED image space: "
                        "'label:x,y,w,h;label2:x,y,w,h'. Overrides "
                        "--ratios/--anchors; preferred for off-center subjects.")
    p.add_argument("--crops-src",
                   help="explicit boxes in SOURCE pixels (what `survey` prints). "
                        "Converted internally; use this when the input is a RAW.")
    p.add_argument("--annotate", action="store_true",
                   help="bigger cells + source coords, ratio and frame position "
                        "drawn on the sheet, so one look judges every candidate")
    p.add_argument("--max", type=int, default=0,
                   help="max side when decoding a RAW (default 1600); 0 = "
                        "leave a normal image at full size")
    p.add_argument("--keep-crops", action="store_true",
                   help="also write individual candidate JPEGs (default: only the "
                        "contact sheet, everything else stays in memory)")
    p.add_argument("--out-dir")
    p.add_argument("--quality", type=int, default=90)
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("survey",
                       help="structure lines + content box + crop budget + "
                            "frame table (deliberately unranked)")
    p.add_argument("image")
    p.add_argument("--ratio", help="target ratio (default: the source ratio)")
    p.add_argument("--max", type=int, default=1600, help="analysis decode side")
    p.add_argument("--top", type=int, default=8,
                   help="how many scenarios to list (default 8, decisive first)")
    p.add_argument("--min-removed", type=float, default=0.03, dest="min_removed",
                   help="drop scenarios that remove less than this fraction of "
                        "the frame area (default 0.03: a pixel-identical export "
                        "is a policy violation, not a framing)")
    p.add_argument("--sheet", action="store_true",
                   help="draw every suggestion on one annotated figure")
    p.add_argument("--out-dir")
    p.add_argument("--quality", type=int, default=88)
    p.set_defaults(func=cmd_survey)

    p = sub.add_parser("level",
                       help="measure roll from vertical edges (horizontals are perspective)")
    p.add_argument("image")
    p.add_argument("--lines", type=int, default=12,
                   help="how many edge candidates to test per axis")
    p.add_argument("--max", type=int, default=1600, help="analysis decode side")
    p.add_argument("--sheet", action="store_true",
                   help="draw the accepted lines over the frame")
    p.add_argument("--out-dir")
    p.add_argument("--quality", type=int, default=88)
    p.set_defaults(func=cmd_level)

    p = sub.add_parser("panels",
                       help="one multi-panel figure with a coordinate ruler")
    p.add_argument("image")
    p.add_argument("--regions",
                   help="'label:x,y,w,h;...' in SOURCE px (default: 5 auto bands)")
    p.add_argument("--pane", type=int, default=620, help="max pane side in px")
    p.add_argument("--ruler", action="store_true", default=True)
    p.add_argument("--max", type=int, default=2000, help="analysis decode side")
    p.add_argument("--out-dir")
    p.add_argument("--quality", type=int, default=88)
    p.set_defaults(func=cmd_panels)

    p = sub.add_parser("develop", help="crop-only pixel export (grading removed; legacy via --grade)")
    p.add_argument("image")
    p.add_argument("--crop", help="'x,y,w,h'")
    p.add_argument("--ratio")
    p.add_argument("--anchor", default="center", choices=ANCHORS)
    p.add_argument("--preset", default="none", choices=list(PRESETS))
    p.add_argument("--out")
    p.add_argument("--quality", type=int, default=92)
    for key in ("exposure", "temp", "shadows", "highlights", "contrast",
                "saturation", "sharpen", "grain", "vignette"):
        p.add_argument(f"--{key}", type=float, default=None)
    p.add_argument("--mono", action="store_true", default=None)
    p.add_argument("--grade", action="store_true",
                   help="LEGACY: enable color grading (off by default in v1.2.0)")
    p.set_defaults(func=cmd_develop)

    p = sub.add_parser("compare", help="side-by-side before/after")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("presets", help="LEGACY: list grading presets (not in default workflow)")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("formats", help="list output formats and the DNG policy")
    p.set_defaults(func=cmd_formats)

    p = sub.add_parser("raw-preview", help="small JPEG preview of a RAW/large image")
    p.add_argument("image")
    p.add_argument("--max", type=int, default=2000, help="max side in px (default 2000)")
    p.add_argument("--quality", type=int, default=88)
    p.add_argument("--grid", action="store_true",
                   help="also render the thirds/golden grid in the same call")
    p.add_argument("--out-dir")
    p.set_defaults(func=cmd_raw_preview)

    p = sub.add_parser("raw-export", help="full-size JPEG/PNG/TIFF decode of a RAW")
    p.add_argument("image")
    p.add_argument("--out")
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--quality", type=int, default=95)
    p.add_argument("--format", default="jpeg", choices=("jpeg", "png", "tiff"))
    p.add_argument("--keep-orientation", action="store_true",
                   help="skip EXIF orientation normalization (advanced; "
                        "crop coordinates then use STORED sensor axes)")
    p.set_defaults(func=cmd_raw_export)

    p = sub.add_parser("finish", help="RAW -> final image in one pass (no big intermediate)")
    p.add_argument("image")
    p.add_argument("--out")
    p.add_argument("--format", default="jpeg", choices=("jpeg", "png", "tiff"))
    p.add_argument("--quality", type=int, default=92)
    p.add_argument("--decode-quality", type=int, default=95)
    p.add_argument("--decode-format", default="jpeg", choices=("jpeg", "png", "tiff"),
                   help="temp decode format; jpeg is fast and small, png is lossless")
    p.add_argument("--crop", help="'x,y,w,h' in DISPLAY-oriented pixels")
    p.add_argument("--ratio")
    p.add_argument("--anchor", default="center", choices=ANCHORS)
    p.add_argument("--zoom", type=float, default=1.0,
                   help="tighten the framing while KEEPING the ratio "
                        "(1.0 = widest possible, 0.85 = 15%% tighter)")
    p.add_argument("--rotate", type=float, default=0.0,
                   help="degrees, positive = clockwise. Horizon levelling or a "
                        "deliberate frame tilt; the ratio is preserved by "
                        "cropping back to the largest inscribed rectangle.")
    p.add_argument("--sidecar", action="store_true",
                   help="also write a .json parameter record (default: image only)")
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser("map-crop", help="map preview crop coords back to original")
    p.add_argument("preview")
    p.add_argument("--crop", required=True, help="'x,y,w,h' on the PREVIEW image")
    p.add_argument("--meta", help="preview json sidecar (default: <preview>.json)")
    p.set_defaults(func=cmd_map_crop)

    p = sub.add_parser("crop-plan", help="non-destructive crop parameters, no image written")
    p.add_argument("image")
    p.add_argument("--crop", help="'x,y,w,h' in original pixels")
    p.add_argument("--ratio")
    p.add_argument("--anchor", default="center", choices=ANCHORS)
    p.add_argument("--out", help="default: <stem>_crop.json")
    p.set_defaults(func=cmd_crop_plan)

    p = sub.add_parser("xmp-crop", help="write Camera Raw XMP sidecar, source untouched")
    p.add_argument("image")
    p.add_argument("--from-plan", help="crop.json produced by crop-plan")
    p.add_argument("--crop", help="alternatively: 'x,y,w,h' in original pixels")
    p.add_argument("--out", help="default: <stem>.xmp next to the image")
    p.set_defaults(func=cmd_xmp_crop)
    return ap


if __name__ == "__main__":
    args = build_parser().parse_args()
    try:
        args.func(args)
    except Exception as exc:  # keep errors machine-readable
        die(f"{type(exc).__name__}: {exc}")
