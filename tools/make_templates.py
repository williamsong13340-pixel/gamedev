#!/usr/bin/env python3
"""Generate the gray-box layout templates that every AI asset is painted onto.

Why templates: the image model is asked to *repaint* these silhouettes instead
of inventing new ones. That keeps every carriage the same size (they couple
cleanly on the track), makes each interior line up pixel-for-pixel with its
exterior (needed for the X-ray cutaway transition), and makes every character
share one pose so they can all use the same skeleton rig.

Run:
    python3 tools/make_templates.py

Writes into templates/:
    carriage_exterior.png   1536x1024  side view of a standard carriage
    carriage_interior.png   1536x1024  same outline, near wall removed (cutaway)
    locomotive_exterior.png 1536x1024  locomotive, facing right (direction of travel)
    character_pose.png      1024x1536  shared rig pose, 3/4 view facing right
    templates.json          every anchor coordinate (wheels, couplers, floor
                            line, joints...) for the game engine to use
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "templates"

SS = 2  # supersampling factor for smooth (anti-aliased) edges

# Grayscale only: templates must not bias the model's colour choices.
BG = (230, 230, 230)
BODY = (138, 138, 138)
ROOF = (110, 110, 110)
DARK = (74, 74, 74)
WHEEL = (58, 58, 58)
HUB = (106, 106, 106)
WINDOW = (196, 196, 196)
WINDOW_FRAME = (90, 90, 90)
SHELL_CUT = (62, 62, 62)
BACK_WALL = (166, 166, 166)
FLOOR = (124, 124, 124)
OPENING = (189, 189, 189)
NEAR = (138, 138, 138)
FAR = (106, 106, 106)
SKIN = (154, 154, 154)

# Shared track geometry: every rolling stock template uses the same rail line.
CANVAS_WIDE = (1536, 1024)
RAIL_Y = 860  # y where wheels touch the rail


class Canvas:
    """Draw in final-pixel coordinates; rendered at SS x and downsampled."""

    def __init__(self, size: tuple[int, int]):
        self.size = size
        self.img = Image.new("RGB", (size[0] * SS, size[1] * SS), BG)
        self.d = ImageDraw.Draw(self.img)

    @staticmethod
    def _s(box):
        return [v * SS for v in box]

    def rect(self, box, fill, radius=0):
        if radius:
            self.d.rounded_rectangle(self._s(box), radius=radius * SS, fill=fill)
        else:
            self.d.rectangle(self._s(box), fill=fill)

    def ellipse(self, box, fill):
        self.d.ellipse(self._s(box), fill=fill)

    def circle(self, cx, cy, r, fill):
        self.ellipse((cx - r, cy - r, cx + r, cy + r), fill)

    def polygon(self, pts, fill):
        self.d.polygon([(x * SS, y * SS) for x, y in pts], fill=fill)

    def capsule(self, p0, p1, width, fill):
        """Thick line with round caps (limbs, rods)."""
        self.d.line([p0[0] * SS, p0[1] * SS, p1[0] * SS, p1[1] * SS], fill=fill, width=width * SS)
        r = width / 2
        self.circle(p0[0], p0[1], r, fill)
        self.circle(p1[0], p1[1], r, fill)

    def save(self, path: Path):
        self.img.resize(self.size, Image.LANCZOS).save(path, optimize=True)


# ---------------------------------------------------------------- carriage ----

CARRIAGE = {
    "canvas": list(CANVAS_WIDE),
    "rail_y": RAIL_Y,
    "body": [140, 300, 1396, 740],
    "roof": [126, 268, 1410, 316],
    "chassis": [150, 740, 1386, 772],
    "gangway_left": [112, 450, 140, 740],
    "gangway_right": [1396, 450, 1424, 740],
    "coupler_left": [92, 744, 150, 762],
    "coupler_right": [1386, 744, 1444, 762],
    "bogies": [[250, 778, 530, 812], [1006, 778, 1286, 812]],
    "wheel_radius": 52,
    "wheel_centers": [[305, 808], [475, 808], [1061, 808], [1231, 808]],
    "windows": [[303, 400, 473, 520], [683, 400, 853, 520], [1063, 400, 1233, 520]],
    # Distance between coupler tips: carriages are placed this far apart.
    "coupling_length": 1444 - 92,
}

INTERIOR = {
    "shell_thickness": 22,
    "ceiling_slab": [140, 300, 1396, 330],
    "floor_slab": [140, 706, 1396, 740],
    "back_wall": [162, 330, 1374, 706],
    "floor_band": [162, 680, 1374, 706],
    "door_left": [140, 470, 162, 706],
    "door_right": [1374, 470, 1396, 706],
    "walk_line_y": 700,  # characters' feet are placed on this line
    "walk_x_range": [190, 1346],
}


def draw_running_gear(c: Canvas, spec: dict):
    c.rect(spec["chassis"], DARK)
    c.rect(spec["coupler_left"], DARK)
    c.rect(spec["coupler_right"], DARK)
    for b in spec["bogies"]:
        c.rect(b, DARK, radius=8)
    r = spec["wheel_radius"]
    for cx, cy in spec["wheel_centers"]:
        c.circle(cx, cy, r, WHEEL)
        c.circle(cx, cy, r * 0.35, HUB)


def draw_window(c: Canvas, box):
    x0, y0, x1, y1 = box
    c.rect((x0 - 8, y0 - 8, x1 + 8, y1 + 8), WINDOW_FRAME, radius=10)
    c.rect(box, WINDOW, radius=6)


def make_carriage_exterior(path: Path):
    s = CARRIAGE
    c = Canvas(CANVAS_WIDE)
    draw_running_gear(c, s)
    c.rect(s["gangway_left"], DARK)
    c.rect(s["gangway_right"], DARK)
    c.rect(s["body"], BODY, radius=14)
    c.rect(s["roof"], ROOF, radius=24)
    for w in s["windows"]:
        draw_window(c, w)
    c.save(path)


def make_carriage_interior(path: Path):
    s, i = CARRIAGE, INTERIOR
    c = Canvas(CANVAS_WIDE)
    draw_running_gear(c, s)
    c.rect(s["gangway_left"], DARK)
    c.rect(s["gangway_right"], DARK)
    # Cut shell (outer outline) drawn dark, then the room inside it.
    c.rect(s["body"], SHELL_CUT, radius=14)
    c.rect(s["roof"], ROOF, radius=24)
    c.rect(i["back_wall"], BACK_WALL)
    c.rect(i["floor_band"], FLOOR)
    c.rect(i["door_left"], OPENING)
    c.rect(i["door_right"], OPENING)
    # Back-wall windows sit at the same x positions as the exterior windows.
    for w in s["windows"]:
        draw_window(c, w)
    c.save(path)


# -------------------------------------------------------------- locomotive ----

LOCOMOTIVE = {
    "canvas": list(CANVAS_WIDE),
    "rail_y": RAIL_Y,
    "facing": "right",
    "cab": [170, 300, 470, 740],
    "cab_roof": [150, 270, 490, 312],
    "cab_window": [240, 360, 380, 470],
    "boiler": [450, 445, 1170, 695],
    "smokebox": [1150, 432, 1296, 708],
    "stack": [1190, 318, 1252, 436],
    "stack_cap": [1172, 292, 1270, 324],
    "dome": [800, 395, 900, 450],
    "headlamp": [1262, 384, 1300, 428],
    "frame": [160, 700, 1300, 740],
    "cylinder": [1040, 690, 1140, 742],
    "plow": [[1292, 700], [1334, 700], [1446, 858], [1292, 858]],
    "coupler_rear": [100, 744, 170, 762],
    "driver_radius": 80,
    "driver_centers": [[620, 780], [800, 780], [980, 780]],
    "small_wheel_radius": 46,
    "small_wheel_centers": [[300, 814], [1170, 814]],
    "rod_y": 800,
    "smoke_origin": [1221, 292],
    "headlight_origin": [1300, 406],
}


def make_locomotive(path: Path):
    s = LOCOMOTIVE
    c = Canvas(CANVAS_WIDE)
    c.rect(s["frame"], DARK)
    c.rect(s["coupler_rear"], DARK)
    c.polygon(s["plow"], DARK)
    c.rect(s["cylinder"], DARK, radius=10)
    c.rect(s["cab"], BODY, radius=12)
    c.rect(s["cab_roof"], ROOF, radius=20)
    draw_window(c, s["cab_window"])
    c.rect(s["boiler"], BODY, radius=18)
    c.rect(s["smokebox"], ROOF, radius=22)
    c.rect(s["stack"], ROOF)
    c.rect(s["stack_cap"], DARK, radius=8)
    c.rect(s["dome"], ROOF, radius=40)
    c.rect(s["headlamp"], WINDOW, radius=8)
    r = s["driver_radius"]
    for cx, cy in s["driver_centers"]:
        c.circle(cx, cy, r, WHEEL)
        c.circle(cx, cy, r * 0.3, HUB)
    rs = s["small_wheel_radius"]
    for cx, cy in s["small_wheel_centers"]:
        c.circle(cx, cy, rs, WHEEL)
        c.circle(cx, cy, rs * 0.35, HUB)
    x_first = s["driver_centers"][0][0]
    x_last = s["driver_centers"][-1][0]
    c.capsule((x_first, s["rod_y"]), (x_last, s["rod_y"]), 14, HUB)
    c.save(path)


# --------------------------------------------------------------- character ----

def _limb_end(p, length, angle_deg):
    """Point `length` away from p, angle measured from straight down (+ = image-left)."""
    a = math.radians(angle_deg)
    return [round(p[0] - length * math.sin(a)), round(p[1] + length * math.cos(a))]


def character_spec() -> dict:
    # ~4 heads tall, 3/4 view facing right. "near" = side closer to the camera
    # (drawn on image-left, in front); "far" = image-right, behind the torso.
    j = {}
    j["near_shoulder"] = [410, 545]
    j["near_elbow"] = _limb_end(j["near_shoulder"], 200, 18)
    j["near_wrist"] = _limb_end(j["near_elbow"], 180, 14)
    j["far_shoulder"] = [598, 545]
    j["far_elbow"] = _limb_end(j["far_shoulder"], 200, -14)
    j["far_wrist"] = _limb_end(j["far_elbow"], 180, -10)
    j["near_hip"] = [462, 915]
    j["near_knee"] = _limb_end(j["near_hip"], 230, 5)
    j["near_ankle"] = _limb_end(j["near_knee"], 220, 3)
    j["far_hip"] = [562, 915]
    j["far_knee"] = _limb_end(j["far_hip"], 230, -5)
    j["far_ankle"] = _limb_end(j["far_knee"], 220, -3)
    j["neck"] = [526, 490]
    j["pelvis"] = [512, 880]
    return {
        "canvas": [1024, 1536],
        "facing": "right",
        "view": "three-quarter",
        "heads_tall": 4.1,
        "head": [393, 170, 663, 470],
        "neck_box": [491, 455, 561, 505],
        "torso": [398, 500, 626, 850],
        "pelvis_box": [404, 820, 620, 935],
        "upper_arm_width": 70,
        "forearm_width": 62,
        "hand_radius": 40,
        "thigh_width": 92,
        "shin_width": 78,
        "foot_size": [130, 56],
        "feet_line_y": 1413,
        "joints": j,
    }


def _hand_center(elbow, wrist, r):
    dx, dy = wrist[0] - elbow[0], wrist[1] - elbow[1]
    n = math.hypot(dx, dy)
    return wrist[0] + dx / n * r * 0.9, wrist[1] + dy / n * r * 0.9


def make_character(path: Path, s: dict):
    j = s["joints"]
    c = Canvas(tuple(s["canvas"]))
    fw, fh = s["foot_size"]

    def arm(side, color):
        c.capsule(j[f"{side}_shoulder"], j[f"{side}_elbow"], s["upper_arm_width"], color)
        c.capsule(j[f"{side}_elbow"], j[f"{side}_wrist"], s["forearm_width"], color)
        hx, hy = _hand_center(j[f"{side}_elbow"], j[f"{side}_wrist"], s["hand_radius"])
        c.circle(hx, hy, s["hand_radius"], color)

    def leg(side, color):
        c.capsule(j[f"{side}_hip"], j[f"{side}_knee"], s["thigh_width"], color)
        c.capsule(j[f"{side}_knee"], j[f"{side}_ankle"], s["shin_width"], color)
        ax, ay = j[f"{side}_ankle"]
        # Feet point right: the direction the character faces.
        c.ellipse((ax - fw * 0.3, ay - 7, ax + fw * 0.7, ay - 7 + fh), color)

    arm("far", FAR)
    leg("far", FAR)
    c.rect(s["pelvis_box"], NEAR, radius=45)
    c.rect(s["torso"], NEAR, radius=60)
    c.rect(s["neck_box"], SKIN, radius=20)
    leg("near", NEAR)
    arm("near", NEAR)
    c.ellipse(s["head"], SKIN)
    c.save(path)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    make_carriage_exterior(OUT / "carriage_exterior.png")
    make_carriage_interior(OUT / "carriage_interior.png")
    make_locomotive(OUT / "locomotive_exterior.png")
    char = character_spec()
    make_character(OUT / "character_pose.png", char)
    meta = {
        "note": "Pixel coordinates in template space. Generated by tools/make_templates.py; do not edit by hand.",
        "carriage": CARRIAGE,
        "carriage_interior": INTERIOR,
        "locomotive": LOCOMOTIVE,
        "character": char,
    }
    (OUT / "templates.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    for p in sorted(OUT.iterdir()):
        print(f"wrote {p.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
