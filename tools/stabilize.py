#!/usr/bin/env python3
"""Steady walk cycles from an AI sprite sheet: one upper body, AI legs, coded bounce.

Why: an image model redraws the whole character in every cell of a sheet, so
the details boil (hat, eyes, hair, coat folds), the size pulses by a few
percent and the head and body drift against each other. Played back, that
reads as twitching and flicker, even when every single frame looks fine.

What this tool does instead:
  1. cleans the matte: drops the soft halo the model paints around the
     figure and rebuilds a thin anti-aliased edge;
  2. aligns every frame on its torso (scale and position) against one
     master frame;
  3. keeps the master frame's upper body (head, torso, arms, backpack) for
     every frame and takes only the legs from each AI frame, with the feet
     planted on one ground line; the leg tops are extended a little so they
     always tuck under the coat;
  4. adds a designed bounce (down after each step, up at the passing pose)
     in whole output pixels, so apart from that bounce the upper body is
     pixel-identical in every frame.

    python3 tools/stabilize.py art/raw/004-chibi-walk/posed-walk-xiaoxue__X_1.png --grid 4x2 \\
        --name xiaoxue-chibi-walk-steady --label "Q版小雪：走路（稳定版）" --ref 2

The coat hem (where the upper body ends and the legs begin) is found from
the coat colour, sampled from the master's torso. Check the debug image
(--debug DIR) for a new character: green line = hem, orange = legs taken
from that frame, blue = synthesised leg extension.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sprites import label_blobs, write_animation  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LUMA = np.array([0.299, 0.587, 0.114], np.float32)


# ------------------------------------------------------------------ matte ----

def clean_matte(cell: np.ndarray, thr: float = 200.0, ss: int = 4) -> np.ndarray:
    """Cut the figure at the alpha=thr contour with sub-pixel anti-aliasing; drop specks."""
    h, w = cell.shape[:2]
    a = cell[..., 3]
    core = a >= thr
    labels, blobs, f = label_blobs(core)
    if blobs:
        biggest = max(b["size"] for b in blobs)
        keep = [b["id"] for b in blobs if b["size"] >= 0.02 * biggest]
        big = np.repeat(np.repeat(np.isin(labels, keep), f, 0), f, 1)[:h, :w]
        core &= big
    up = np.asarray(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).resize((w * ss, h * ss), Image.BILINEAR))
    cov = (up >= thr).reshape(h, ss, w, ss).mean(axis=(1, 3)).astype(np.float32)
    near = np.zeros_like(core)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            near |= np.roll(np.roll(core, dy, 0), dx, 1)
    cov = np.where(near, cov, 0.0)
    # Pixels just outside the core get the colour of their core neighbours (no halo tint).
    rgb = cell[..., :3].copy()
    filled = core.copy()
    for _ in range(2):
        acc = np.zeros_like(rgb)
        cnt = np.zeros(core.shape, np.float32)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                sf = np.roll(np.roll(filled, dy, 0), dx, 1) & ~filled
                acc[sf] += np.roll(np.roll(rgb, dy, 0), dx, 1)[sf]
                cnt[sf] += 1
        new = cnt > 0
        rgb[new] = acc[new] / cnt[new][:, None]
        filled |= new
    return np.dstack([rgb, cov * 255.0]).astype(np.float32)


# --------------------------------------------------------------- resample ----

def premul(rgba: np.ndarray) -> np.ndarray:
    a = rgba[..., 3:4] / 255.0
    return np.concatenate([rgba[..., :3] * a, rgba[..., 3:4]], axis=2)


def unpremul(pm: np.ndarray) -> np.ndarray:
    a = np.clip(pm[..., 3:4], 0, 255)
    rgb = np.where(a > 0.5, pm[..., :3] * 255.0 / np.maximum(a, 1e-3), 0.0)
    return np.concatenate([np.clip(rgb, 0, 255), a], axis=2)


def resize(rgba: np.ndarray, s: float) -> np.ndarray:
    """Scale with premultiplied alpha (no dark or light fringes)."""
    if abs(s - 1.0) < 1e-6:
        return rgba.copy()
    h, w = rgba.shape[:2]
    size = (max(1, round(w * s)), max(1, round(h * s)))
    pm = premul(rgba)
    chans = [np.asarray(Image.fromarray(pm[..., c], "F").resize(size, Image.BICUBIC)) for c in range(4)]
    return unpremul(np.stack(chans, axis=2))


def shift_crop(img: np.ndarray, dx: int, dy: int, h: int, w: int) -> np.ndarray:
    """out[y, x] = img[y + dy, x + dx]; zero outside."""
    out = np.zeros((h, w, img.shape[2]), np.float32)
    y0, x0 = max(0, -dy), max(0, -dx)
    y1, x1 = min(h, img.shape[0] - dy), min(w, img.shape[1] - dx)
    if y1 > y0 and x1 > x0:
        out[y0:y1, x0:x1] = img[y0 + dy:y1 + dy, x0 + dx:x1 + dx]
    return out


def register(ref: np.ndarray, img: np.ndarray, box, scales, search: int = 40):
    """Scale s and offset d so that resize(img, s)[p + d] ~ ref[p] inside box (y0, y1, x0, x1)."""
    y0, y1, x0, x1 = box
    T = premul(ref)[y0:y1, x0:x1]
    best = None
    for s in scales:
        I = premul(resize(img, s))
        P = search + 4
        Ip = np.zeros((I.shape[0] + 2 * P + ref.shape[0], I.shape[1] + 2 * P + ref.shape[1], 4), np.float32)
        Ip[P:P + I.shape[0], P:P + I.shape[1]] = I

        def ssd(dx, dy):
            sub = Ip[P + y0 + dy:P + y1 + dy, P + x0 + dx:P + x1 + dx]
            return float(((sub - T) ** 2).sum())

        cy, cx = (y0 + y1) / 2, (x0 + x1) / 2
        ex, ey = round(cx * s - cx), round(cy * s - cy)
        e, bx, by = min((ssd(ex + dx, ey + dy), ex + dx, ey + dy)
                        for dy in range(-search, search + 1, 2) for dx in range(-search, search + 1, 2))
        for _ in range(4):
            e, bx, by = min((ssd(bx + dx, by + dy), bx + dx, by + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1))
        if best is None or e < best[0]:
            best = (e, s, bx, by)
    return best


# -------------------------------------------------------------- segments ----

def figure_box(rgba: np.ndarray):
    m = rgba[..., 3] > 128
    rows, cols = np.where(m.sum(1) >= 3)[0], np.where(m.sum(0) >= 3)[0]
    return int(rows[0]), int(rows[-1]), int(cols[0]), int(cols[-1])


def coat_colour(master: np.ndarray, box) -> np.ndarray:
    """Most common colour in the torso box: the coat."""
    y0, y1, x0, x1 = box
    px = master[y0:y1, x0:x1].reshape(-1, 4)
    px = px[px[:, 3] > 250][:, :3]
    q = (px // 24).astype(np.int32)
    keys = q[:, 0] * 10000 + q[:, 1] * 100 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    top = vals[np.argmax(counts)]
    return px[keys == top].mean(0)


def coat_mask(rgba: np.ndarray, coat: np.ndarray, lum_tol: float = 80.0, chroma_tol: float = 40.0) -> np.ndarray:
    rgb = rgba[..., :3]
    lum = rgb @ LUMA
    chroma = rgb - lum[..., None]
    c_lum = float(coat @ LUMA)
    c_chroma = coat - c_lum
    ok = (np.abs(lum - c_lum) < lum_tol) & (np.sqrt(((chroma - c_chroma) ** 2).sum(-1)) < chroma_tol)
    return ok & (rgba[..., 3] > 128)


def find_hem(coat: np.ndarray, y_start: int, y_end: int, max_gap: int = 8) -> np.ndarray:
    """Lowest coat row per column (smooth curve); NaN outside the coat."""
    h, w = coat.shape
    raw = np.full(w, np.nan)
    for x in range(w):
        col = coat[:, x]
        last, gap, started = -1, 0, False
        for y in range(y_start, min(h, y_end)):
            if col[y]:
                last, gap, started = y, 0, True
            elif started:
                gap += 1
                if gap > max_gap:
                    break
            elif y > y_start + 30:
                break
        if last >= 0:
            raw[x] = last
    xs = np.where(~np.isnan(raw))[0]
    if len(xs) < 10:
        sys.exit("could not find the coat hem; check --ref and the debug image")
    # Robust quadratic fit: gloves or pockets that hide the hem make upward dips (outliers).
    keep = np.ones(len(xs), bool)
    for _ in range(5):
        c = np.polyfit(xs[keep], raw[xs][keep], 2)
        fit = np.polyval(c, xs)
        keep = raw[xs] > fit - 5
    hem = np.full(w, np.nan)
    span = np.arange(xs.min(), xs.max() + 1)
    hem[span] = np.polyval(c, span)
    return hem


def extend_edges(hem: np.ndarray) -> np.ndarray:
    xs = np.where(~np.isnan(hem))[0]
    out = hem.copy()
    out[:xs.min()] = hem[xs.min()]
    out[xs.max() + 1:] = hem[xs.max()]
    return out


def legs_mask(rgba: np.ndarray, cut: np.ndarray, feet_zone: int) -> np.ndarray:
    """Opaque pixels below the cut line that belong to blobs reaching the feet zone."""
    h, w = rgba.shape[:2]
    below = (np.arange(h)[:, None] > cut[None, :]) & (rgba[..., 3] > 8)
    labels, blobs, f = label_blobs(below, factor=1)
    keep = [b["id"] for b in blobs if b["y1"] >= feet_zone]
    return np.isin(labels, keep)


def feet_line(mask: np.ndarray) -> int:
    rows = np.where(mask.sum(1) >= 3)[0]
    return int(rows[-1])


# ------------------------------------------------------------------ main ----

def bounce(n: int, amp: int, low: int) -> list[int]:
    """Whole-pixel bounce: two dips per loop, lowest at frame `low` (the down pose)."""
    return [int(round(amp * math.cos(2 * math.pi * 2 * (k - low) / n))) for k in range(n)]


def main():
    ap = argparse.ArgumentParser(description="Steady walk cycle: one upper body, AI legs, coded bounce.")
    ap.add_argument("image")
    ap.add_argument("--grid", default="4x2")
    ap.add_argument("--name", required=True)
    ap.add_argument("--label")
    ap.add_argument("--ref", type=int, default=2, help="master frame (a passing pose with a neutral arm works best)")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--target-height", type=int, default=400, help="figure height in output pixels")
    ap.add_argument("--bounce", type=int, default=3, help="bounce amplitude in output pixels (each way)")
    ap.add_argument("--low", type=int, default=1, help="frame where the body is lowest (down pose)")
    ap.add_argument("--debug", help="write diagnostic images into this folder")
    a = ap.parse_args()

    cols, rows = (int(v) for v in a.grid.lower().split("x"))
    sheet = np.asarray(Image.open(a.image).convert("RGBA")).astype(np.float32)
    H, W = sheet.shape[0] // rows, sheet.shape[1] // cols
    cells = [clean_matte(sheet[r * H:(r + 1) * H, c * W:(c + 1) * W]) for r in range(rows) for c in range(cols)]
    n = len(cells)
    master = cells[a.ref]
    top, bot, left, right = figure_box(master)
    fh = bot - top
    torso_box = (top + int(0.44 * fh), top + int(0.77 * fh), max(0, left - 6), min(W, right + 6))
    scales = [round(0.94 + 0.01 * i, 2) for i in range(13)]

    # 1. align every frame on the torso
    aligned, regs = [], []
    for k, c in enumerate(cells):
        e, s, dx, dy = register(master, c, torso_box, scales)
        aligned.append(shift_crop(resize(c, s), dx, dy, H, W))
        regs.append((s, dx, dy))
        print(f"frame {k}: torso scale {s:.2f}, offset ({dx:+d}, {dy:+d})")

    # 2. hem of the coat in every frame
    coat = coat_colour(master, torso_box)
    y_start, y_end = top + int(0.62 * fh), top + int(0.90 * fh)
    hems = [find_hem(coat_mask(f, coat), y_start, y_end) for f in aligned]
    hem_m = extend_edges(hems[a.ref])
    coat_cols = ~np.isnan(hems[a.ref])
    feet_zone = top + int(0.92 * fh)

    # 3. master upper body = master minus its legs
    legs_m = legs_mask(master, hem_m + 3, feet_zone)
    upper = master.copy()
    upper[legs_m] = 0
    ground = feet_line(legs_m)

    S = a.target_height / fh
    out_w, out_h = round(W * S), round(H * S)
    bob = bounce(n, a.bounce, a.low)
    upper_out = resize(upper, S)
    if upper_out.shape[:2] != (out_h, out_w):
        upper_out = shift_crop(upper_out, 0, 0, out_h, out_w)

    frames, debug = [], []
    for k, f in enumerate(aligned):
        hem_k = extend_edges(hems[k])
        cut = hem_k + 6
        lm = legs_mask(f, cut, feet_zone)
        v = ground - feet_line(lm)              # plant the feet
        b_raw = bob[k] / S
        legs = np.where(lm[..., None], f, 0.0)
        ext = np.zeros(lm.shape, bool)
        for x in np.where(coat_cols)[0]:
            ys = np.where(lm[:, x])[0]
            if len(ys) == 0 or ys[0] > cut[x] + 2:
                continue                        # this column's leg does not reach the hem
            target = int(math.floor(hem_m[x] + b_raw - v - 4))
            src_y = min(ys[0] + 3, ys[-1])
            if target < ys[0]:
                legs[target:ys[0], x] = f[src_y, x]
                legs[target:ys[0], x, 3] = 255.0
                ext[target:ys[0], x] = True
        legs = shift_crop(legs, 0, -v, H, W)
        legs_out = resize(legs, S)
        if legs_out.shape[:2] != (out_h, out_w):
            legs_out = shift_crop(legs_out, 0, 0, out_h, out_w)
        up = shift_crop(upper_out, 0, -bob[k], out_h, out_w)
        canvas = Image.fromarray(np.clip(legs_out, 0, 255).astype(np.uint8), "RGBA")
        canvas.alpha_composite(Image.fromarray(np.clip(up, 0, 255).astype(np.uint8), "RGBA"))
        frames.append(canvas)
        if a.debug:
            dbg = Image.fromarray(np.clip(f, 0, 255).astype(np.uint8), "RGBA")
            ov = np.zeros((H, W, 4), np.uint8)
            ov[lm] = (255, 140, 0, 110)
            ov[ext] = (60, 120, 255, 200)
            dbg.alpha_composite(Image.fromarray(ov, "RGBA"))
            d = ImageDraw.Draw(dbg)
            pts = [(x, float(hem_k[x])) for x in range(W)]
            d.line(pts, fill=(0, 230, 80, 255), width=1)
            debug.append(dbg)
        print(f"frame {k}: feet moved {v:+d} px, bounce {bob[k]:+d} px, leg extension {int(ext.sum())} px")

    # common crop, anchor at the feet
    boxes = [fr.getbbox() for fr in frames]
    x0 = max(0, min(b[0] for b in boxes) - 8)
    y0 = max(0, min(b[1] for b in boxes) - 8)
    x1 = min(out_w, max(b[2] for b in boxes) + 8)
    y1 = min(out_h, max(b[3] for b in boxes) + 8)
    frames = [fr.crop((x0, y0, x1, y1)) for fr in frames]
    anchor = (round((left + right) / 2 * S) - x0, round(ground * S) - y0)
    info = {"method": "tools/stabilize.py: master upper body + AI legs + coded bounce", "ref": a.ref,
            "bounce_px": bob, "torso_registration": [list(r) for r in regs], "grid": a.grid}
    out = write_animation(a.name, frames, anchor, a.fps, str(Path(a.image)), info, a.label)
    print(f"{a.name}: {n} frames {frames[0].width}x{frames[0].height} -> {out}")
    if a.debug:
        dd = Path(a.debug)
        dd.mkdir(parents=True, exist_ok=True)
        strip = Image.new("RGBA", (W * n, H), (60, 70, 110, 255))
        for k, im in enumerate(debug):
            strip.alpha_composite(im, (k * W, 0))
        strip.save(dd / f"{a.name}-segments.png")
        print(f"debug -> {dd / (a.name + '-segments.png')}")


if __name__ == "__main__":
    main()
