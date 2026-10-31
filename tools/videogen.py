#!/usr/bin/env python3
"""Generate short character animation clips with an OpenAI-compatible Videos API (Sora), safely.

    python3 tools/videogen.py prompts/video-001-anim-test.json --dry-run
    python3 tools/videogen.py prompts/video-001-anim-test.json --only walk

Same safety rules as imagegen.py: the key comes only from OPENAI_API_KEY and is
never printed or stored; HTTPS only; only repository files are uploaded; budget
caps, counted here in seconds of video (art/video_ledger.jsonl, committed).

The reference character is composited onto a flat chroma-key background and
fitted to the exact video size before upload (the API requires the reference
image to match the requested size), so frames can be keyed out afterwards with
tools/sprites.py video.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from imagegen import (CONFIG_PATH, ROOT, RETRY_STATUS, FatalError, check_base_url,  # noqa: E402
                      load_json, redact, rel, repo_file, resolve)
from sprites import key_out  # noqa: E402

DONE_FAILED = {"failed", "cancelled", "canceled", "expired"}


def video_ledger_seconds(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            total += float(json.loads(line).get("seconds", 0))
    return total


def prepare_reference(path: Path, size: str, bg_hex: str, fill: float = 0.78) -> Image.Image:
    """Character centred on a flat colour, feet at 90% height, exact video size."""
    W, H = (int(v) for v in size.lower().split("x"))
    arr = key_out(np.asarray(Image.open(path).convert("RGBA"), dtype=np.float32))
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")
    bbox = img.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    if not bbox:
        sys.exit(f"no character found in {path}")
    crop = img.crop(bbox)
    scale = min(fill * H / crop.height, 0.9 * W / crop.width)
    crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.LANCZOS)
    bg = tuple(int(bg_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    canvas = Image.new("RGBA", (W, H), bg + (255,))
    canvas.alpha_composite(crop, ((W - crop.width) // 2, max(0, round(H * 0.9) - crop.height)))
    return canvas.convert("RGB")


class Client:
    def __init__(self, base_url: str, key: str, retries: int):
        self.base, self.key, self.retries = base_url, key, retries
        self.s = requests.Session()

    def send(self, method: str, path: str, **kw) -> requests.Response:
        headers = {"Authorization": f"Bearer {self.key}"}
        for attempt in range(self.retries + 1):
            try:
                resp = self.s.request(method, f"{self.base}{path}", headers=headers, **kw)
            except requests.exceptions.ProxyError as e:
                host = urlparse(self.base).hostname
                raise FatalError(f"the network policy blocked {host} ({redact(str(e), self.key)[:300]}). "
                                 f"Add {host} to Allowed domains in the environment's Network access settings.")
            except requests.RequestException as e:
                if attempt < self.retries:
                    time.sleep(5 * 3 ** attempt)
                    continue
                raise RuntimeError(f"network error: {redact(str(e), self.key)[:300]}")
            if resp.status_code < 300:
                return resp
            body = redact(resp.text[:2000], self.key)
            if resp.status_code in (401, 403):
                raise FatalError(f"HTTP {resp.status_code} (key invalid, revoked or without video access): {body}")
            if resp.status_code in RETRY_STATUS and attempt < self.retries:
                time.sleep(5 * 3 ** attempt)
                continue
            raise RuntimeError(f"HTTP {resp.status_code}: {body}")
        raise RuntimeError("unreachable")


def build_jobs(spec: dict, only: list[str] | None) -> list[dict]:
    d = spec.get("defaults", {})
    jobs = []
    for raw in spec.get("jobs", []):
        job = {**d, **raw}
        if only and not any(p in job["id"] for p in only):
            continue
        if "prompt" not in job:
            job["prompt"] = spec["prompt_template"].replace("{action}", job["action"].strip())
        job["seconds"] = int(job.get("seconds", 4))
        jobs.append(job)
    return jobs


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate animation clips from a video batch spec.")
    ap.add_argument("batch_file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", action="append")
    ap.add_argument("--allow-large-run", action="store_true")
    ap.add_argument("--out-root", default="art/raw")
    ap.add_argument("--ledger", default="art/video_ledger.jsonl")
    ap.add_argument("--poll-interval", type=float, help="seconds between status checks (default from config)")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH)
    vcfg = cfg["video"]
    spec = load_json(resolve(args.batch_file))
    batch = spec.get("batch") or Path(args.batch_file).stem
    jobs = build_jobs(spec, args.only)
    if not jobs:
        sys.exit("no jobs selected")
    for j in jobs:
        j.setdefault("model", vcfg["model"])
    base_url = (os.environ.get("OPENAI_BASE_URL") or cfg["base_url"]).rstrip("/")
    check_base_url(base_url)
    ledger, out_root = resolve(args.ledger), resolve(args.out_root)
    planned = sum(j["seconds"] for j in jobs)
    used = video_ledger_seconds(ledger)
    print(f"batch {batch}: {len(jobs)} clip(s), {planned}s of video | host {urlparse(base_url).hostname} | "
          f"video ledger {used:g}/{vcfg['max_seconds_total']}s used")
    refs = sorted({j["reference"] for j in jobs if j.get("reference")})
    for r in refs:
        repo_file(r)
    missing = [r for r in refs if not (ROOT / r).exists()]

    if args.dry_run:
        for j in jobs:
            print(f"\n=== {j['id']} | {j['model']} | {j['size']} | {j['seconds']}s | "
                  f"reference {j.get('reference')} on {j.get('background')}\n{j['prompt']}")
        if missing:
            print(f"\nWARNING: missing reference image(s): {', '.join(missing)}")
        print("\ndry run: nothing was sent")
        return
    if missing:
        sys.exit(f"missing reference image(s): {', '.join(missing)}")
    if planned > vcfg["max_seconds_per_run"] and not args.allow_large_run:
        sys.exit(f"{planned}s exceeds max_seconds_per_run={vcfg['max_seconds_per_run']}")
    if used + planned > vcfg["max_seconds_total"]:
        sys.exit(f"video budget cap: {used:g}s used + {planned}s planned > {vcfg['max_seconds_total']}s")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("OPENAI_API_KEY is not set. Add it in the cloud environment settings, then start a new session.")

    client = Client(base_url, key, cfg["max_retries"])
    out_dir = out_root / batch
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for i, job in enumerate(jobs, 1):
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = out_dir / f"{job['id']}__{stamp}"
        print(f"[{i}/{len(jobs)}] {job['id']}: {job['seconds']}s ...", flush=True)
        try:
            data = {"model": job["model"], "prompt": job["prompt"], "size": job["size"],
                    "seconds": str(job["seconds"])}
            if job.get("reference"):
                ref = prepare_reference(repo_file(job["reference"]), job["size"], job.get("background", "#00FF00"))
                ref_path = stem.with_name(stem.name + "_ref.png")
                ref.save(ref_path)
                buf = io.BytesIO()
                ref.save(buf, "PNG")
                created = client.send("POST", "/videos", data=data, timeout=120,
                                      files={"input_reference": ("reference.png", buf.getvalue(), "image/png")}).json()
            else:
                ref_path = None
                created = client.send("POST", "/videos", json=data, timeout=120).json()
            vid = created.get("id")
            if not vid:
                raise RuntimeError(f"no video id in response: {redact(json.dumps(created), key)[:300]}")
            t0, status = time.time(), created
            while status.get("status") != "completed":
                if status.get("status") in DONE_FAILED:
                    raise RuntimeError(f"video {status.get('status')}: "
                                       f"{redact(json.dumps(status.get('error') or status), key)[:500]}")
                if time.time() - t0 > vcfg["max_wait_seconds"]:
                    raise RuntimeError(f"timed out after {vcfg['max_wait_seconds']}s (video id {vid})")
                time.sleep(args.poll_interval or vcfg["poll_interval_seconds"])
                status = client.send("GET", f"/videos/{vid}", timeout=60).json()
                print(f"  {status.get('status')} {status.get('progress', '')}", flush=True)
            mp4 = stem.with_suffix(".mp4")
            mp4.write_bytes(client.send("GET", f"/videos/{vid}/content", timeout=300).content)
            meta = {"file": rel(mp4), "batch": batch, "job": job["id"], "video_id": vid,
                    "model": job["model"], "size": job["size"], "seconds": job["seconds"],
                    "reference": job.get("reference"), "prepared_reference": rel(ref_path) if ref_path else None,
                    "background": job.get("background"), "prompt": job["prompt"], "created_utc": stamp}
            mp4.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                                                encoding="utf-8")
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"utc": stamp, "file": rel(mp4), "batch": batch, "job": job["id"],
                                    "model": job["model"], "seconds": job["seconds"], "video_id": vid}) + "\n")
            print(f"  saved {rel(mp4)}", flush=True)
        except FatalError as e:
            print(f"  FAILED: {e}\nstopping the run.", flush=True)
            failures += 1
            break
        except RuntimeError as e:
            print(f"  FAILED: {e}", flush=True)
            failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
