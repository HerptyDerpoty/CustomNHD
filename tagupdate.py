#!/usr/bin/env python3
"""
Refresh ComicInfo.xml in existing CBZ files by fetching current gallery metadata.
Usage:
  python refresh_metadata.py [--normal] [--favorites] [--dry-run] [--dir PATH] [--limit N]
  If neither --normal nor --favorites is given, both are processed (if defined in config).
  --limit N: only process the N newest galleries (by highest gallery ID).
"""

import os
import sys
import json
import time
import zipfile
import shutil
import argparse
import requests
import xml.sax.saxutils
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
from tqdm import tqdm

# ========== CONSTANTS ==========
CONFIG_FILE = "nh_downloader_config.json"
USER_AGENT = "CustomNHD/1.0 (https://github.com/HerptyDerpoty/CustomNHD)"
# ===================================

# ---------- Load config ----------
def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Config file '{CONFIG_FILE}' not found.")
        print("Please ensure it exists (same as nh_downloader_config.json).")
        sys.exit(1)
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

# ---------- Rate Limiter ----------
class RateLimiter:
    def __init__(self, min_interval=1.0):
        self.min_interval = min_interval
        self.last_request_time = 0

    def wait_if_needed(self):
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()

general_limiter = RateLimiter(min_interval=1.5)  # 45/min

# ---------- Retry helper ----------
def request_with_retry(method, url, headers=None, max_retries=3, limiter=None):
    if limiter:
        limiter.wait_if_needed()
    headers = headers or {}
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get('Retry-After', 60))
                print(f"  ⚠️ Rate limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f"  ⚠️ Request failed: {e}. Retrying in {wait}s...")
            time.sleep(wait)
    raise Exception("Max retries exceeded")

# ---------- Helper to extract gallery ID from folder name ----------
def extract_id_from_name(name):
    import re
    match = re.match(r'^(\d+)', name)
    return int(match.group(1)) if match else None

# ---------- Fetch current gallery metadata ----------
def fetch_gallery_details(gallery_id, api_key=None):
    url = f"https://nhentai.net/api/v2/galleries/{gallery_id}"
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Key {api_key}"
    resp = request_with_retry('GET', url, headers=headers, limiter=general_limiter)
    return resp.json()

# ---------- Read existing ComicInfo.xml from CBZ ----------
def read_comicinfo_from_cbz(cbz_path):
    import tempfile
    temp_dir = Path(tempfile.mkdtemp())
    try:
        with zipfile.ZipFile(cbz_path, 'r') as zin:
            zin.extractall(temp_dir)
        xml_path = temp_dir / "ComicInfo.xml"
        if not xml_path.exists():
            return None, None
        with open(xml_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return content, None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

# ---------- Update ComicInfo.xml inside CBZ (atomic replace) ----------
def update_comicinfo_in_cbz(cbz_path, new_xml_content, dry_run=False):
    if dry_run:
        return True
    import tempfile
    import uuid
    temp_dir = Path(tempfile.mkdtemp())
    try:
        # Extract existing CBZ content
        with zipfile.ZipFile(cbz_path, 'r') as zin:
            zin.extractall(temp_dir)
        # Replace ComicInfo.xml
        xml_path = temp_dir / "ComicInfo.xml"
        with open(xml_path, 'w', encoding='utf-8') as f:
            f.write(new_xml_content)
        # Build new CBZ in a temporary file with a short name
        temp_filename = f".tmp_{uuid.uuid4().hex[:12]}.cbz"
        temp_cbz = cbz_path.parent / temp_filename
        with zipfile.ZipFile(temp_cbz, 'w', zipfile.ZIP_DEFLATED) as zout:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_path, temp_dir)
                    zout.write(full_path, arcname)
        # Atomic replace
        os.replace(temp_cbz, cbz_path)
        return True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        # In case the temporary file wasn't replaced (e.g., exception after creation), clean it up
        if 'temp_cbz' in locals() and temp_cbz.exists():
            try:
                temp_cbz.unlink()
            except OSError:
                pass

# ---------- Create ComicInfo.xml from gallery data ----------
def create_comic_info_xml_from_gallery(gallery_data, add_upload_date=True):
    gallery_id = gallery_data['id']
    title_data = gallery_data.get('title', {})
    original_title = title_data.get('english') or title_data.get('pretty') or f"Gallery {gallery_id}"
    pages = gallery_data.get('num_pages', 0)
    web_url = f"https://nhentai.net/g/{gallery_id}"
    tags = gallery_data.get('tags', [])

    # Group tags by type
    tags_by_type = {}
    for tag in tags:
        ttype = tag['type']
        name = tag['name']
        tags_by_type.setdefault(ttype, []).append(name)

    # Map artist -> Writer, group -> Publisher, category -> Genre
    writer_names = tags_by_type.pop('artist', [])
    publisher_names = tags_by_type.pop('group', [])
    genre_names = tags_by_type.pop('category', [])
    other_tags = []
    for ttype, names in tags_by_type.items():
        capitalized_type = ttype.capitalize()
        for name in names:
            capitalized_name = ' '.join(word.capitalize() for word in name.split())
            other_tags.append(f"{capitalized_type}: {capitalized_name}")

    # Capitalize writer/publisher/genre names
    writer_names = [' '.join(w.capitalize() for w in name.split()) for name in writer_names]
    publisher_names = [' '.join(w.capitalize() for w in name.split()) for name in publisher_names]
    genre_names = [' '.join(w.capitalize() for w in name.split()) for name in genre_names]

    # Build XML
    xml_parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
        f'    <Title>{xml.sax.saxutils.escape(f"{gallery_id} {original_title}")}</Title>',
        f'    <Series>{xml.sax.saxutils.escape(f"{gallery_id} {original_title}")}</Series>',
        f'    <PageCount>{pages}</PageCount>'
    ]

    if add_upload_date and 'upload_date' in gallery_data:
        upload_ts = gallery_data['upload_date']
        dt = datetime.fromtimestamp(upload_ts)
        xml_parts.append(f'    <Year>{dt.year}</Year>')
        xml_parts.append(f'    <Month>{dt.month}</Month>')
        xml_parts.append(f'    <Day>{dt.day}</Day>')

    genre_str = ", ".join(genre_names) if genre_names else "Doujinshi"
    xml_parts.append(f'    <Genre>{xml.sax.saxutils.escape(genre_str)}</Genre>')
    xml_parts.append(f'    <Web>{xml.sax.saxutils.escape(web_url)}</Web>')
    xml_parts.append(f'    <GalleryId>{gallery_id}</GalleryId>')

    if writer_names:
        xml_parts.append(f'    <Writer>{xml.sax.saxutils.escape(", ".join(writer_names))}</Writer>')
    if publisher_names:
        xml_parts.append(f'    <Publisher>{xml.sax.saxutils.escape(", ".join(publisher_names))}</Publisher>')
    if other_tags:
        xml_parts.append(f'    <Tags>{xml.sax.saxutils.escape(", ".join(other_tags))}</Tags>')

    xml_parts.append('</ComicInfo>')
    return '\n'.join(xml_parts)

# ---------- Extract tag set from ComicInfo.xml ----------
def get_tag_set_from_xml(xml_content):
    """Return a set of tag strings (e.g., 'Tag: Big Breasts') from the <Tags> element."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return set()
    tags_elem = root.find('Tags')
    if tags_elem is None or not tags_elem.text:
        return set()
    tags = [t.strip() for t in tags_elem.text.split(',')]
    return set(tags)

# ---------- Compare metadata fields ----------
def compare_metadata(old_root, new_root):
    fields = ['Title', 'Series', 'PageCount', 'Year', 'Month', 'Day',
              'Genre', 'Web', 'GalleryId', 'Writer', 'Publisher']
    changes = []
    for field in fields:
        old_elem = old_root.find(field)
        new_elem = new_root.find(field)
        old_val = old_elem.text.strip() if old_elem is not None else None
        new_val = new_elem.text.strip() if new_elem is not None else None
        if old_val != new_val:
            changes.append((field, old_val, new_val))
    return changes

# ---------- Main refresh logic ----------
def refresh_cbz(cbz_path, api_key, dry_run=False):
    print(f"Processing: {cbz_path}")

    folder = cbz_path.parent
    gid = extract_id_from_name(folder.name)
    if not gid:
        gid = extract_id_from_name(cbz_path.stem)
    if not gid:
        print("  Could not extract gallery ID, skipping.\n")
        return False

    print(f"  Gallery ID: {gid}")
    try:
        gallery_data = fetch_gallery_details(gid, api_key)
    except Exception as e:
        print(f"  Failed to fetch gallery details: {e}\n")
        return False

    new_xml = create_comic_info_xml_from_gallery(gallery_data, add_upload_date=True)
    try:
        new_root = ET.fromstring(new_xml)
    except ET.ParseError:
        new_root = None

    old_xml, _ = read_comicinfo_from_cbz(cbz_path)
    if old_xml is None:
        print("  No ComicInfo.xml found, creating new one.")
        if dry_run:
            print("  [DRY RUN] Would add ComicInfo.xml\n")
            return True
        success = update_comicinfo_in_cbz(cbz_path, new_xml, dry_run)
        print("  ✅ Updated ComicInfo.xml\n" if success else "  ❌ Update failed\n")
        return success

    try:
        old_root = ET.fromstring(old_xml)
    except ET.ParseError:
        old_root = None

    old_tags = get_tag_set_from_xml(old_xml)
    new_tags = get_tag_set_from_xml(new_xml)
    added = new_tags - old_tags
    removed = old_tags - new_tags

    metadata_changes = []
    if old_root is not None and new_root is not None:
        metadata_changes = compare_metadata(old_root, new_root)

    if not added and not removed and not metadata_changes and old_xml == new_xml:
        print("  No changes needed.\n")
        return True

    if added:
        print(f"  + Added tags: {', '.join(sorted(added))}")
    if removed:
        print(f"  - Removed tags: {', '.join(sorted(removed))}")
    if metadata_changes:
        for field, old_val, new_val in metadata_changes:
            old_display = old_val if old_val is not None else '(none)'
            new_display = new_val if new_val is not None else '(none)'
            print(f"  * {field}: '{old_display}' → '{new_display}'")

    if dry_run:
        print("  [DRY RUN] Would update ComicInfo.xml\n")
        return True
    print("  Updating ComicInfo.xml...")
    success = update_comicinfo_in_cbz(cbz_path, new_xml, dry_run)
    print("  ✅ Updated ComicInfo.xml\n" if success else "  ❌ Update failed\n")
    return success

# ---------- Main ----------
def main():
    parser = argparse.ArgumentParser(description="Refresh ComicInfo.xml in existing CBZ files")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying")
    parser.add_argument("--normal", action="store_true", help="Scan only normal download directory")
    parser.add_argument("--favorites", action="store_true", help="Scan only favorites download directory")
    parser.add_argument("--dir", help="Specific directory to scan (overrides config and mode flags)")
    parser.add_argument("--limit", type=int, metavar="N", help="Process only the N newest galleries (by highest ID)")
    args = parser.parse_args()

    config = load_config()
    api_key = config.get("api_key")
    if not api_key:
        print("❌ API key required. Add 'api_key' to config file.")
        sys.exit(1)

    if args.dir:
        root_dirs = [Path(args.dir).resolve()]
    else:
        normal_dir = config.get("download_dir")
        fav_dir = config.get("favorites_download_dir")
        root_dirs = set()
        if args.normal and normal_dir:
            root_dirs.add(Path(normal_dir).resolve())
        if args.favorites and fav_dir:
            root_dirs.add(Path(fav_dir).resolve())
        if not args.normal and not args.favorites:
            if normal_dir:
                root_dirs.add(Path(normal_dir).resolve())
            if fav_dir:
                root_dirs.add(Path(fav_dir).resolve())
        if not root_dirs:
            print("❌ No valid directories to scan. Check config and mode flags.")
            sys.exit(1)

    # Collect all CBZ files with their gallery IDs
    id_cbz_pairs = []  # list of (gallery_id, cbz_path)
    for rd in root_dirs:
        if not rd.exists():
            print(f"⚠️ Directory {rd} does not exist, skipping.")
            continue
        for cbz in rd.rglob("*.cbz"):
            # Extract ID from parent folder name (preferred) or filename
            folder = cbz.parent
            gid = extract_id_from_name(folder.name)
            if not gid:
                gid = extract_id_from_name(cbz.stem)
            if gid:
                id_cbz_pairs.append((gid, cbz.resolve()))

    # Remove duplicates (same CBZ might be found in multiple root dirs)
    unique = {}
    for gid, cbz in id_cbz_pairs:
        if cbz not in unique:
            unique[cbz] = gid
    id_cbz_pairs = [(gid, cbz) for cbz, gid in unique.items()]

    if args.limit:
        # Sort by ID descending, take first N
        id_cbz_pairs.sort(key=lambda x: x[0], reverse=True)
        id_cbz_pairs = id_cbz_pairs[:args.limit]

    # Sort remaining by ID ascending for stable ordering (optional)
    id_cbz_pairs.sort(key=lambda x: x[0])
    all_cbz = [cbz for (_, cbz) in id_cbz_pairs]

    print(f"Found {len(all_cbz)} CBZ files.")
    if args.limit:
        print(f"Limit set to {args.limit} – processing only the newest {min(args.limit, len(all_cbz))} galleries.")
    if args.dry_run:
        print("DRY RUN – no files will be modified.\n")
    else:
        print("Modifications will be applied.\n")

    for i, cbz in enumerate(all_cbz, 1):
        # Pretty relative path for logging
        try:
            rel_path = cbz.relative_to(cbz.parent.parent) if cbz.parent.parent != cbz.parent else cbz.name
        except ValueError:
            rel_path = cbz.name
        print(f"[{i}/{len(all_cbz)}] {rel_path}")
        refresh_cbz(cbz, api_key, args.dry_run)

    print("✨ Done.")

if __name__ == "__main__":
    main()