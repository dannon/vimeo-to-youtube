# Vimeo → YouTube Migration Script

## Goal

Build a Python script to migrate ~154 videos (~80GB) from a paid Vimeo account to the existing **galaxyproject** YouTube channel, preserving metadata. The script should be resumable, handle failures gracefully, and manage disk space by downloading/uploading one video at a time.

## Architecture

Three-stage pipeline per video: **fetch metadata → download → upload → cleanup**.

### Key Design Decisions

- **One video at a time**: Download, upload, delete local file. Don't accumulate 80GB on disk.
- **Resumable**: Track progress in a local JSON state file (e.g., `migration_state.json`). On restart, skip already-completed videos.
- **Resumable uploads**: Use YouTube's resumable upload protocol so partial uploads can continue after network failures.
- **Exponential backoff + retries**: Both APIs can be flaky. Retry transient failures.
- **Upload as unlisted first**: All videos go up as `unlisted`. A separate flag/mode can flip them to `public` after verification.

## API Details

### Vimeo (Source)

- **Library**: `PyVimeo` (`pip install PyVimeo`)
- **Auth**: Personal access token with `private` and `video_files` scopes — generate at https://developer.vimeo.com/apps
- **List videos**: `GET /me/videos?per_page=100&page=N` — paginate to get all 154
- **Download**: Paid accounts get a `download` array on each video object with links at various quality levels. Use the highest quality (`source` or largest `width`).
- **Metadata to capture**: `name` (title), `description`, `tags` (array of `{tag: "..."}` objects), `created_time`, `privacy.view`, `duration`, `link` (original Vimeo URL — useful for description footer)

### YouTube (Destination)

- **Library**: `google-api-python-client` + `google-auth-oauthlib` (`pip install google-api-python-client google-auth-oauthlib`)
- **Auth**: OAuth 2.0 (required for uploads — API keys won't work)
  1. Go to https://console.cloud.google.com
  2. Create a project (or use existing)
  3. Enable "YouTube Data API v3"
  4. Create OAuth 2.0 Client ID (Desktop application type)
  5. Download the `client_secrets.json` file into this project directory
  6. First run will open a browser for consent; token is cached in `token.pickle` after that
- **Upload endpoint**: `youtube.videos().insert()` with `part="snippet,status"` and `MediaFileUpload(path, resumable=True)`
- **Quota**: Default is 10,000 units/day. Each upload = 1,600 units = ~6 uploads/day on default quota. **Request a quota increase immediately** at https://console.cloud.google.com → YouTube Data API → Quotas → Request increase. Ask for enough to do all 154 in a reasonable timeframe (250,000 units would allow ~156 uploads/day).

## Script Structure

```
vimeo-to-youtube/
├── VIMEO_TO_YOUTUBE_MIGRATION.md   ← this file
├── migrate.py                       ← main migration script
├── config.py                        ← credentials/settings (gitignored)
├── migration_state.json             ← auto-generated progress tracker
├── client_secrets.json              ← YouTube OAuth credentials (gitignored)
├── token.pickle                     ← cached YouTube auth token (gitignored)
├── .gitignore
└── requirements.txt
```

### `migrate.py` — Core Logic

```
1. Load config (Vimeo token, YouTube client secrets path)
2. Authenticate to both APIs
3. Fetch full video list from Vimeo (paginate through all pages)
4. Load migration_state.json (or create empty)
5. For each video not yet in state as "completed":
   a. Download from Vimeo to a temp file (stream, don't load into memory)
   b. Upload to YouTube with metadata:
      - title = Vimeo title
      - description = Vimeo description + "\n\nOriginally published on Vimeo: {vimeo_link}"
      - tags = Vimeo tags
      - privacyStatus = "unlisted"
      - categoryId = "28" (Science & Technology) — or make configurable
   c. Record in state: vimeo_id, youtube_id, status="completed", timestamp
   d. Delete local temp file
   e. Log progress: "Completed 47/154: {title}"
6. On failure: record status="failed" with error, continue to next video
```

### `migration_state.json` — Progress Tracker

```json
{
  "videos": {
    "/videos/123456": {
      "title": "Galaxy Admin Training 2023",
      "vimeo_uri": "/videos/123456",
      "youtube_id": "dQw4w9WgXcQ",
      "status": "completed",
      "completed_at": "2025-03-24T10:30:00Z"
    },
    "/videos/789012": {
      "title": "GCC2024 Keynote",
      "vimeo_uri": "/videos/789012",
      "status": "failed",
      "error": "Upload quota exceeded",
      "failed_at": "2025-03-24T11:00:00Z"
    }
  },
  "last_run": "2025-03-24T11:00:00Z"
}
```

### CLI Interface

Suggested argparse commands:

- `python migrate.py` — run migration (skip completed, retry failed)
- `python migrate.py --list` — fetch and display all Vimeo videos with status
- `python migrate.py --dry-run` — go through the motions without downloading/uploading
- `python migrate.py --publish` — flip all completed unlisted videos to public
- `python migrate.py --retry-failed` — only retry videos with status "failed"
- `python migrate.py --status` — show migration progress summary

## Config

`config.py` (or use env vars — whatever's cleaner):

```python
VIMEO_ACCESS_TOKEN = "your_token_here"
YOUTUBE_CLIENT_SECRETS_FILE = "client_secrets.json"
YOUTUBE_CATEGORY_ID = "28"  # Science & Technology
DOWNLOAD_DIR = "/tmp/vimeo-migration"  # temp storage for downloads
DEFAULT_PRIVACY = "unlisted"
```

## Dependencies

```
requirements.txt:
PyVimeo
google-api-python-client
google-auth-oauthlib
requests
tqdm  # progress bars for download/upload
```

## Important Notes

- **Quota is the bottleneck**: If the quota increase isn't approved yet, the script will hit quota limits after ~6 videos. It should handle `quotaExceeded` errors gracefully — log the error, save state, and exit cleanly so you can resume the next day.
- **Video file sizes vary**: Some might be multi-GB. Use streaming downloads and chunked resumable uploads.
- **Vimeo download links expire**: They're temporary signed URLs. Fetch a fresh one right before downloading each video, don't pre-fetch all links at the start.
- **YouTube title limit**: 100 characters. Truncate if needed.
- **YouTube description limit**: 5,000 characters. Truncate if needed.
- **YouTube tags limit**: 500 characters total across all tags.
- **Rate limit both directions**: Add a small delay between uploads (even 2-3 seconds) to be a good API citizen.

