#!/usr/bin/env python3
"""
Resize WKCD viewpoint WebP images (shorter side = 720px), re-upload to Cloudflare
Images, delete old Cloudflare images, and update wkcd_viewpoint_images.json + viewphoto.geojson.

Usage:
    python resize_and_reupload_webp.py [--dry-run] [--skip-resize] [--skip-upload]

Options:
    --dry-run       Show planned actions without modifying files or calling the API
    --skip-resize   Skip local resize (use existing ~webp files as-is)
    --skip-upload   Only resize locally, do not call Cloudflare API
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
WEBP_DIR = REPO_ROOT / "map" / "~webp"
IMAGES_JSON = REPO_ROOT / "map" / "wkcd_viewpoint_images.json"
GEOJSON_PATH = REPO_ROOT / "map" / "viewphoto.geojson"
SHORT_SIDE = 720
WEBP_QUALITY = 85


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def get_cloudflare_credentials() -> tuple[str, str]:
    load_dotenv(ENV_FILE)
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not api_token:
        print(
            "Error: Cloudflare credentials not found.\n"
            f"Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in {ENV_FILE}"
        )
        sys.exit(1)
    return account_id, api_token


def verify_cloudflare_credentials(base_url: str, headers: dict) -> None:
    """Confirm token works against Cloudflare Images (cfat_ tokens fail /user/tokens/verify)."""
    for attempt in range(5):
        response = requests.get(f"{base_url}?per_page=1", headers=headers, timeout=30)
        if response.status_code == 429:
            wait = 2 ** attempt
            print(f"  Rate limited on credential check, waiting {wait}s...")
            time.sleep(wait)
            continue
        data = response.json()
        if response.status_code == 200 and data.get("success"):
            print("Cloudflare Images API credentials verified.")
            return
        errors = data.get("errors") or [{"message": response.text[:200]}]
        print("Error: Cloudflare Images API credential check failed.")
        for error in errors:
            print(f"  {error.get('message', error)}")
        print("Check CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .env")
        print("  Token needs Account -> Cloudflare Images -> Read and Edit")
        sys.exit(1)
    print("Error: Cloudflare credential check rate limited after retries.")
    sys.exit(1)


def request_with_retry(method: str, url: str, headers: dict, **kwargs) -> requests.Response:
    for attempt in range(6):
        response = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 120), **kwargs)
        if response.status_code != 429:
            return response
        wait = min(30, 2 ** attempt)
        print(f"  Rate limited, waiting {wait}s...")
        time.sleep(wait)
    return response


def resize_to_short_side(path: Path, short_side: int = SHORT_SIDE) -> tuple[int, int]:
    with Image.open(path) as img:
        width, height = img.size
        if min(width, height) <= short_side:
            return width, height

        if width <= height:
            new_w = short_side
            new_h = round(height * short_side / width)
        else:
            new_h = short_side
            new_w = round(width * short_side / height)

        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        resized.save(path, "WEBP", quality=WEBP_QUALITY, method=6)
        return new_w, new_h


def load_image_mapping() -> dict[str, str]:
    data = json.loads(IMAGES_JSON.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for entry in data["images"]:
        filename = entry["filename"]
        stem = filename[:-5] if filename.endswith(".webp") else filename
        mapping[stem] = entry["image_id"]
    return mapping


def save_image_mapping(entries: list[dict]) -> None:
    output = {
        "total_images": len(entries),
        "images": sorted(entries, key=lambda e: e["filename"]),
    }
    IMAGES_JSON.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def update_geojson_image_ids(id_by_filename: dict[str, str]) -> int:
    geojson = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    updated = 0
    for feature in geojson["features"]:
        filename = feature["properties"]["filename"]
        new_id = id_by_filename.get(filename)
        if new_id and feature["properties"].get("image_id") != new_id:
            feature["properties"]["image_id"] = new_id
            updated += 1
        elif new_id:
            feature["properties"]["image_id"] = new_id
    GEOJSON_PATH.write_text(json.dumps(geojson, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return updated


def upload_image(base_url: str, headers: dict, path: Path) -> str:
    with path.open("rb") as f:
        response = request_with_retry(
            "POST",
            base_url,
            headers={"Authorization": headers["Authorization"]},
            files={"file": (path.name, f, "image/webp")},
            data={"requireSignedURLs": "false"},
        )
    if response.status_code in (401, 403):
        raise PermissionError(
            "Cloudflare API rejected upload. "
            "Ensure CLOUDFLARE_API_TOKEN has Account → Cloudflare Images → Edit permission."
        )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"Upload failed for {path.name}: {data.get('errors')}")
    return data["result"]["id"]


def delete_image(base_url: str, headers: dict, image_id: str) -> None:
    response = request_with_retry("DELETE", f"{base_url}/{image_id}", headers=headers, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"Delete failed for {image_id}: {data.get('errors')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-resize", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    args = parser.parse_args()

    webp_files = sorted(WEBP_DIR.glob("wkcd_*.webp"))
    if not webp_files:
        print(f"No WebP files found in {WEBP_DIR}")
        sys.exit(1)

    old_ids = load_image_mapping()
    missing = [p.stem for p in webp_files if p.stem not in old_ids]
    if missing:
        print(f"Error: {len(missing)} webp files missing from {IMAGES_JSON.name}: {missing[:5]}")
        sys.exit(1)

    print(f"Processing {len(webp_files)} images (short side -> {SHORT_SIDE}px)")

    resize_stats: list[tuple[str, str, str]] = []
    for path in webp_files:
        with Image.open(path) as img:
            old_size = img.size
        if args.skip_resize:
            new_size = old_size
        elif args.dry_run:
            w, h = old_size
            if min(w, h) <= SHORT_SIDE:
                new_size = old_size
            elif w <= h:
                new_size = (SHORT_SIDE, round(h * SHORT_SIDE / w))
            else:
                new_size = (round(w * SHORT_SIDE / h), SHORT_SIDE)
        else:
            new_size = resize_to_short_side(path)
        resize_stats.append((path.name, f"{old_size[0]}x{old_size[1]}", f"{new_size[0]}x{new_size[1]}"))

    print("\nResize preview (first 5 / last 2):")
    for row in resize_stats[:5] + resize_stats[-2:]:
        print(f"  {row[0]}: {row[1]} -> {row[2]}")

    if args.skip_upload:
        print("\n--skip-upload set; local resize complete.")
        return

    account_id, api_token = get_cloudflare_credentials()
    base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/images/v1"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    verify_cloudflare_credentials(base_url, headers)

    new_entries: list[dict] = []
    id_by_filename: dict[str, str] = {}
    failures: list[str] = []

    for idx, path in enumerate(webp_files, start=1):
        stem = path.stem
        old_id = old_ids[stem]
        filename = f"{stem}.webp"
        print(f"[{idx}/{len(webp_files)}] {filename}")

        if args.dry_run:
            print(f"  would upload -> new id, delete old {old_id}")
            new_entries.append({"filename": filename, "image_id": f"NEW-ID-FOR-{stem}"})
            id_by_filename[stem] = f"NEW-ID-FOR-{stem}"
            continue

        try:
            new_id = upload_image(base_url, headers, path)
            print(f"  uploaded: {new_id}")
            delete_image(base_url, headers, old_id)
            print(f"  deleted old: {old_id}")
            new_entries.append({"filename": filename, "image_id": new_id})
            id_by_filename[stem] = new_id
            time.sleep(0.5)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            failures.append(filename)

    if args.dry_run:
        print(f"\nDry run complete for {len(webp_files)} images.")
        return

    if failures:
        print(f"\nFailed on {len(failures)} images: {failures}")
        if new_entries:
            save_image_mapping(new_entries)
            update_geojson_image_ids(id_by_filename)
            print("Partial updates saved for successful uploads.")
        sys.exit(1)

    save_image_mapping(new_entries)
    update_geojson_image_ids(id_by_filename)
    print(f"\nDone. Updated {IMAGES_JSON.name} and {GEOJSON_PATH.name}.")


if __name__ == "__main__":
    main()
