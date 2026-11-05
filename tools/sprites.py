#!/usr/bin/env python3
"""Turn AI output (sprite sheets or video clips) into clean, aligned game animations.

    python3 tools/sprites.py sheet art/raw/003-anim-test/sheet-walk__X_1.png --grid 4x2 --name sunburst-walk
    python3 tools/sprites.py video art/raw/video-001-anim-test/video-walk__X.mp4 --name sora-walk
    python3 tools/sprites.py preview sunburst-walk sora-walk --out art/preview/anim-test.html

Each animation is written to art/anim/<name>/:
    frame_00.png ...  RGBA frames on one shared canvas, feet on one baseline
    sheet.png         all frames packed into one texture (for the engine)
    anim.json         frame size, count, fps, anchor (feet point), source, loop quality
    preview.gif       quick look on a dark background

Background removal: real alpha is used when present; otherwise the flat
background colour is estimated from the image border and keyed out, with
colour-spill suppression on soft edges (for green-screen video frames).
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ANIM_ROOT = ROOT / "art" / "anim"
PREVIEW_BG = (34, 42, 54)  # cold dark slate, close to the in-game night interior
MAX_SHEET_WIDTH = 4096     # safe texture width on phones


# ------------------------------------------------------------ background ----

def has_real_alpha(arr: np.ndarray) -> bool:
    return float((arr[..., 3] < 16).mean()) > 0.05


def estimate_bg(arr: np.ndarray) -> np.ndarray:
    h, w = arr.shape[:2]
    b = max(2, min(h, w) // 100)
    border = np.concatenate([arr[:b].reshape(-1, 4), arr[-b:].reshape(-1, 4),
                             arr[:, :b].reshape(-1, 4), arr[:, -b:].reshape(-1, 4)])
    return np.median(border[:, :3], axis=0)


def key_out(arr: np.ndarray, bg: np.ndarray | None = None, t0: float = 40.0, t1: float = 110.0) -> np.ndarray:
    """Return a float RGBA array (0..255) with the background made transparent."""
    if has_real_alpha(arr):
        return arr.copy()
    if bg is None:
        bg = estimate_bg(arr)
    rgb = arr[..., :3]
    dist = np.sqrt(((rgb - bg) ** 2).sum(-1))
    alpha = np.clip((dist - t0) / (t1 - t0), 0.0, 1.0)
    out = arr.copy()
    ch = int(np.argmax(bg))
    others = [c for c in range(3) if c != ch]
    if bg[ch] - max(bg[others[0]], bg[others[1]]) > 60:  # strongly coloured screen: suppress spill
        # Spill sits on soft edges and, with video chroma subsampling, on the first
        # opaque pixels inside the outline too: treat a 2 px band inside the edge as edge.
        eroded = Image.fromarray((alpha * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(5))
        edge = (np.asarray(eroded) / 255.0) < 0.98
        limit = np.maximum(rgb[..., others[0]], rgb[..., others[1]])
        out[..., ch] = np.where(edge, np.minimum(rgb[..., ch], limit), rgb[..., ch])
    out[..., 3] = alpha * 255.0
    return out


# ------------------------------------------------------------- alignment ----

def label_blobs(solid: np.ndarray, factor: int = 4):
    """Connected blobs (8-connectivity) on a downsampled mask. Returns (labels, blobs, factor)."""
    h, w = solid.shape
    hs, ws = math.ceil(h / factor), math.ceil(w / factor)
    padded = np.zeros((hs * factor, ws * factor), bool)
    padded[:h, :w] = solid
    small = padded.reshape(hs, factor, ws, factor).any(axis=(1, 3))
    labels = np.zeros(small.shape, np.int32)
    blobs = []
    for sy in range(hs):
        for sx in range(ws):
            if not small[sy, sx] or labels[sy, sx]:
                continue
            bid = len(blobs) + 1
            labels[sy, sx] = bid
            stack, ys, xs = [(sy, sx)], [], []
            while stack:
                y, x = stack.pop()
                ys.append(y)
                xs.append(x)
                for yy in (y - 1, y, y + 1):
                    for xx in (x - 1, x, x + 1):
                        if 0 <= yy < hs and 0 <= xx < ws and small[yy, xx] and not labels[yy, xx]:
                            labels[yy, xx] = bid
                            stack.append((yy, xx))
            blobs.append({"id": bid, "size": len(ys), "y0": min(ys), "y1": max(ys), "x0": min(xs),
                          "x1": max(xs), "cy": sum(ys) / len(ys), "cx": sum(xs) / len(xs)})
    return labels, blobs, factor


def split_sheet(arr: np.ndarray, cols: int, rows: int) -> tuple[list[np.ndarray], list[str]]:
    """Cut a sprite sheet into one array per figure, in reading order.

    Figures are found on the whole sheet and assigned to the cell holding their
    centre, so a figure that pokes out of its cell stays whole and does not leak
    into the neighbouring frame.
    """
    h, w = arr.shape[:2]
    labels, blobs, f = label_blobs(arr[..., 3] > 64)
    warnings = []
    if not blobs:
        return [], ["no figures found"]
    biggest = max(b["size"] for b in blobs)
    cell_w, cell_h = w / cols, h / rows
    groups: dict[int, list[int]] = {k: [] for k in range(cols * rows)}
    for b in blobs:
        if b["size"] < 0.005 * biggest:  # specks
            continue
        if (b["x1"] - b["x0"] + 1) * f > 1.4 * cell_w or (b["y1"] - b["y0"] + 1) * f > 1.4 * cell_h:
            warnings.append("two figures touch each other; that pair may be cut badly")
        c = min(cols - 1, int(b["cx"] * f / cell_w))
        r = min(rows - 1, int(b["cy"] * f / cell_h))
        groups[r * cols + c].append(b["id"])
    frames = []
    for k in range(cols * rows):
        if not groups[k]:
            warnings.append(f"cell {k} (row {k // cols}, column {k % cols}) is empty")
            continue
        kept = np.repeat(np.repeat(np.isin(labels, groups[k]), f, 0), f, 1)[:h, :w]
        kept = np.asarray(Image.fromarray((kept * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(2 * f + 1))) > 0
        ys, xs = np.nonzero(kept)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        fr = arr[y0:y1, x0:x1].copy()
        fr[..., 3] = np.where(kept[y0:y1, x0:x1], fr[..., 3], 0)
        frames.append(fr)
    return frames, warnings


def measure(rgba: np.ndarray) -> dict | None:
    """Bounding box, feet line and upper-body centre of the figure."""
    m = Image.fromarray(((rgba[..., 3] > 128) * 255).astype(np.uint8))
    m = np.asarray(m.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))) > 0  # drop specks
    rows = np.where(m.sum(1) >= 3)[0]
    cols = np.where(m.sum(0) >= 3)[0]
    if len(rows) == 0 or len(cols) == 0:
        return None
    top, bottom, left, right = int(rows[0]), int(rows[-1]), int(cols[0]), int(cols[-1])
    upper = m[top: top + max(1, int((bottom - top) * 0.45))]
    xs = np.nonzero(upper)[1]
    cx = float(xs.mean()) if len(xs) else (left + right) / 2
    return {"top": top, "bottom": bottom, "left": left, "right": right,
            "cx": cx, "height": bottom - top + 1}


def align(frames: list[np.ndarray], target_height: int | None, drift_tol: float = 0.10):
    """Put every frame on one canvas with the feet point at the same spot.

    Frames whose figure height drifts more than drift_tol from the median are
    rescaled (AI sheets sometimes change the character's size between cells).
    """
    measured = [(f, measure(f)) for f in frames]
    measured = [(f, m) for f, m in measured if m]
    if not measured:
        sys.exit("no figure found in any frame (background removal failed?)")
    med = float(np.median([m["height"] for _, m in measured]))
    base_scale = target_height / med if target_height else 1.0
    margin = 6
    items, rescaled = [], 0
    for f, m in measured:
        s = base_scale
        if abs(m["height"] - med) / med > drift_tol:
            s *= med / m["height"]
            rescaled += 1
        img = Image.fromarray(np.clip(f, 0, 255).astype(np.uint8), "RGBA")
        x0, y0 = max(0, m["left"] - margin), max(0, m["top"] - margin)
        x1, y1 = min(img.width, m["right"] + 1 + margin), min(img.height, m["bottom"] + 1 + margin)
        crop = img.crop((x0, y0, x1, y1))
        ax, ay = m["cx"] - x0, m["bottom"] - y0
        if abs(s - 1.0) > 1e-3:
            crop = crop.resize((max(1, round(crop.width * s)), max(1, round(crop.height * s))), Image.LANCZOS)
            ax, ay = ax * s, ay * s
        items.append((crop, ax, ay))
    pad = 8
    left = max(ax for _, ax, _ in items)
    right = max(c.width - ax for c, ax, _ in items)
    up = max(ay for _, _, ay in items)
    down = max(c.height - ay for c, _, ay in items)
    W, H = int(math.ceil(left + right)) + 2 * pad, int(math.ceil(up + down)) + 2 * pad
    anchor = (int(round(left)) + pad, int(round(up)) + pad)
    out = []
    for crop, ax, ay in items:
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        canvas.paste(crop, (int(round(anchor[0] - ax)), int(round(anchor[1] - ay))))
        out.append(canvas)
    return out, anchor, {"median_height": med, "scale": base_scale, "rescaled_frames": rescaled}


# ----------------------------------------------------------------- loops ----

def _features(images: list[Image.Image]) -> np.ndarray:
    feats = []
    for im in images:
        bg = Image.new("RGBA", im.size, (128, 128, 128, 255))
        bg.alpha_composite(im)
        g = bg.convert("L").resize((48, max(1, round(48 * im.height / im.width))), Image.BILINEAR)
        feats.append(np.asarray(g, dtype=np.float32) / 255.0)
    return np.stack(feats)


def find_loop(images: list[Image.Image], fps: float, min_s: float, max_s: float, skip_s: float):
    """Pick start i and length L so frame i+L looks like frame i (seamless wrap)."""
    n = len(images)
    feats = _features(images)
    lmin = max(2, round(min_s * fps))
    lmax = min(n - 1, round(max_s * fps))
    if lmax < lmin:
        return 0, n, None
    start_min = min(round(skip_s * fps), max(0, n - lmax - 1))
    def d(a, b):
        return float(np.abs(feats[a] - feats[b]).mean())

    # Compare a 3-frame window, not a single frame: a pose that merely matches while
    # moving the opposite way (e.g. legs crossing) must not count as a loop point.
    best_per_len = {}
    for length in range(lmin, lmax + 1):
        costs = [((d(i - 1, i - 1 + length) + d(i, i + length) + d(i + 1, i + 1 + length)) / 3, i)
                 for i in range(max(1, start_min), n - length - 1)]
        if costs:
            best_per_len[length] = min(costs)
    if not best_per_len:
        return 0, n, None
    global_best = min(c for c, _ in best_per_len.values())
    # Several whole cycles match equally well: take the shortest (fewest frames to ship).
    length = min(L for L, (c, _) in best_per_len.items() if c <= global_best * 1.25 + 0.003)
    cost, start = best_per_len[length]
    return start, length, cost


# ---------------------------------------------------------------- output ----

def write_animation(name: str, frames: list[Image.Image], anchor, fps: float, source: str,
                    info: dict, label: str | None, root: Path | None = None) -> Path:
    out = (root or ANIM_ROOT) / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    names = []
    for k, im in enumerate(frames):
        fname = f"frame_{k:02d}.png"
        im.save(out / fname, optimize=True)
        names.append(fname)
    W, H = frames[0].size
    cols = max(1, min(len(frames), MAX_SHEET_WIDTH // W))
    rows = math.ceil(len(frames) / cols)
    sheet = Image.new("RGBA", (cols * W, rows * H), (0, 0, 0, 0))
    for k, im in enumerate(frames):
        sheet.paste(im, ((k % cols) * W, (k // cols) * H))
    sheet.save(out / "sheet.png", optimize=True)
    gif = []
    for im in frames:
        bg = Image.new("RGBA", im.size, PREVIEW_BG + (255,))
        bg.alpha_composite(im)
        gif.append(bg.convert("RGB"))
    gif[0].save(out / "preview.gif", save_all=True, append_images=gif[1:],
                duration=max(20, round(1000 / fps)), loop=0)
    meta = {"name": name, "label": label or name, "fps": fps, "frame_count": len(frames),
            "frame_size": [W, H], "anchor_feet": list(anchor), "sheet": {"file": "sheet.png",
            "columns": cols, "rows": rows}, "frames": names, "source": source, "info": info}
    (out / "anim.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def load_rgba(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGBA"), dtype=np.float32)


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def cmd_sheet(a):
    src = Path(a.image)
    cols, rows = (int(v) for v in a.grid.lower().split("x"))
    arr = key_out(load_rgba(src))
    cells, warnings = split_sheet(arr, cols, rows)
    for msg in warnings:
        print(f"WARNING: {msg}")
    if len(cells) < 2:
        sys.exit("fewer than 2 figures found: is the grid right, and was the background removed?")
    frames, anchor, info = align(cells, a.target_height)
    info["grid"] = a.grid
    info["warnings"] = warnings
    path = write_animation(a.name, frames, anchor, a.fps, rel(src), info, a.label)
    print(f"{a.name}: {len(frames)} frames {frames[0].size[0]}x{frames[0].size[1]} -> {rel(path)} "
          f"(rescaled {info['rescaled_frames']} drifting frame(s))")


def cmd_video(a):
    src = Path(a.video)
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required for video input")
    tmp = Path(tempfile.mkdtemp(prefix="frames_"))
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vf", f"fps={a.fps}",
                        str(tmp / "f_%04d.png")], check=True)
        files = sorted(tmp.glob("f_*.png"))
        if len(files) < 4:
            sys.exit(f"only {len(files)} frames extracted from {src}")
        first = load_rgba(files[0])
        bg = None if has_real_alpha(first) else estimate_bg(first)  # one key colour for the whole clip
        keyed = [key_out(load_rgba(f), bg) for f in files]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    aligned, anchor, info = align(keyed, a.target_height)
    start, length, cost = find_loop(aligned, a.fps, a.min_loop, a.max_loop, a.skip)
    frames = aligned[start:start + length]
    info.update({"extracted_frames": len(aligned), "loop_start": start, "loop_length": length,
                 "loop_cost": None if cost is None else round(cost, 4),
                 "loop_quality": None if cost is None else
                 ("seamless" if cost < 0.012 else "ok" if cost < 0.025 else "visible jump")})
    path = write_animation(a.name, frames, anchor, a.fps, rel(src), info, a.label)
    print(f"{a.name}: {len(aligned)} frames extracted, loop = frames {start}..{start + length - 1} "
          f"({length} frames, {length / a.fps:.2f}s, {info['loop_quality']}) -> {rel(path)}")


PREVIEW_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --bg:#141a22; --card:#1d2530; --ink:#e8eef2; --dim:#8fa3b5; --warm:#f2a65a; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.5 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  header { padding:20px 24px 8px; }
  h1 { margin:0 0 4px; font-size:20px; font-weight:600; }
  header p { margin:0; color:var(--dim); }
  .bar { display:flex; flex-wrap:wrap; gap:16px; align-items:center; padding:12px 24px; color:var(--dim); }
  .bar button { background:#2a3542; color:var(--ink); border:1px solid #3c4a5c; border-radius:6px;
                padding:5px 12px; font:inherit; cursor:pointer; }
  .bar button.on { border-color:var(--warm); color:var(--warm); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); gap:16px; padding:8px 24px 32px; }
  .card { background:var(--card); border-radius:10px; overflow:hidden; }
  .stage { display:flex; align-items:flex-end; justify-content:center; height:340px; padding:12px; }
  .stage.dark { background:#222a36; }
  .stage.light { background:#d9e1e8; }
  .stage.check { background:conic-gradient(#9aa4ad 25%, #c9d0d6 0 50%, #9aa4ad 0 75%, #c9d0d6 0) 0 0/24px 24px; }
  canvas { max-width:100%; max-height:100%; image-rendering:auto; }
  .stage.small canvas { height:120px; width:auto; }  /* about the size of a Q-version figure in the game */
  .meta { padding:10px 14px 14px; }
  .meta b { display:block; font-weight:600; }
  .meta span { color:var(--dim); font-size:13px; }
</style></head>
<body>
<header><h1>__TITLE__</h1><p>__SUBTITLE__</p></header>
<div class="bar">
  <span>速度</span>
  <button data-speed="0.5">0.5×</button><button data-speed="1" class="on">1×</button><button data-speed="2">2×</button>
  <span>背景</span>
  <button data-bg="dark" class="on">深色</button><button data-bg="light">浅色</button><button data-bg="check">棋盘格</button>
  <span>大小</span>
  <button data-size="big" class="on">放大看</button><button data-size="small">游戏里的大小</button>
  <button id="pause">暂停</button>
</div>
<div class="grid" id="grid"></div>
<script>
const ANIMS = __DATA__;
let speed = 1, paused = false, bgClass = "dark";
const players = [];
for (const a of ANIMS) {
  const card = document.createElement("div"); card.className = "card";
  card.innerHTML = `<div class="stage ${bgClass}"><canvas width="${a.w}" height="${a.h}"></canvas></div>
    <div class="meta"><b></b><span></span></div>`;
  card.querySelector("b").textContent = a.label;
  card.querySelector("span").textContent = `${a.frames.length} 帧 · ${a.fps} fps · ${a.info}`;
  document.getElementById("grid").appendChild(card);
  const imgs = a.frames.map(src => { const i = new Image(); i.src = src; return i; });
  players.push({ a, imgs, ctx: card.querySelector("canvas").getContext("2d"),
                 stage: card.querySelector(".stage"), t: 0, idx: -1 });
}
let last = performance.now();
function tick(now) {
  const dt = Math.max(0, (now - last) / 1000); last = now;  // first rAF stamp can precede `last`
  for (const p of players) {
    if (!paused) p.t += dt * speed;
    const n = p.imgs.length, idx = ((Math.floor(p.t * p.a.fps) % n) + n) % n;
    if (idx !== p.idx && p.imgs[idx].complete) {
      p.ctx.clearRect(0, 0, p.a.w, p.a.h); p.ctx.drawImage(p.imgs[idx], 0, 0); p.idx = idx;
    }
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
document.querySelectorAll("[data-speed]").forEach(b => b.onclick = () => {
  speed = +b.dataset.speed;
  document.querySelectorAll("[data-speed]").forEach(x => x.classList.toggle("on", x === b));
});
document.querySelectorAll("[data-bg]").forEach(b => b.onclick = () => {
  bgClass = b.dataset.bg;
  players.forEach(p => p.stage.className = "stage " + bgClass + sizeClass);
  document.querySelectorAll("[data-bg]").forEach(x => x.classList.toggle("on", x === b));
});
let sizeClass = "";
document.querySelectorAll("[data-size]").forEach(b => b.onclick = () => {
  sizeClass = b.dataset.size === "small" ? " small" : "";
  players.forEach(p => p.stage.className = "stage " + bgClass + sizeClass);
  document.querySelectorAll("[data-size]").forEach(x => x.classList.toggle("on", x === b));
});
document.getElementById("pause").onclick = e => {
  paused = !paused; e.target.textContent = paused ? "播放" : "暂停";
};
</script></body></html>
"""


def cmd_preview(a):
    entries = []
    for name in a.anims:
        d = Path(name) if Path(name).is_dir() else ANIM_ROOT / name
        meta = json.loads((d / "anim.json").read_text(encoding="utf-8"))
        frames = []
        for fname in meta["frames"]:
            buf = io.BytesIO()
            Image.open(d / fname).save(buf, "WEBP", quality=88, method=4)
            frames.append("data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode())
        inf = meta.get("info", {})
        note = inf.get("loop_quality") or (f"网格 {inf['grid']}" if inf.get("grid") else "")
        entries.append({"label": meta["label"], "fps": meta["fps"], "frames": frames,
                        "w": meta["frame_size"][0], "h": meta["frame_size"][1], "info": note})
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = (PREVIEW_HTML.replace("__TITLE__", a.title).replace("__SUBTITLE__", a.subtitle)
            .replace("__DATA__", json.dumps(entries)))
    out.write_text(html, encoding="utf-8")
    print(f"preview with {len(entries)} animation(s) -> {rel(out)} ({out.stat().st_size // 1024} KB)")


def cmd_contact(a):
    """Numbered overview of a batch so the user can answer with a number."""
    files = sorted(f for f in Path(a.folder).glob("*.png") if not f.name.endswith("_ref.png"))
    if a.only:
        files = [f for f in files if any(k in f.name for k in a.only)]
    if not files:
        sys.exit(f"no images in {a.folder}")
    cols, t, label_h, pad = a.cols, a.thumb, 44, 16
    rows = math.ceil(len(files) / cols)
    sheet = Image.new("RGB", (pad + cols * (t + pad), pad + rows * (t + label_h + pad)), (20, 26, 34))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=26)
    for k, f in enumerate(files):
        im = Image.open(f).convert("RGBA")
        im.thumbnail((t, t))
        bg = Image.new("RGBA", im.size, (96, 104, 114, 255))  # mid gray shows transparent assets
        bg.alpha_composite(im)
        x, y = pad + (k % cols) * (t + pad), pad + (k // cols) * (t + label_h + pad)
        sheet.paste(bg.convert("RGB"), (x + (t - im.width) // 2, y + (t - im.height) // 2))
        job, idx = f.stem.split("__")[0], f.stem.rsplit("_", 1)[-1]
        draw.text((x, y + t + 8), f"{k + 1}. {job} #{idx}", fill=(232, 238, 242), font=font)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"contact sheet with {len(files)} image(s) -> {out}")
    for k, f in enumerate(files):
        print(f"  {k + 1}. {rel(f)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sheet", help="slice an AI sprite sheet into aligned frames")
    s.add_argument("image")
    s.add_argument("--grid", default="4x2", help="columns x rows (default 4x2)")
    s.add_argument("--name", required=True)
    s.add_argument("--label")
    s.add_argument("--fps", type=float, default=10)
    s.add_argument("--target-height", type=int, default=400, help="figure height in output px")
    s.set_defaults(func=cmd_sheet)

    v = sub.add_parser("video", help="extract a seamless loop from a video clip")
    v.add_argument("video")
    v.add_argument("--name", required=True)
    v.add_argument("--label")
    v.add_argument("--fps", type=float, default=12)
    v.add_argument("--target-height", type=int, default=400)
    v.add_argument("--min-loop", type=float, default=0.6, help="shortest loop, seconds")
    v.add_argument("--max-loop", type=float, default=2.5, help="longest loop, seconds")
    v.add_argument("--skip", type=float, default=0.4, help="ignore the first N seconds (motion ramp-up)")
    v.set_defaults(func=cmd_video)

    p = sub.add_parser("preview", help="build a side-by-side HTML comparison page")
    p.add_argument("anims", nargs="+", help="animation names (art/anim/<name>) or folders")
    p.add_argument("--out", default="art/preview/anim-test.html")
    p.add_argument("--title", default="人物动画对比")
    p.add_argument("--subtitle", default="同一个角色、同一个动作，用不同方法做出来的动画，并排比较。")
    p.set_defaults(func=cmd_preview)

    c = sub.add_parser("contact", help="numbered overview image of a batch folder")
    c.add_argument("folder", help="e.g. art/raw/001-style-explore")
    c.add_argument("--out", required=True)
    c.add_argument("--only", action="append", help="only files whose name contains this (repeatable)")
    c.add_argument("--cols", type=int, default=3)
    c.add_argument("--thumb", type=int, default=480, help="thumbnail size in px")
    c.set_defaults(func=cmd_contact)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
