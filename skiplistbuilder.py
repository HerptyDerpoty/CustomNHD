#!/usr/bin/env python3
"""
Rebuild skip lists from existing gallery folders.

Reads nh_downloader_config.json to get:
  - download_dir        -> used for normal mode skip list (already_downloaded.json)
  - favorites_download_dir -> used for favorites mode skip list (favorites_downloaded.json)

Scans each directory for subfolders (or .cbz files) and extracts numeric gallery IDs
from folder/file names (e.g., "12345 Title" or "[12345] Title").

Usage:
  python rebuild_skip_lists.py [--normal-dir PATH] [--favorites-dir PATH] [--dry-run]

If --normal-dir or --favorites-dir are given, they override the config values.
If a directory is missing or empty, its corresponding skip file is left untouched.
"""

import os
import sys
import json
import re
import argparse
from pathlib import Path

def load_config():
    config_file = "nh_downloader_config.json"
    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            return json.load(f)
    return {}

def extract_id_from_name(name):
    """Extract numeric ID from name like '[12345] Title' or '12345 Title'."""
    # Pattern 1: [12345]...
    match = re.match(r'^\[(\d+)\]', name)
    if match:
        return match.group(1)
    # Pattern 2: 12345...
    match = re.match(r'^(\d+)', name)
    if match:
        return match.group(1)
    return None

def scan_directory(dir_path, dry_run=False, name="skip list"):
    """Scan top‑level folders and .cbz files, return set of IDs."""
    if not dir_path or not os.path.exists(dir_path):
        print(f"⚠️ Directory '{dir_path}' not found – skipping {name}")
        return set()
    ids = set()
    for item in Path(dir_path).iterdir():
        if item.is_dir():
            gid = extract_id_from_name(item.name)
            if gid:
                ids.add(int(gid))
                if not dry_run:
                    print(f"  Found ID {gid} from folder: {item.name}")
        elif item.is_file() and item.suffix.lower() == '.cbz':
            gid = extract_id_from_name(item.stem)
            if gid:
                ids.add(int(gid))
                if not dry_run:
                    print(f"  Found ID {gid} from CBZ: {item.name}")
    return ids

def write_skip_file(output_file, ids, dry_run=False):
    if not ids:
        print(f"  No IDs found – not writing {output_file}")
        return
    sorted_ids = sorted(ids)
    if dry_run:
        print(f"  DRY RUN: Would write {len(sorted_ids)} IDs to {output_file}")
    else:
        with open(output_file, 'w') as f:
            json.dump(sorted_ids, f, indent=2)
        print(f"  ✅ Written {len(sorted_ids)} IDs to {output_file}")

def main():
    parser = argparse.ArgumentParser(description="Rebuild skip lists from existing galleries")
    parser.add_argument("--normal-dir", help="Override download_dir (normal mode galleries)")
    parser.add_argument("--favorites-dir", help="Override favorites_download_dir")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    args = parser.parse_args()

    config = load_config()
    normal_dir = args.normal_dir or config.get("download_dir", "./downloads")
    favorites_dir = args.favorites_dir or config.get("favorites_download_dir", "./favorites")

    print("🔧 Rebuilding skip lists")
    if args.dry_run:
        print("DRY RUN – no files will be written\n")
    else:
        print("Modifications will be applied\n")

    # Normal skip list
    print(f"Scanning normal gallery directory: {normal_dir}")
    normal_ids = scan_directory(normal_dir, args.dry_run, name="normal skip list")
    write_skip_file("already_downloaded.json", normal_ids, args.dry_run)

    # Favorites skip list
    print(f"\nScanning favorites directory: {favorites_dir}")
    fav_ids = scan_directory(favorites_dir, args.dry_run, name="favorites skip list")
    write_skip_file("favorites_downloaded.json", fav_ids, args.dry_run)

    print("\n✨ Done.")

if __name__ == "__main__":
    main()