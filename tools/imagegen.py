#!/usr/bin/env python3
"""Generate game art through an OpenAI-compatible Images API, safely.

Usage:
    python3 tools/imagegen.py prompts/batch-001-style-explore.json --dry-run
    python3 tools/imagegen.py prompts/batch-001-style-explore.json
    python3 tools/imagegen.py prompts/batch-002-rolling-stock.json --only carriage-living --n 1

Safety:
  * The API key is read ONLY from the OPENAI_API_KEY environment variable. It is
    never printed, logged or written to disk, and any echo of it in an error
    message is redacted.
  * The key is only ever sent over HTTPS (plain HTTP is allowed for localhost
    testing only).
  * Edit jobs may only upload files that live inside this repository.
  * Budget guard: refuses runs above max_images_per_run and stops once the
    ledger (art/ledger.jsonl, committed to git so it survives across sessions)
    would pass max_images_total. Limits live in tools/imagegen.config.json.
  * --dry-run prints exactly what would be sent, makes no network calls and
    needs no key.

Environment:
    OPENAI_API_KEY   required for real runs
    OPENAI_BASE_URL  optional, for an OpenAI-compatible provider (default: official API)
    IMAGEGEN_MODEL   optional model override (default from config)

Every image is saved with a sidecar .json holding the full prompt and parameters,
so any asset can be regenerated or iterated on later.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "tools" / "imagegen.config.json"
STYLE_PATH = ROOT / "prompts" / "style.json"

RETRY_STATUS = {429, 500, 502, 503, 504}
KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


class FatalError(RuntimeError):
    """An error that makes the rest of the run pointless (auth, network policy)."""


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"missing file: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON in {path}: {e}")


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else ROOT / p


def repo_file(path_str: str) -> Path:
    """Resolve an input image path and make sure it stays inside the repo."""
    p = (ROOT / path_str).resolve()
    if ROOT not in p.parents:
        sys.exit(f"refusing to upload {path_str!r}: input files must live inside the repository")
    return p


def redact(text: str, key: str | None) -> str:
    if key:
        text = text.replace(key, "***")
    return KEY_PATTERN.sub("sk-***", text)


def check_base_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme == "https":
        return
    if u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"):
        return
    sys.exit(f"refusing base URL {url!r}: the API key may only be sent over HTTPS")


def build_jobs(spec: dict, style_lib: dict, n_override: int | None, only: list[str] | None) -> list[dict]:
    defaults = spec.get("defaults", {})
    subjects = spec.get("subjects", {})
    jobs = []
    for raw in spec.get("jobs", []):
        job = {**defaults, **raw}
        job["params"] = {**defaults.get("params", {}), **raw.get("params", {})}
        jid = job.get("id") or sys.exit("every job needs an id")
        if only and not any(pat in jid or fnmatch.fnmatch(jid, pat) for pat in only):
            continue
        subject = job.get("subject") or subjects.get(job.get("subject_ref", ""))
        if not subject:
            sys.exit(f"job {jid}: no subject (or unknown subject_ref)")
        style_key = job.get("style")
        if style_key not in style_lib["styles"]:
            sys.exit(f"job {jid}: unknown style {style_key!r}; known: {', '.join(style_lib['styles'])}")
        parts = [subject.strip(), style_lib["styles"][style_key]]
        for b in job.get("blocks", []):
            if b not in style_lib["blocks"]:
                sys.exit(f"job {jid}: unknown block {b!r}")
            parts.append(style_lib["blocks"][b])
        if job.get("extra_prompt"):
            parts.append(job["extra_prompt"].strip())
        job["prompt"] = "\n\n".join(parts)
        job["mode"] = job.get("mode", "generate")
        if job["mode"] not in ("generate", "edit"):
            sys.exit(f"job {jid}: mode must be 'generate' or 'edit'")
        if job["mode"] == "edit" and not job.get("images"):
            sys.exit(f"job {jid}: edit jobs need at least one input image")
        job["n"] = int(n_override or job.get("n", 1))
        if job["n"] < 1:
            sys.exit(f"job {jid}: n must be >= 1")
        jobs.append(job)
    return jobs


def ledger_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def form_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def call_api(session: requests.Session, base_url: str, key: str, model: str, job: dict,
             timeout: int, max_retries: int) -> dict:
    headers = {"Authorization": f"Bearer {key}"}  # per request only, never on the session
    fields = {"model": model, "prompt": job["prompt"], "n": job["n"]}
    for k in ("size", "quality"):
        if job.get(k):
            fields[k] = job[k]
    fields.update(job["params"])

    for attempt in range(max_retries + 1):
        handles = []
        try:
            if job["mode"] == "generate":
                resp = session.post(f"{base_url}/images/generations", headers=headers,
                                    json=fields, timeout=timeout)
            else:
                images = job["images"]
                field_name = "image[]" if len(images) > 1 else "image"
                files = []
                for p in images:
                    fh = open(repo_file(p), "rb")
                    handles.append(fh)
                    files.append((field_name, (Path(p).name, fh, mimetypes.guess_type(p)[0] or "image/png")))
                if job.get("mask"):
                    fh = open(repo_file(job["mask"]), "rb")
                    handles.append(fh)
                    files.append(("mask", (Path(job["mask"]).name, fh, "image/png")))
                data = {k: form_value(v) for k, v in fields.items()}
                resp = session.post(f"{base_url}/images/edits", headers=headers,
                                    data=data, files=files, timeout=timeout)
        except requests.exceptions.ProxyError as e:
            host = urlparse(base_url).hostname
            raise FatalError(
                f"the network policy blocked {host} ({redact(str(e), key)[:300]}). "
                f"Add {host} to Allowed domains in the cloud environment's Network access settings."
            )
        except requests.RequestException as e:
            err = redact(str(e), key)[:300]
            if attempt < max_retries:
                wait = 5 * 3 ** attempt
                print(f"  network error ({err}); retrying in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"network error: {err}")
        finally:
            for fh in handles:
                fh.close()

        if resp.status_code == 200:
            return resp.json()
        body = redact(resp.text[:2000], key)
        if resp.status_code in (401, 403):
            raise FatalError(f"HTTP {resp.status_code} (key invalid, revoked or lacking permission): {body}")
        if resp.status_code in RETRY_STATUS and attempt < max_retries:
            wait = 5 * 3 ** attempt
            retry_after = resp.headers.get("retry-after", "")
            if retry_after.isdigit():
                wait = max(wait, int(retry_after))
            print(f"  HTTP {resp.status_code}; retrying in {wait}s", flush=True)
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    raise RuntimeError("unreachable")


def save_images(result: dict, job: dict, batch: str, out_root: Path, ledger_path: Path,
                model: str, session: requests.Session) -> list[Path]:
    items = result.get("data") or []
    if not items:
        raise RuntimeError(f"API returned no images: {json.dumps(result)[:500]}")
    out_dir = out_root / batch
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ext = job["params"].get("output_format", "png")
    saved = []
    for k, item in enumerate(items, 1):
        if item.get("b64_json"):
            blob = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            r = session.get(item["url"], timeout=120)  # no auth header sent to download hosts
            r.raise_for_status()
            blob = r.content
        else:
            raise RuntimeError("image item has neither b64_json nor url")
        path = out_dir / f"{job['id']}__{stamp}_{k}.{ext}"
        path.write_bytes(blob)
        meta = {
            "file": rel(path), "batch": batch, "job": job["id"], "index": k,
            "model": model, "mode": job["mode"], "size": job.get("size"),
            "quality": job.get("quality"), "params": job["params"],
            "inputs": job.get("images", []), "mask": job.get("mask"),
            "style": job.get("style"), "blocks": job.get("blocks", []),
            "prompt": job["prompt"], "revised_prompt": item.get("revised_prompt"),
            "usage_for_request": result.get("usage"), "created_utc": stamp,
        }
        path.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                                             encoding="utf-8")
        entry = {"utc": stamp, "file": rel(path), "batch": batch, "job": job["id"], "model": model,
                 "size": job.get("size"), "quality": job.get("quality"),
                 # usage covers the whole request; record it once so totals can be summed
                 "usage": result.get("usage") if k == 1 else None}
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        saved.append(path)
    return saved


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate game art from a batch spec.")
    ap.add_argument("batch_file", help="e.g. prompts/batch-001-style-explore.json")
    ap.add_argument("--dry-run", action="store_true", help="print requests, send nothing")
    ap.add_argument("--only", action="append", help="only jobs whose id contains/matches this (repeatable)")
    ap.add_argument("--n", type=int, help="override images per job (e.g. 1 for a cheap test)")
    ap.add_argument("--allow-large-run", action="store_true", help="permit more than max_images_per_run")
    ap.add_argument("--out-root", default="art/raw", help="output folder (default art/raw)")
    ap.add_argument("--ledger", default="art/ledger.jsonl", help="spend ledger (default art/ledger.jsonl)")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH)
    style_lib = load_json(STYLE_PATH)
    spec = load_json(resolve(args.batch_file))
    batch = spec.get("batch") or Path(args.batch_file).stem
    jobs = build_jobs(spec, style_lib, args.n, args.only)
    if not jobs:
        sys.exit("no jobs selected")

    model = os.environ.get("IMAGEGEN_MODEL") or cfg["model"]
    base_url = (os.environ.get("OPENAI_BASE_URL") or cfg["base_url"]).rstrip("/")
    check_base_url(base_url)
    ledger_path, out_root = resolve(args.ledger), resolve(args.out_root)
    planned = sum(j["n"] for j in jobs)
    used = ledger_count(ledger_path)

    print(f"batch {batch}: {len(jobs)} job(s), {planned} image(s) | model {model} | "
          f"host {urlparse(base_url).hostname} | ledger {used}/{cfg['max_images_total']} used")

    inputs = [p for j in jobs for p in j.get("images", []) + ([j["mask"]] if j.get("mask") else [])]
    for p in inputs:
        repo_file(p)  # exits if a path points outside the repo
    missing = sorted({p for p in inputs if not (ROOT / p).exists()})

    if args.dry_run:
        for j in jobs:
            print(f"\n=== {j['id']} | {j['mode']} x{j['n']} | size {j.get('size')} | "
                  f"quality {j.get('quality')} | params {json.dumps(j['params'])}")
            if j.get("images"):
                print(f"inputs: {', '.join(j['images'])}")
            print(j["prompt"])
        if missing:
            print(f"\nWARNING: missing input files (create them before a real run): {', '.join(missing)}")
        print("\ndry run: nothing was sent")
        return

    if missing:
        sys.exit(f"missing input files: {', '.join(missing)}")
    if planned > cfg["max_images_per_run"] and not args.allow_large_run:
        sys.exit(f"{planned} images exceeds max_images_per_run={cfg['max_images_per_run']}; "
                 f"use --only/--n to shrink the run, or --allow-large-run if intended")
    if used + planned > cfg["max_images_total"]:
        sys.exit(f"total budget cap: {used} used + {planned} planned > max_images_total="
                 f"{cfg['max_images_total']}. Raise it in tools/imagegen.config.json only deliberately.")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("OPENAI_API_KEY is not set. Add it in the cloud environment settings "
                 "(never in code or chat), then start a new session.")

    session = requests.Session()
    failures = 0
    for i, job in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {job['id']}: {job['mode']} x{job['n']} ...", flush=True)
        try:
            result = call_api(session, base_url, key, model, job,
                              cfg["timeout_seconds"], cfg["max_retries"])
            for p in save_images(result, job, batch, out_root, ledger_path, model, session):
                print(f"  saved {rel(p)}", flush=True)
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
