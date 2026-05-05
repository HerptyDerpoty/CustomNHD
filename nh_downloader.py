#!/usr/bin/env python3
"""
nhentai downloader - multi-query search OR download favorites.
Uses the new /galleries/{id}/download endpoint (CBZ) with rate limiting.
Extracts metadata from the included meta.json (or falls back to API).
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
import signal
from pathlib import Path
from datetime import datetime
from tqdm import tqdm

# ================= GRACEFUL EXIT =================
def signal_handler(sig, frame):
    print("\n⚠️ Interrupted by user. Exiting gracefully...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ========== CONSTANTS ==========
DEBUG = False
CONFIG_FILE = "nh_downloader_config.json"
TAG_CACHE_FILE = "tag_cache_full.json"
SKIP_FILE = "already_downloaded.json"
FAVORITES_CACHE_FILE = "favorites_downloaded.json"
USER_AGENT = "CustomNHD/1.0 (https://github.com/HerptyDerpoty/CustomNHD)"
# ===================================

if DEBUG: print("DEBUG: Script started, imports loaded")

# ---------- JSON helpers with backup and atomic write ----------
def load_json_with_backup(file_path):
    """Load JSON from primary; fallback to backup; exit if both corrupt."""
    primary = file_path
    backup = file_path + ".bak"

    def try_load(path):
        if not os.path.exists(path):
            return None
        if os.path.getsize(path) == 0:
            return None
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(data)
            return None
        except (json.JSONDecodeError, OSError):
            return None

    result = try_load(primary)
    if result is not None:
        return result
    result = try_load(backup)
    if result is not None:
        return result
    for path in [primary, backup]:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            print(f"❌ FATAL: {path} is corrupted (invalid JSON). Cannot continue.")
            print("Please manually restore from backup or delete the file to start fresh.")
            sys.exit(1)
    return set()

def save_json_with_backup(data_set, file_path):
    primary = file_path
    backup = file_path + ".bak"
    temp = file_path + ".tmp"
    with open(temp, 'w') as f:
        json.dump(sorted(data_set), f, indent=2)
    os.replace(temp, primary)
    shutil.copy2(primary, backup)

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

search_limiter = RateLimiter(min_interval=3.0)      # search: 20/min → 3s
favorites_limiter = RateLimiter(min_interval=4.0)   # favorites: 15/min → 4s
general_limiter = RateLimiter(min_interval=1.5)     # gallery details (fallback): 45/min → 1.5s
download_limiter_auth = RateLimiter(min_interval=30.0)   # authenticated: 10 per 5 min → 30s
download_limiter_public = RateLimiter(min_interval=60.0) # public: 5 per 5 min → 60s

# ---------- Retry helper ----------
def request_with_retry(method, url, headers=None, json=None, max_retries=3, limiter=None):
    if limiter:
        limiter.wait_if_needed()
    headers = headers or {}
    for attempt in range(max_retries):
        try:
            if method.upper() == 'GET':
                resp = requests.get(url, headers=headers, timeout=30)
            elif method.upper() == 'POST':
                resp = requests.post(url, json=json, headers=headers, timeout=30)
            else:
                raise ValueError("Unsupported method")
            if resp.status_code == 429:
                retry_after = int(resp.headers.get('Retry-After', 60))
                print(f"  ⚠️ Rate limited (429). Waiting {retry_after} seconds...")
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

# ---------- Tag cache ----------
def load_tag_cache():
    if not os.path.exists(TAG_CACHE_FILE):
        print(f"⚠️ Tag cache '{TAG_CACHE_FILE}' not found. Starting empty (will auto-populate).")
        return {}, {}
    with open(TAG_CACHE_FILE, 'r') as f:
        tag_cache = json.load(f)   # tag_id -> {"type": "...", "name": "..."}
    name_to_id = {}
    for tid, info in tag_cache.items():
        key = f"{info['type']}:{info['name'].lower()}"
        name_to_id[key] = int(tid)
    return tag_cache, name_to_id

def save_tag_cache(tag_cache):
    with open(TAG_CACHE_FILE, 'w') as f:
        json.dump(tag_cache, f, indent=2)

TAG_CACHE, NAME_TO_ID = load_tag_cache()

def get_tag_name_by_id(tag_id):
    return TAG_CACHE.get(str(tag_id), {}).get("name")

def update_cache_from_tags(tags):
    """Update tag cache from a list of tag dicts (each with id, type, name)."""
    added = 0
    for tag in tags:
        tid = str(tag['id'])
        if tid not in TAG_CACHE:
            TAG_CACHE[tid] = {"type": tag['type'], "name": tag['name']}
            key = f"{tag['type']}:{tag['name'].lower()}"
            NAME_TO_ID[key] = tag['id']
            added += 1
    if added:
        save_tag_cache(TAG_CACHE)
    return added

# ---------- Skip lists ----------
def load_skip_ids():
    return load_json_with_backup(SKIP_FILE)

def add_to_skip_list(gallery_id):
    global SKIP_IDS
    SKIP_IDS.add(gallery_id)
    save_json_with_backup(SKIP_IDS, SKIP_FILE)

SKIP_IDS = load_skip_ids()

def load_favorites_cache():
    return load_json_with_backup(FAVORITES_CACHE_FILE)

def add_to_favorites_cache(gallery_id):
    global FAVORITES_DOWNLOADED
    FAVORITES_DOWNLOADED.add(gallery_id)
    save_json_with_backup(FAVORITES_DOWNLOADED, FAVORITES_CACHE_FILE)

FAVORITES_DOWNLOADED = load_favorites_cache()

# ---------- Config loading ----------
def load_config():
    if DEBUG: print(f"DEBUG: Looking for config file: {CONFIG_FILE}")
    if DEBUG: print(f"DEBUG: Current working directory: {os.getcwd()}")
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Config file '{CONFIG_FILE}' not found.")
        print("Example config (copy the JSON below):")
        print(json.dumps({
            "queries": ["tag -tag tag", "tag -tag -tag tag"],
            "consecutive_skipped_limit": 100,
            "favorites_consecutive_skipped_limit": 100,
            "download_dir": "./downloads",
            "favorites_download_dir": "./favorites",
            "dry_run": False,
            "stop_at_first": False,
            "add_upload_date": True,
            "api_key": "nhk_..."
        }, indent=2))
        print("\n# Notes:")
        print("# - 'queries' supports search syntax (https://nhentai.net/info). Each query is processed sequentially.")
        print("# - 'consecutive_skipped_limit': stop after N skipped galleries in a row. 0 = unlimited.")
        sys.exit(1)
    if DEBUG: print("DEBUG: Config file found, loading...")
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

# ---------- API calls ----------
def get_auth_headers(api_key=None):
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Key {api_key}"
    return headers

def search_galleries(query, page, sort="date", api_key=None):
    url = f"https://nhentai.net/api/v2/search?query={requests.utils.quote(query)}&page={page}&sort={sort}"
    headers = get_auth_headers(api_key)
    resp = request_with_retry('GET', url, headers=headers, limiter=search_limiter)
    return resp.json()

def get_favorites(page, api_key=None):
    url = f"https://nhentai.net/api/v2/favorites?page={page}"
    headers = get_auth_headers(api_key)
    resp = request_with_retry('GET', url, headers=headers, limiter=favorites_limiter)
    return resp.json()

def get_gallery_details(gallery_id, api_key=None):
    """Fallback endpoint (used only if meta.json is missing in the downloaded CBZ)."""
    url = f"https://nhentai.net/api/v2/galleries/{gallery_id}"
    headers = get_auth_headers(api_key)
    resp = request_with_retry('GET', url, headers=headers, limiter=general_limiter)
    return resp.json()

# ---------- Helper functions for filenames and XML ----------
def safe_filename(text, max_bytes=200):
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode('utf-8', 'ignore')

def capitalize_words(s):
    return ' '.join(word.capitalize() for word in s.split())

def escape_xml(text):
    if text is None:
        return ''
    return xml.sax.saxutils.escape(str(text))

def create_comic_info_xml(gallery_info, api_key=None, year=None, month=None, day=None):
    original_title = gallery_info.get('english_title') or gallery_info.get('japanese_title') or f"Gallery {gallery_info['id']}"
    pages = gallery_info.get('num_pages', 0)
    tag_ids = gallery_info.get('tag_ids', [])
    web_url = f"https://nhentai.net/g/{gallery_info['id']}"
    gallery_id = gallery_info['id']

    title = f"{gallery_id} {original_title}"
    series = title

    tags_by_type = {}
    for tid in tag_ids:
        tinfo = TAG_CACHE.get(str(tid))
        if tinfo:
            ttype = tinfo['type']
            name = tinfo['name']
            tags_by_type.setdefault(ttype, []).append(name)

    writer_names = tags_by_type.pop('artist', [])
    publisher_names = tags_by_type.pop('group', [])
    genre_names = tags_by_type.pop('category', [])
    all_other_tags = []
    for ttype, names in tags_by_type.items():
        capitalized_type = ttype.capitalize()
        for name in names:
            capitalized_name = capitalize_words(name)
            all_other_tags.append(f"{capitalized_type}: {capitalized_name}")

    xml_parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
        f'    <Title>{escape_xml(title)}</Title>',
        f'    <Series>{escape_xml(series)}</Series>',
        f'    <PageCount>{escape_xml(pages)}</PageCount>'
    ]

    if year is not None and month is not None and day is not None:
        xml_parts.append(f'    <Year>{escape_xml(year)}</Year>')
        xml_parts.append(f'    <Month>{escape_xml(month)}</Month>')
        xml_parts.append(f'    <Day>{escape_xml(day)}</Day>')

    genre_str = ", ".join(genre_names) if genre_names else "Doujinshi"
    xml_parts.append(f'    <Genre>{escape_xml(genre_str)}</Genre>')
    xml_parts.append(f'    <Web>{escape_xml(web_url)}</Web>')
    xml_parts.append(f'    <GalleryId>{escape_xml(gallery_id)}</GalleryId>')

    if writer_names:
        xml_parts.append(f'    <Writer>{escape_xml(", ".join(writer_names))}</Writer>')
    if publisher_names:
        xml_parts.append(f'    <Publisher>{escape_xml(", ".join(publisher_names))}</Publisher>')
    if all_other_tags:
        xml_parts.append(f'    <Tags>{escape_xml(", ".join(all_other_tags))}</Tags>')

    xml_parts.append('</ComicInfo>')
    return '\n'.join(xml_parts)

# ---------- Gallery download using the new endpoint ----------
def download_gallery(gallery_id, title, download_dir, dry_run, gallery_listing, api_key=None, add_upload_date=False):
    english_title = gallery_listing.get('english_title')
    if not english_title:
        english_title = str(gallery_id)

    safe_title = safe_filename(english_title, max_bytes=200)
    safe_title = "".join(c for c in safe_title if c.isalnum() or c in (' ', '-', '_')).strip()
    name_base = f"{gallery_id} {safe_title}"

    folder_path = Path(download_dir) / name_base
    cbz_path = folder_path / f"{name_base}.cbz"
    temp_dir = folder_path / "temp"

    if not dry_run:
        folder_path.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

    # Request a signed download URL
    print(f"  Requesting download URL...")
    url = f"https://nhentai.net/api/v2/galleries/{gallery_id}/download?format=cbz"
    headers = get_auth_headers(api_key)
    try:
        # Choose rate limiter based on authentication
        if api_key:
            limiter = download_limiter_auth
        else:
            limiter = download_limiter_public
        limiter.wait_if_needed()
        resp = request_with_retry('POST', url, headers=headers, json=None, limiter=None)
        if resp.status_code != 200:
            print(f"  Failed to get download URL: {resp.status_code}")
            return False
        data = resp.json()
        download_url = data.get('url')
        if not download_url:
            print("  No URL in response")
            return False
        expires_at = data.get('expires_at')
        if expires_at:
            print(f"  URL expires at {datetime.fromtimestamp(expires_at)}")
    except Exception as e:
        print(f"  Error getting download URL: {e}")
        return False

    # Download the CBZ file
    print(f"  Downloading CBZ...")
    try:
        dl_resp = requests.get(download_url, stream=True, timeout=60)
        dl_resp.raise_for_status()
        temp_cbz = temp_dir / "downloaded.cbz"
        with open(temp_cbz, 'wb') as f:
            for chunk in dl_resp.iter_content(8192):
                f.write(chunk)
        size_mb = temp_cbz.stat().st_size / (1024*1024)
        print(f"  Downloaded {size_mb:.1f} MB")
    except Exception as e:
        print(f"  Download failed: {e}")
        return False

    if dry_run:
        print(f"  [DRY RUN] Would create CBZ at {cbz_path}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return True

    # Extract the downloaded CBZ
    extract_dir = temp_dir / "extract"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(temp_cbz, 'r') as zin:
        zin.extractall(extract_dir)

    # Look for meta.json / info.json
    meta_path = None
    for candidate in ['meta.json', 'info.json']:
        p = extract_dir / candidate
        if p.exists():
            meta_path = p
            break

    if meta_path:
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        tags = meta.get('tags', [])
        upload_ts = meta.get('upload_date')
        # Update tag cache
        added = update_cache_from_tags(tags)
        if added:
            print(f"  Added {added} new tags to cache from meta.json")
        year = month = day = None
        if add_upload_date and upload_ts:
            dt = datetime.fromtimestamp(upload_ts)
            year, month, day = dt.year, dt.month, dt.day
            print(f"  Upload date: {year}-{month:02d}-{day:02d}")
        # Build a gallery_listing with correct tag_ids and num_pages
        gallery_listing_with_meta = gallery_listing.copy()
        gallery_listing_with_meta['tag_ids'] = [tag['id'] for tag in tags]
        gallery_listing_with_meta['num_pages'] = meta.get('num_pages', 0)
        xml_content = create_comic_info_xml(gallery_listing_with_meta, api_key, year, month, day)
    else:
        print(f"  meta.json not found, falling back to API call...")
        try:
            full_gallery = get_gallery_details(gallery_id, api_key)
            # Update tag cache from API
            added = update_cache_from_tags(full_gallery.get('tags', []))
            if added:
                print(f"  Added {added} new tags to cache from API fallback")
            upload_ts = full_gallery.get('upload_date') if add_upload_date else None
            year = month = day = None
            if upload_ts:
                dt = datetime.fromtimestamp(upload_ts)
                year, month, day = dt.year, dt.month, dt.day
                print(f"  Upload date: {year}-{month:02d}-{day:02d}")
            xml_content = create_comic_info_xml(gallery_listing, api_key, year, month, day)
        except Exception as e:
            print(f"  Fallback failed: {e}")
            return False

    # Remove any original ComicInfo.xml from the downloaded archive
    orig_xml = extract_dir / "ComicInfo.xml"
    if orig_xml.exists():
        orig_xml.unlink()

    # Write our ComicInfo.xml
    xml_path = extract_dir / "ComicInfo.xml"
    with open(xml_path, 'w', encoding='utf-8') as f:
        f.write(xml_content)

    # Rebuild the CBZ
    with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_DEFLATED) as zout:
        for root, dirs, files in os.walk(extract_dir):
            for file in files:
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, extract_dir)
                zout.write(full_path, arcname)

    shutil.rmtree(temp_dir)
    print(f"  ✅ Created CBZ: {cbz_path}")
    return True

# ---------- Favorites downloader ----------
def download_favorites(config):
    api_key = config.get("api_key")
    if not api_key:
        print("❌ API key required for favorites mode. Add 'api_key' to config file.")
        sys.exit(1)
    download_dir = config.get("favorites_download_dir", "./favorites")
    dry_run = config.get("dry_run", False)
    consecutive_skipped_limit = config.get("favorites_consecutive_skipped_limit", 0)
    add_upload_date = config.get("add_upload_date", False)

    if dry_run:
        print("🚀 DRY RUN")

    print("🔑 Using API key")

    to_download = []  # list of (gallery_id, gallery_listing)

    page = 1
    consecutive_skipped = 0
    while True:
        print(f"📄 Fetching favorites page {page}...")
        try:
            data = get_favorites(page, api_key)
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
            break
        favorites = data.get('result', [])
        if not favorites:
            print("No more favorites found.")
            break

        new_favorites = [fav for fav in favorites if fav['id'] not in FAVORITES_DOWNLOADED]
        skipped_this_page = len(favorites) - len(new_favorites)
        if skipped_this_page > 0:
            consecutive_skipped += skipped_this_page
            print(f"  Skipped {skipped_this_page} already downloaded favorites. Consecutive skipped: {consecutive_skipped}")
        else:
            consecutive_skipped = 0

        if consecutive_skipped_limit > 0 and consecutive_skipped >= consecutive_skipped_limit:
            print(f"  ⏭️ Skipped {consecutive_skipped} already downloaded favorites in a row. Stopping.")
            break

        print(f"  Found {len(favorites)} favorites, {len(new_favorites)} new.")
        for fav in new_favorites:
            to_download.append((fav['id'], fav))
            print(f"  Added ID {fav['id']} to download queue")

        if page >= data.get('num_pages', 0):
            break
        page += 1

    if not to_download:
        print("No new favorites to download.")
        return

    limit_msg = "10 per 5 minutes" if api_key else "5 per 5 minutes"
    print(f"\n📥 Downloading {len(to_download)} new favorites (rate limited to {limit_msg})...")
    total_downloaded = 0
    for idx, (gid, fav) in enumerate(to_download, 1):
        title = fav.get('english_title') or fav.get('japanese_title') or str(gid)
        print(f"\n🎯 [{idx}/{len(to_download)}] {title} (ID: {gid})")
        if download_gallery(gid, title, download_dir, dry_run, fav, api_key, add_upload_date):
            total_downloaded += 1
            add_to_favorites_cache(gid)
        else:
            print(f"❌ Failed {title}")

    print(f"\n✨ Favorites download complete! Downloaded {total_downloaded} new favorites to {download_dir}")

# ---------- Normal query mode ----------
def run_queries(config):
    queries = config.get("queries")
    if not queries or not isinstance(queries, list):
        print("❌ 'queries' must be a non-empty list in config for normal mode.")
        sys.exit(1)
    consecutive_skipped_limit = config.get("consecutive_skipped_limit", 0)
    download_dir = config.get("download_dir", "./downloads")
    dry_run = config.get("dry_run", False)
    stop_at_first = config.get("stop_at_first", False)
    api_key = config.get("api_key", None)
    add_upload_date = config.get("add_upload_date", False)

    if dry_run:
        print("🚀 DRY RUN")
    if api_key:
        print("🔑 Using API key")
    else:
        print("🔓 No API key")

    # Collect galleries to download
    to_download = []  # list of (gallery_id, gallery_listing)

    for qidx, query in enumerate(queries, 1):
        print(f"\n{'='*60}")
        print(f"🔍 Processing query {qidx}/{len(queries)}: {query}")
        print(f"{'='*60}")

        page = 1
        query_found = 0
        consecutive_skipped = 0
        stop_query = False

        # Optional progress bar for the whole query
        if not stop_at_first:
            try:
                data = search_galleries(query, 1, api_key=api_key)
                total_count = data.get('total', 0)
                if total_count is None:
                    total_count = 0
                if total_count:
                    pbar = tqdm(total=total_count, desc=f"Query {qidx}", unit="gal", leave=True)
                else:
                    pbar = None
            except:
                pbar = None
        else:
            pbar = None

        while not stop_query:
            print(f"\n📄 Fetching page {page} for query...")
            try:
                data = search_galleries(query, page, api_key=api_key)
            except Exception as e:
                print(f"Error fetching page {page}: {e}")
                break
            galleries = data.get('result', [])
            if not galleries:
                print("No more galleries found for this query.")
                break

            for gal in galleries:
                gid = gal['id']
                if gid in SKIP_IDS:
                    consecutive_skipped += 1
                    if pbar:
                        pbar.update(1)
                    if consecutive_skipped_limit > 0 and consecutive_skipped >= consecutive_skipped_limit:
                        print(f"  ⏭️ Skipped {consecutive_skipped} already downloaded galleries in a row. Moving to next query.")
                        stop_query = True
                        break
                    continue
                else:
                    consecutive_skipped = 0

                # New gallery found - add to download list
                to_download.append((gid, gal))
                query_found += 1
                if pbar:
                    pbar.update(1)

                if stop_at_first:
                    stop_query = True
                    break

            if stop_query:
                break

            if page >= data.get('num_pages', 0):
                break
            page += 1

        if pbar:
            pbar.close()
        print(f"✅ Query finished. Found {query_found} new galleries to download.")

    if not to_download:
        print("\nNo new galleries to download.")
        return

    limit_msg = "10 per 5 minutes" if api_key else "5 per 5 minutes"
    print(f"\n📥 Downloading {len(to_download)} new galleries (rate limited to {limit_msg})...")
    total_downloaded = 0
    for idx, (gid, gal) in enumerate(to_download, 1):
        title = gal.get('english_title') or gal.get('japanese_title') or str(gid)
        print(f"\n🎯 [{idx}/{len(to_download)}] {title} (ID: {gid})")
        if download_gallery(gid, title, download_dir, dry_run, gal, api_key, add_upload_date):
            total_downloaded += 1
            add_to_skip_list(gid)
        else:
            print(f"❌ Failed {title}")

    print(f"\n✨ All queries done! Downloaded {total_downloaded} CBZ file(s) in {download_dir}")

# ---------- Main ----------
def main():
    if DEBUG: print("DEBUG: Entered main()")
    start_time = datetime.now()
    print(f"🚀 Script started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    parser = argparse.ArgumentParser(description="nhentai downloader")
    parser.add_argument("--favorites", action="store_true", help="Download all favorites (requires API key in config)")
    args = parser.parse_args()
    if DEBUG: print(f"DEBUG: Arguments parsed: {args}")

    config = load_config()
    if DEBUG: print(f"DEBUG: Config loaded: {list(config.keys()) if config else 'None'}")

    if args.favorites:
        if DEBUG: print("DEBUG: Running favorites mode")
        download_favorites(config)
    else:
        if DEBUG: print("DEBUG: Running normal query mode")
        run_queries(config)

    end_time = datetime.now()
    duration = end_time - start_time
    print(f"✨ Script finished at {end_time.strftime('%Y-%m-%d %H:%M:%S')} (elapsed: {duration})")

if __name__ == "__main__":
    main()