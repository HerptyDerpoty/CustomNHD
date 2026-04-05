# Custom nhentai Downloader

Downloads galleries from nhentai using the official v2 API.  
Supports search queries and downloading your entire favorites list.  
Made to work with komga specifically hence the folder structure  
Will slot in replace 9-FS/nhentai_archivist if you are using that to archive, uses same tag structure and folder naming scheme

## Features

- Search with natural language, tag filters, and negation ([same syntax as website](https://nhentai.net/info))
- Download all favorites (requires API key)
- Respects rate limits, mostly (shouldnt get you banned, we go pretty slow!)
- Retries on network errors and 429 responses
- Outputs CBZ files with ComicInfo.xml
- Keeps skip lists to avoid re-downloading

## Requirements

- Python 3.8+
- `requests`, `tqdm`

## Installation


git clone https://github.com/HerptyDerpoty/CustomNHD  
cd CustomNHD  

Then


pip install requests tqdm

- On Debian/Ubuntu:

sudo apt update  
sudo apt install python3-requests python3-tqdm

## Running

python3 nh_downloader.py

- For favorites scraping

python3 nh_downloader.py --favorites

Will dump cbzs into the download folders with this structure /12345 Name/12345 name.cbz  
as this is what something like komga likes

There is further info inside nh_downloader.py AND skiplistbuilder.py

## This is AI slop! Sorry!

## License

MIT