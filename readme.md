# nhentai Downloader

Downloads galleries from nhentai using the official v2 API.  
Supports search queries and downloading your entire favorites list.

## Features

- Search with natural language, tag filters, and negation (same syntax as website)
- Download all favorites (requires API key)
- Concurrent image downloads (configurable threads)
- Respects rate limits (30/min for search/favorites, 45/min for gallery details)
- Retries on network errors and 429 responses
- Outputs CBZ files with ComicInfo.xml (typed tags, upload date, source URL)
- Keeps skip lists to avoid re-downloading
- Atomic writes with backup for skip lists

## Requirements

- Python 3.8+
- `requests`, `tqdm`

## Installation


git clone https://github.com/yourname/nhentai-dl  
cd nhentai-dl  
pip install -r requirements.txt

- On Debian/Ubuntu:

sudo apt update  
sudo apt install python3-requests python3-tqdm