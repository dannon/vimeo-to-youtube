# Vimeo to YouTube Migration

Migrates videos from a Vimeo account to YouTube, preserving titles, descriptions, tags, and original publish dates. Downloads via yt-dlp, uploads via the YouTube Data API. Tracks progress in a local state file so the process is resumable across runs (useful when hitting YouTube API quota limits).

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Vimeo

Generate a personal access token at https://developer.vimeo.com/apps with `private` and `video_files` scopes.

### YouTube

1. Create a project at https://console.cloud.google.com
2. Enable **YouTube Data API v3**
3. Create an **OAuth 2.0 Client ID** (Desktop app type)
4. Download the credentials as `client_secrets.json` into this directory
5. Add yourself as a test user in the OAuth consent screen

### Config

Copy and edit `config.py`:

```python
VIMEO_ACCESS_TOKEN = "your_token"
YOUTUBE_CLIENT_SECRETS_FILE = "client_secrets.json"
YOUTUBE_CATEGORY_ID = "28"  # Science & Technology
DOWNLOAD_DIR = "./downloads"
DEFAULT_PRIVACY = "unlisted"
```

Or set `VIMEO_ACCESS_TOKEN` and `DOWNLOAD_DIR` as environment variables.

## Usage

```bash
# Download all videos locally (no YouTube interaction)
python migrate.py --download-only

# Upload downloaded videos to YouTube (skips already completed)
python migrate.py

# List all Vimeo videos with migration status
python migrate.py --list

# Show progress summary
python migrate.py --status

# Simulate without downloading/uploading
python migrate.py --dry-run

# Retry only previously failed videos
python migrate.py --retry-failed

# Flip all completed videos from unlisted to public
python migrate.py --publish

# Add completed videos to a YouTube playlist
python migrate.py --add-to-playlist PLAYLIST_ID

# Set recording dates from original Vimeo publish dates
python migrate.py --set-dates
```

## Quota

YouTube Data API default quota is 10,000 units/day. Each upload costs 1,600 units (~6 uploads/day). Request a quota increase in the Google Cloud Console for faster migration. The script handles `quotaExceeded` errors gracefully — saves state and exits so you can resume the next day.
