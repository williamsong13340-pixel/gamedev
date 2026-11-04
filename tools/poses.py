#!/usr/bin/env python3
"""Pose guides: animation skeletons that the image model paints the character onto.

The motion is designed here, as a small 2D rig with proper animation keys
(contact / down / passing / up for walking, wind-up / push / return for work).
The image model only has to draw the character in each given pose, so the
motion is always correct and the loop always closes. All 8 frames go into one
4x2 sheet so the model paints them together, which keeps the character
consistent from frame to frame.

Colour code in the guide (repeated in the prompt):
    orange limbs  = near (camera-side) arm and leg
    blue limbs    = far arm and leg, behind the body
    gray          = head, torso, props (e.g. a wrench)
    dark wedge    = nose: the direction the head faces (right)

    python3 tools/poses.py                     # templates/poses/<cycle>_sheet.png + poses.json
    python3 tools/poses.py --preview DIR       # also animated previews (frames + GIF) in DIR/pose-<cycle>/
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sprites import write_animation  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "templates" / "poses"

SS = 2
CELL_W, CELL_H = 384, 512
COLS, ROWS = 4, 2
BASELINE = 480  # feet line inside each cell

NEAR = (242, 140, 40, 255)
FAR = (74, 123, 208, 255)
BODY = (160, 166, 172, 255)
HEAD = (200, 205, 210, 255)
NOSE = (70, 74, 80, 255)
PROP = (120, 126, 132, 255)
BOLT = (60, 64, 70, 255)

# Proportions in cell pixels: templates/character_pose.png scaled to a 420 px tall figure (~4 heads).
P = {
    "head_w": 91, "head_h": 101, "neck": 10, "torso": 132, "shoulder_drop": 18, "hip_drop": 12,
    "sh_near_dx": -14, "sh_far_dx": 12, "hip_near_dx": -6, "hip_far_dx": 6,
    "upper_arm": 68, "forearm": 61, "hand_r": 13, "thigh": 78, "shin": 74,
    "ankle_h": 14, "heel": 10, "foot_len": 44,
    "w_upper_arm": 22, "w_forearm": 19, "w_thigh": 30, "w_shin": 25, "w_foot": 16,
    "torso_w": 74, "pelvis_w": 68,
}


# ----------------------------------------------------------------- maths ----

def vec(angle_deg: float, length: float) -> tuple[float, float]:
    """Vector of `length`; angle measured from straight down, positive = forward (+x)."""
    a = math.radians(angle_deg)
    return length * math.sin(a), length * math.cos(a)


def add(p, v):
    return p[0] + v[0], p[1] + v[1]


def lerp_keys(keys: list[float], phase: float) -> float:
    """Smoothly interpolate a looping list of evenly spaced keyframes."""
    n = len(keys)
    x = (phase % 1.0) * n
    i = int(x)
    f = x - i
    p0, p1, p2, p3 = keys[(i - 1) % n], keys[i % n], keys[(i + 1) % n], keys[(i + 2) % n]
    return 0.5 * (2 * p1 + (-p0 + p2) * f + (2 * p0 - 5 * p1 + 4 * p2 - p3) * f * f
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * f ** 3)  # Catmull-Rom


def ik2(root, target, l1, l2, bend: int):
    """Two-bone IK in 2D: middle joint position. bend=+1/-1 picks the elbow/knee side."""
    dx, dy = target[0] - root[0], target[1] - root[1]
    d = max(abs(l1 - l2) + 1e-3, min(l1 + l2 - 1e-3, math.hypot(dx, dy)))
    a = math.acos(max(-1.0, min(1.0, (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d))))
    base = math.atan2(dy, dx)
    ang = base + bend * a
    return root[0] + l1 * math.cos(ang), root[1] + l1 * math.sin(ang)


def foot_points(ankle, pitch_deg):
    """Heel and toe on the sole; pitch > 0 = toe down."""
    c, s = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))

    def rot(x, y):
        return ankle[0] + x * c - y * s, ankle[1] + x * s + y * c

    return rot(-P["heel"], P["ankle_h"]), rot(P["foot_len"] - P["heel"], P["ankle_h"])


def upper_body(pelvis, lean, head_tilt):
    up = (math.sin(math.radians(lean)), -math.cos(math.radians(lean)))
    down = (-up[0], -up[1])
    neck = add(pelvis, (up[0] * P["torso"], up[1] * P["torso"]))
    sh_base = add(neck, (down[0] * P["shoulder_drop"], down[1] * P["shoulder_drop"]))
    ht = math.radians(lean * 0.5 + head_tilt)
    head_c = add(neck, (math.sin(ht) * (P["neck"] + P["head_h"] / 2), -math.cos(ht) * (P["neck"] + P["head_h"] / 2)))
    return {
        "pelvis": pelvis, "neck": neck, "head": head_c, "head_tilt": lean * 0.5 + head_tilt,
        "shoulder_near": add(sh_base, (P["sh_near_dx"], 0)), "shoulder_far": add(sh_base, (P["sh_far_dx"], 0)),
        "hip_near": add(pelvis, (P["hip_near_dx"], P["hip_drop"])),
        "hip_far": add(pelvis, (P["hip_far_dx"], P["hip_drop"])),
    }


# ---------------------------------------------------------------- cycles ----

# One leg over a walk cycle, 8 keys starting at heel contact (degrees).
WALK_THIGH = [24, 16, 4, -8, -20, -2, 10, 20]   # forward +
WALK_KNEE = [4, 18, 8, 10, 28, 52, 46, 18]      # flexion
WALK_FOOT = [-14, 0, 0, 6, 32, 16, 0, -10]      # toe-down +


def pose_walk(t: float) -> dict:
    j = upper_body((0.0, 0.0), lean=5, head_tilt=0)
    for side, phase in (("near", t), ("far", t + 0.5)):
        thigh = lerp_keys(WALK_THIGH, phase)
        shin = thigh - lerp_keys(WALK_KNEE, phase)
        knee = add(j[f"hip_{side}"], vec(thigh, P["thigh"]))
        ankle = add(knee, vec(shin, P["shin"]))
        heel, toe = foot_points(ankle, lerp_keys(WALK_FOOT, phase))
        j.update({f"knee_{side}": knee, f"ankle_{side}": ankle, f"heel_{side}": heel, f"toe_{side}": toe})
        # The arm on the same side swings against its leg.
        arm = -18 * math.cos(2 * math.pi * phase)
        flex = 12 + 26 * max(0.0, arm / 18)
        elbow = add(j[f"shoulder_{side}"], vec(arm, P["upper_arm"]))
        wrist = add(elbow, vec(arm + flex, P["forearm"]))
        j.update({f"elbow_{side}": elbow, f"wrist_{side}": wrist})
    # Ground contact: lowest heel/toe sits on the baseline (this also produces the natural bob).
    low = max(j[k][1] for k in ("heel_near", "toe_near", "heel_far", "toe_far"))
    shift = (CELL_W * 0.5, BASELINE - low)
    return {"joints": {k: (add(v, shift) if isinstance(v, tuple) else v) for k, v in j.items()}, "props": []}


def pose_work(t: float) -> dict:
    """Tightening a bolt with a big wrench: wind up (t=0), push down (t=0.5), return."""
    c = math.cos(2 * math.pi * t)
    push = (1 - c) / 2  # 0 at wind-up, 1 at the bottom of the push
    cx = CELL_W * 0.40
    pelvis = (cx + 7 * push, 313 + 6 * push)
    j = upper_body(pelvis, lean=12 + 10 * push, head_tilt=8)
    feet = {"near": (cx + 32, BASELINE - P["ankle_h"]), "far": (cx - 30, BASELINE - P["ankle_h"])}
    for side in ("near", "far"):
        ankle = feet[side]
        knee = ik2(j[f"hip_{side}"], ankle, P["thigh"], P["shin"], bend=-1)  # knees point forward
        heel, toe = foot_points(ankle, 0)
        j.update({f"knee_{side}": knee, f"ankle_{side}": ankle, f"heel_{side}": heel, f"toe_{side}": toe})
    bolt = (cx + 150, 280)
    beta = math.radians(-34 * c)  # handle angle: raised at wind-up, pushed down mid-cycle
    direction = (-math.cos(beta), math.sin(beta))
    handle_end = add(bolt, (direction[0] * 72, direction[1] * 72))
    grips = {"near": handle_end, "far": add(bolt, (direction[0] * 50, direction[1] * 50))}
    for side in ("near", "far"):
        elbow = ik2(j[f"shoulder_{side}"], grips[side], P["upper_arm"], P["forearm"], bend=1)  # elbows down
        j.update({f"elbow_{side}": elbow, f"wrist_{side}": grips[side]})
    props = [{"type": "wrench", "bolt": bolt, "handle_end": add(bolt, (direction[0] * 86, direction[1] * 86))}]
    return {"joints": j, "props": props}


CYCLES = {
    "walk": {"fn": pose_walk, "fps": 10, "label": "骨架：走路"},
    "work": {"fn": pose_work, "fps": 8, "label": "骨架：拧螺栓"},
}


# -------------------------------------------------------------- drawing ----

class Layer:
    def __init__(self, w: int, h: int, bg):
        self.w, self.h = w, h
        self.img = Image.new("RGBA", (w * SS, h * SS), bg)
        self.d = ImageDraw.Draw(self.img)

    def circle(self, c, r, fill):
        x, y = c
        self.d.ellipse([(x - r) * SS, (y - r) * SS, (x + r) * SS, (y + r) * SS], fill=fill)

    def capsule(self, a, b, w, fill):
        self.d.line([a[0] * SS, a[1] * SS, b[0] * SS, b[1] * SS], fill=fill, width=round(w * SS))
        self.circle(a, w / 2, fill)
        self.circle(b, w / 2, fill)

    def polygon(self, pts, fill):
        self.d.polygon([(x * SS, y * SS) for x, y in pts], fill=fill)

    def done(self) -> Image.Image:
        return self.img.resize((self.w, self.h), Image.LANCZOS)


def draw_pose(layer: Layer, pose: dict, ox: float = 0, oy: float = 0):
    j = {k: (v[0] + ox, v[1] + oy) if isinstance(v, tuple) else v for k, v in pose["joints"].items()}

    def limb_set(side, color):
        return [
            lambda: layer.capsule(j[f"hip_{side}"], j[f"knee_{side}"], P["w_thigh"], color),
            lambda: layer.capsule(j[f"knee_{side}"], j[f"ankle_{side}"], P["w_shin"], color),
            lambda: layer.capsule(j[f"heel_{side}"], j[f"toe_{side}"], P["w_foot"], color),
        ], [
            lambda: layer.capsule(j[f"shoulder_{side}"], j[f"elbow_{side}"], P["w_upper_arm"], color),
            lambda: layer.capsule(j[f"elbow_{side}"], j[f"wrist_{side}"], P["w_forearm"], color),
            lambda: layer.circle(j[f"wrist_{side}"], P["hand_r"], color),
        ]

    far_leg, far_arm = limb_set("far", FAR)
    near_leg, near_arm = limb_set("near", NEAR)
    for f in far_arm + far_leg:
        f()
    # torso + pelvis
    layer.capsule(j["neck"], j["pelvis"], P["torso_w"], BODY)
    layer.capsule(add(j["pelvis"], (-P["pelvis_w"] / 2 + 18, 4)), add(j["pelvis"], (P["pelvis_w"] / 2 - 18, 4)),
                  P["pelvis_w"] * 0.62, BODY)
    # head: rotated ellipse + nose wedge on the facing side
    hc, tilt = j["head"], math.radians(j["head_tilt"])
    a, b = P["head_w"] / 2, P["head_h"] / 2
    pts = []
    for k in range(48):
        u = 2 * math.pi * k / 48
        x, y = a * math.cos(u), b * math.sin(u)
        pts.append((hc[0] + x * math.cos(tilt) - y * math.sin(tilt), hc[1] + x * math.sin(tilt) + y * math.cos(tilt)))
    layer.capsule(j["neck"], add(j["neck"], (0, -P["neck"] - 6)), 30, BODY)
    layer.polygon(pts, HEAD)

    def head_pt(x, y):
        return hc[0] + x * math.cos(tilt) - y * math.sin(tilt), hc[1] + x * math.sin(tilt) + y * math.cos(tilt)

    layer.polygon([head_pt(a - 5, 2), head_pt(a + 6, 10), head_pt(a - 5, 17)], NOSE)
    layer.circle(head_pt(a * 0.45, -8), 5, NOSE)
    for prop in pose["props"]:
        if prop["type"] == "wrench":
            bolt = (prop["bolt"][0] + ox, prop["bolt"][1] + oy)
            end = (prop["handle_end"][0] + ox, prop["handle_end"][1] + oy)
            layer.capsule(bolt, end, 12, PROP)
            layer.circle(bolt, 15, PROP)
            layer.circle(bolt, 6, BOLT)
    for f in near_leg + near_arm:
        f()


def frame_image(pose: dict, bg) -> Image.Image:
    layer = Layer(CELL_W, CELL_H, bg)
    draw_pose(layer, pose)
    return layer.done()


def sheet_image(poses: list[dict]) -> Image.Image:
    layer = Layer(CELL_W * COLS, CELL_H * ROWS, (255, 255, 255, 255))
    for k, pose in enumerate(poses):
        draw_pose(layer, pose, (k % COLS) * CELL_W, (k // COLS) * CELL_H)
    return layer.done().convert("RGB")


def main():
    ap = argparse.ArgumentParser(description="Render animation pose guides.")
    ap.add_argument("--preview", help="also write animated previews (frames + GIF) into this folder")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"note": "Generated by tools/poses.py. Cells are 384x512 in a 4x2 sheet; feet line y=480 in each cell.",
            "cell": [CELL_W, CELL_H], "grid": [COLS, ROWS], "baseline": BASELINE,
            "legend": {"orange": "near arm and leg", "blue": "far arm and leg", "gray": "head, torso, props",
                       "dark wedge": "nose / facing direction"}, "cycles": {}}
    for name, cyc in CYCLES.items():
        poses = [cyc["fn"](k / (COLS * ROWS)) for k in range(COLS * ROWS)]
        sheet_image(poses).save(OUT / f"{name}_sheet.png", optimize=True)
        meta["cycles"][name] = {"fps": cyc["fps"], "frames": [
            {k: [round(v[0], 1), round(v[1], 1)] for k, v in p["joints"].items() if isinstance(v, tuple)}
            for p in poses]}
        print(f"wrote templates/poses/{name}_sheet.png")
        if a.preview:
            frames = [frame_image(p, (0, 0, 0, 0)) for p in poses]
            path = write_animation(f"pose-{name}", frames, (CELL_W // 2, BASELINE), cyc["fps"],
                                   "tools/poses.py", {"grid": "4x2"}, cyc["label"], root=Path(a.preview))
            print(f"preview -> {path}")
    (OUT / "poses.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
