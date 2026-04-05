#!/usr/bin/env python3
"""
nhentai downloader multi-query search OR download favorites.
Features: rate limiting, concurrency, skip lists, upload date addition.
Now with atomic JSON writes and automatic backup/restore of skip lists.

run as a cronjob! example!

0 */2 * * * cd /your/dir && /usr/bin/python3 nh_downloader.py >> /your/dir/archive.log 2>&1
30 */2 * * * cd /your/dir && /usr/bin/python3 nh_downloader.py --favorites >> /your/dir/favorites.log 2>&1
"""

import os
import sys
import json
import time
import zipfile
import shutil
import argparse
import concurrent.futures
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
    """Load JSON from primary; fallback to backup; exit if both corrupt (nonempty malformed)."""
    primary = file_path
    backup = file_path + ".bak"

    def try_load(path):
        if not os.path.exists(path):
            return None  # missing
        if os.path.getsize(path) == 0:
            return None  # empty
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(data)
            return None
        except (json.JSONDecodeError, OSError):
            return None  # corrupt

    # Try primary
    result = try_load(primary)
    if result is not None:
        return result

    # Primary missing/empty/corrupt → try backup
    result = try_load(backup)
    if result is not None:
        # Optional: restore primary from backup here? Not needed; next save will overwrite.
        return result

    # Both primary and backup are missing, empty, or corrupt.
    # Check if either exists and is non‑empty but corrupt → panic
    for path in [primary, backup]:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            print(f"❌ FATAL: {path} is corrupted (invalid JSON). Cannot continue.")
            print("Please manually restore from backup or delete the file to start fresh.")
            sys.exit(1)
    # Neither file exists or both are empty → first run
    return set()

def save_json_with_backup(data_set, file_path):
    """Atomically write JSON to primary file, then copy to backup."""
    primary = file_path
    backup = file_path + ".bak"
    temp = file_path + ".tmp"

    # Write to temporary file first
    with open(temp, 'w') as f:
        json.dump(sorted(data_set), f, indent=2)
    # Atomic rename (works on Unix/Windows)
    os.replace(temp, primary)
    # Copy to backup
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

search_limiter = RateLimiter(min_interval=3.0)   # 20/min
general_limiter = RateLimiter(min_interval=1.5)  # 30/min
favorites_limiter = RateLimiter(min_interval=4.0)  # 15/min

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
        print(f"⚠️ Tag cache '{TAG_CACHE_FILE}' not found. Starting empty (will auto‑populate).")
        return {}, {}
    with open(TAG_CACHE_FILE, 'r') as f:
        tag_cache = json.load(f)
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

def update_cache_from_gallery(gallery_id, gallery_full):
    tags = gallery_full.get('tags', [])
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
        print(f"  Added {added} new tags to cache (gallery {gallery_id})")
    return added

# ---------- Skip list (for queries) with backup ----------
def load_skip_ids():
    return load_json_with_backup(SKIP_FILE)

def add_to_skip_list(gallery_id):
    global SKIP_IDS
    SKIP_IDS.add(gallery_id)
    save_json_with_backup(SKIP_IDS, SKIP_FILE)

SKIP_IDS = load_skip_ids()

# ---------- Favorites cache with backup ----------
def load_favorites_cache():
    return load_json_with_backup(FAVORITES_CACHE_FILE)

def add_to_favorites_cache(gallery_id):
    global FAVORITES_DOWNLOADED
    FAVORITES_DOWNLOADED.add(gallery_id)
    save_json_with_backup(FAVORITES_DOWNLOADED, FAVORITES_CACHE_FILE)

FAVORITES_DOWNLOADED = load_favorites_cache()

# ---------- Config loading ----------
def load_config():
    if DEBUG: print("DEBUG: Looking for config file: {CONFIG_FILE}")
    if DEBUG: print("DEBUG: Current working directory: {os.getcwd()}")
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Config file '{CONFIG_FILE}' not found.")
        print("Example config (copy the JSON below):")
        print(json.dumps({
            "queries": ["tag -tag tag", "tag -tag -tag tag"],
            "consecutive_skipped_limit": 100,
            "favorites_consecutive_skipped_limit": 100,
            "max_concurrent_downloads": 3,
            "download_dir": "./downloads",
            "favorites_download_dir": "./favorites",
            "delay_between_galleries": 1,
            "dry_run": False,
            "stop_at_first": False,
            "add_upload_date": True,
            "api_key": "nhk_..."
        }, indent=2))
        print("\n# Notes:")
        print("# - 'queries' supports search syntax (https://nhentai.net/info). Each query is processed sequentially.")
        print("# - 'consecutive_skipped_limit': stop after N skipped galleries in a row. 0 = unlimited.")
        print("# - 'delay_between_galleries' adds a pause after each gallery download, even with 1s delay we will be rate limited anyway.")
        sys.exit(1)
    if DEBUG: print("DEBUG: Config file found, loading...")
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def get_cdn_base():
    resp = request_with_retry('GET', "https://nhentai.net/api/v2/cdn", headers={"User-Agent": USER_AGENT}, limiter=general_limiter)
    data = resp.json()
    server = data.get("image_servers", ["i.nhentai.net"])[0]
    if not server.startswith(('http://', 'https://')):
        return f"https://{server}"
    return server.rstrip('/')

# ---------- API calls with optional API key ----------
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
    url = f"https://nhentai.net/api/v2/galleries/{gallery_id}"
    headers = get_auth_headers(api_key)
    resp = request_with_retry('GET', url, headers=headers, limiter=general_limiter)
    return resp.json()

# ---------- Image download with retries and timeout ----------
def download_image(img_url, save_path, max_retries=3):
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(max_retries):
        try:
            resp = requests.get(img_url, headers=headers, stream=True, timeout=30)
            resp.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            return True
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"  Failed after {max_retries} attempts: {e}")
                return False
            print(f"  Retry {attempt+1}/{max_retries} for {img_url}")
            time.sleep(2)
    return False

# ---------- Safe filename truncation ----------
def safe_filename(text, max_bytes=200):
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode('utf-8', 'ignore')

def capitalize_words(s):
    return ' '.join(word.capitalize() for word in s.split())

# ---------- ComicInfo.xml ----------
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

    # Collect tags by type
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

    # Build XML parts with escaping
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

# ---------- Gallery download (with concurrency and fail on missing image) ----------
def download_gallery(gallery_id, title, base_url, download_dir, dry_run, gallery_listing, max_workers, api_key=None, add_upload_date=False):
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

    # Always fetch full gallery details (now includes pages)
    print(f"  Fetching gallery details...")
    try:
        full_gallery = get_gallery_details(gallery_id, api_key)
        update_cache_from_gallery(gallery_id, full_gallery)
    except Exception as e:
        print(f"  Warning: Could not fetch gallery details: {e}")
        return False

    # Extract upload date if requested
    year = month = day = None
    if add_upload_date:
        upload_ts = full_gallery.get('upload_date')
        if upload_ts:
            dt = datetime.fromtimestamp(upload_ts)
            year, month, day = dt.year, dt.month, dt.day
            print(f"  Upload date: {year}-{month:02d}-{day:02d}")

    # Pages are now inside full_gallery
    pages = full_gallery.get('pages', [])
    if not pages:
        print(f"  No pages found for gallery {gallery_id}")
        return False

    if dry_run:
        print(f"  [DRY RUN] Would download {len(pages)} images and create CBZ at {cbz_path}")
        return True

    # Prepare download tasks (use page 'path' field)
    image_tasks = []
    for idx, page in enumerate(pages, 1):
        path = page['path']
        if not path.startswith('/'):
            path = '/' + path
        img_url = base_url + path
        ext = img_url.split('.')[-1].split('?')[0]
        if ext not in ('jpg','jpeg','png','gif','webp'):
            ext = 'jpg'
        save_path = temp_dir / f"{idx:03d}.{ext}"
        image_tasks.append((img_url, save_path))

    # Download images concurrently, fail if any image fails
    image_paths = []
    failed = False
    with tqdm(total=len(image_tasks), desc=f"Downloading {english_title[:30]}", unit="img", leave=False) as pbar:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {}
            for idx, (img_url, save_path) in enumerate(image_tasks):
                future = executor.submit(download_image, img_url, save_path)
                future_to_idx[future] = idx
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    if future.result():
                        image_paths.append(image_tasks[idx][1])
                    else:
                        failed = True
                        for f in future_to_idx:
                            f.cancel()
                        break
                except Exception as e:
                    print(f"  Error downloading image {idx+1}: {e}")
                    failed = True
                    for f in future_to_idx:
                        f.cancel()
                    break
                pbar.update(1)
                time.sleep(0.05)

    if failed:
        print(f"  ❌ Gallery {gallery_id} failed due to missing image(s). Not added to skip list.")
        return False

    # Create ComicInfo.xml with date if available
    xml_content = create_comic_info_xml(gallery_listing, api_key, year, month, day)
    xml_path = temp_dir / "ComicInfo.xml"
    with open(xml_path, 'w', encoding='utf-8') as f:
        f.write(xml_content)

    # Create CBZ
    with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_DEFLATED) as cbz:
        for img_path in sorted(image_paths):
            cbz.write(img_path, img_path.name)
        cbz.write(xml_path, "ComicInfo.xml")

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
    max_concurrent = config.get("max_concurrent_downloads", 3)
    delay = config.get("delay_between_galleries", 1)
    dry_run = config.get("dry_run", False)
    consecutive_skipped_limit = config.get("favorites_consecutive_skipped_limit", 0)
    add_upload_date = config.get("add_upload_date", False)

    if dry_run:
        print("🚀 DRY RUN MODE – no files will be written\n")

    base_url = get_cdn_base()
    print(f"CDN base: {base_url}")
    print("🔑 Using API key to fetch favorites\n")

    page = 1
    total_downloaded = 0
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

        # Filter out already downloaded favorites
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
            gid = fav['id']
            title = fav.get('english_title') or fav.get('japanese_title') or str(gid)
            print(f"\n🎯 {title} (ID: {gid})")
            if download_gallery(gid, title, base_url, download_dir, dry_run, fav, max_concurrent, api_key, add_upload_date):
                total_downloaded += 1
                add_to_favorites_cache(gid)
                consecutive_skipped = 0
            else:
                print(f"❌ Failed {title}")
            time.sleep(delay)

        if page >= data.get('num_pages', 0):
            break
        page += 1

    print(f"\n✨ Favorites download complete! Downloaded {total_downloaded} new favorites to {download_dir}")

# ---------- Normal query mode ----------
def run_queries(config):
    queries = config.get("queries")
    if not queries or not isinstance(queries, list):
        print("❌ 'queries' must be a non‑empty list in config for normal mode.")
        sys.exit(1)
    consecutive_skipped_limit = config.get("consecutive_skipped_limit", 0)
    max_concurrent = config.get("max_concurrent_downloads", 3)
    download_dir = config.get("download_dir", "./downloads")
    delay = config.get("delay_between_galleries", 1)
    dry_run = config.get("dry_run", False)
    stop_at_first = config.get("stop_at_first", False)
    api_key = config.get("api_key", None)
    add_upload_date = config.get("add_upload_date", False)

    if dry_run:
        print("🚀 DRY RUN MODE – no files will be written\n")

    base_url = get_cdn_base()
    print(f"CDN base: {base_url}")
    if api_key:
        print("🔑 Using API key for higher rate limits")
    else:
        print("🔓 No API key – using public endpoints (lower rate limits)")

    total_downloaded = 0

    for qidx, query in enumerate(queries, 1):
        print(f"\n{'='*60}")
        print(f"🔍 Processing query {qidx}/{len(queries)}: {query}")
        print(f"{'='*60}")

        page = 1
        query_downloaded = 0
        consecutive_skipped = 0
        stop_query = False

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

                title = gal.get('english_title') or gal.get('japanese_title') or str(gid)
                print(f"\n🎯 {title} (ID: {gid})")
                if download_gallery(gid, title, base_url, download_dir, dry_run, gal, max_concurrent, api_key, add_upload_date):
                    query_downloaded += 1
                    total_downloaded += 1
                    add_to_skip_list(gid)
                else:
                    print(f"❌ Failed {title}")

                if pbar:
                    pbar.update(1)

                if stop_at_first:
                    stop_query = True
                    break

                time.sleep(delay)

            if stop_query:
                break

            if page >= data.get('num_pages', 0):
                break
            page += 1

        if pbar:
            pbar.close()
        print(f"✅ Query finished. Downloaded {query_downloaded} new galleries.")

    print(f"\n✨ All queries done! Total downloaded: {total_downloaded} CBZ file(s) in {download_dir}")

# ---------- Main ----------
def main():
    if DEBUG: print("DEBUG: Entered main()")
    start_time = datetime.now()
    print(f"🚀 Script started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    parser = argparse.ArgumentParser(description="nhentai downloader")
    parser.add_argument("--favorites", action="store_true", help="Download all favorites (requires API key in config)")
    args = parser.parse_args()
    if DEBUG: print("DEBUG: Arguments parsed: {args}")

    config = load_config()
    if DEBUG: print("DEBUG: Config loaded: {list(config.keys()) if config else 'None'}")

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