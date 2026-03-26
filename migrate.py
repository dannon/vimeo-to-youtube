#!/usr/bin/env python3
"""Migrate videos from Vimeo to YouTube, one at a time, with resumable state."""

import argparse
import json
import os
import pickle
import random
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests
import vimeo
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from tqdm import tqdm

import config

STATE_FILE = "migration_state.json"
METADATA_FILE = "vimeo_metadata.json"
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
CHUNK_SIZE = 50 * 1024 * 1024  # 50MB upload chunks


class QuotaExceededError(Exception):
    pass


# --- State Management ---


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            print(f"Warning: corrupt {STATE_FILE}, starting fresh")
    return {"videos": {}, "last_run": None}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def update_video_state(state, vimeo_uri, **kwargs):
    if vimeo_uri not in state["videos"]:
        state["videos"][vimeo_uri] = {"vimeo_uri": vimeo_uri}
    state["videos"][vimeo_uri].update(kwargs)
    save_state(state)


# --- Retry Logic ---


def retry_with_backoff(func, max_retries=5, initial_delay=1):
    """Call func(), retrying on transient errors with exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            return func()
        except HttpError as e:
            if e.resp.status == 403 and "quotaExceeded" in str(e):
                raise QuotaExceededError(str(e))
            if e.resp.status in (429, 500, 502, 503) and attempt < max_retries:
                delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"  Retrying in {delay:.1f}s (HTTP {e.resp.status})...")
                time.sleep(delay)
                continue
            raise
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                delay = initial_delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"  Retrying in {delay:.1f}s ({type(e).__name__})...")
                time.sleep(delay)
                continue
            raise


# --- Vimeo ---


def get_vimeo_client():
    client = vimeo.VimeoClient(token=config.VIMEO_ACCESS_TOKEN)
    resp = client.get("/me")
    if resp.status_code != 200:
        print(f"Vimeo auth failed: {resp.status_code} {resp.text}")
        sys.exit(1)
    user = resp.json()
    print(f"Authenticated to Vimeo as: {user.get('name', 'unknown')}")
    return client


def fetch_all_vimeo_videos(client):
    videos = []
    page = 1
    while True:
        resp = client.get("/me/videos", params={
            "per_page": 100,
            "page": page,
            "fields": "uri,name,description,tags,created_time,link,duration,download",
        })
        if resp.status_code != 200:
            print(f"Failed to fetch videos page {page}: {resp.status_code}")
            sys.exit(1)
        data = resp.json()
        videos.extend(data["data"])
        if data["paging"]["next"] is None:
            break
        page += 1
    print(f"Found {len(videos)} videos on Vimeo")
    return videos


def download_video(vimeo_url, dest_path):
    """Download a video using yt-dlp (handles all Vimeo auth/format selection)."""
    result = subprocess.run(
        [
            "yt-dlp",
            "--no-warnings",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", dest_path,
            "--newline",  # progress on separate lines
            vimeo_url,
        ],
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour max per video
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr.strip()}")
    if not os.path.exists(dest_path):
        raise RuntimeError(f"yt-dlp completed but file not found at {dest_path}")


# --- YouTube ---


def get_youtube_service():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        if not os.path.exists(config.YOUTUBE_CLIENT_SECRETS_FILE):
            print(f"Missing {config.YOUTUBE_CLIENT_SECRETS_FILE} — download from Google Cloud Console")
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(
            config.YOUTUBE_CLIENT_SECRETS_FILE, YOUTUBE_SCOPES
        )
        creds = flow.run_local_server(port=0)

    with open("token.pickle", "wb") as f:
        pickle.dump(creds, f)

    service = build("youtube", "v3", credentials=creds)
    print("Authenticated to YouTube")
    return service


def map_metadata(vimeo_video):
    title = (vimeo_video.get("name") or "Untitled")[:100]

    desc = vimeo_video.get("description") or ""
    vimeo_link = vimeo_video.get("link", "")
    if vimeo_link:
        suffix = f"\n\nOriginally published on Vimeo: {vimeo_link}"
        desc = desc[: 5000 - len(suffix)] + suffix
    else:
        desc = desc[:5000]

    tags = []
    total_len = 0
    for tag_obj in vimeo_video.get("tags") or []:
        tag = tag_obj.get("tag", "")
        if not tag:
            continue
        if total_len + len(tag) + (1 if tags else 0) > 500:
            break
        tags.append(tag)
        total_len += len(tag) + (1 if len(tags) > 1 else 0)

    return {
        "snippet": {
            "title": title,
            "description": desc,
            "tags": tags,
            "categoryId": config.YOUTUBE_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": config.DEFAULT_PRIVACY,
        },
    }


def upload_video(youtube, file_path, metadata):
    """Resumable upload to YouTube, returns video ID."""
    media = MediaFileUpload(file_path, mimetype="video/*", resumable=True, chunksize=CHUNK_SIZE)
    request = youtube.videos().insert(
        part="snippet,status",
        body=metadata,
        media_body=media,
    )

    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"  Upload progress: {int(status.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status == 403 and "quotaExceeded" in str(e):
                raise QuotaExceededError(str(e))
            if e.resp.status in (500, 502, 503):
                time.sleep(5)
                continue
            raise

    return response["id"]


def publish_videos(youtube, state):
    completed = [
        (uri, v) for uri, v in state["videos"].items()
        if v.get("status") == "completed" and v.get("youtube_id")
    ]
    if not completed:
        print("No completed videos to publish")
        return

    print(f"Publishing {len(completed)} videos...")
    for uri, v in completed:
        try:
            youtube.videos().update(
                part="status",
                body={
                    "id": v["youtube_id"],
                    "status": {"privacyStatus": "public"},
                },
            ).execute()
            print(f"  Published: {v.get('title', uri)}")
        except HttpError as e:
            print(f"  Failed to publish {v.get('title', uri)}: {e}")
        time.sleep(1)


# --- Download Only ---


def save_metadata(videos, path=METADATA_FILE):
    """Save full Vimeo metadata for all videos to a JSON file."""
    metadata = []
    for v in videos:
        metadata.append({
            "uri": v.get("uri"),
            "name": v.get("name"),
            "description": v.get("description"),
            "link": v.get("link"),
            "duration": v.get("duration"),
            "created_time": v.get("created_time"),
            "tags": [t.get("tag", "") for t in (v.get("tags") or [])],
        })
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(metadata, f, indent=2)
    os.replace(tmp, path)
    print(f"Saved metadata for {len(metadata)} videos to {path}")


def run_download_only(vimeo_client):
    """Download all videos and save metadata locally, no YouTube interaction."""
    videos = fetch_all_vimeo_videos(vimeo_client)
    save_metadata(videos)

    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    state = load_state()

    # Skip already-downloaded files
    eligible = []
    for video in videos:
        uri = video["uri"]
        video_state = state["videos"].get(uri, {})
        if video_state.get("status") == "downloaded":
            continue
        eligible.append(video)

    if not eligible:
        print("All videos already downloaded")
        return

    print(f"\n{len(eligible)} videos to download\n")

    for i, video in enumerate(eligible, 1):
        vimeo_uri = video["uri"]
        title = video.get("name") or "Untitled"
        video_id = vimeo_uri.split("/")[-1]
        file_path = os.path.join(config.DOWNLOAD_DIR, f"{video_id}.mp4")

        print(f"\n--- {i}/{len(eligible)}: {title} ---")

        try:
            update_video_state(state, vimeo_uri, status="downloading", title=title)
            vimeo_url = video.get("link", f"https://vimeo.com/{video_id}")
            download_video(vimeo_url, file_path)
            update_video_state(
                state, vimeo_uri,
                status="downloaded",
                local_path=file_path,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"  Saved to {file_path}")

        except Exception as e:
            update_video_state(
                state, vimeo_uri,
                status="failed",
                error=str(e),
                failed_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"  FAILED: {e}")

        time.sleep(1)

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


# --- Main Loop ---


def run_migration(args, vimeo_client, youtube):
    videos = fetch_all_vimeo_videos(vimeo_client)
    state = load_state()
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    # Filter to eligible videos
    eligible = []
    for video in videos:
        uri = video["uri"]
        video_state = state["videos"].get(uri, {})
        status = video_state.get("status")
        if args.retry_failed:
            if status == "failed":
                eligible.append(video)
        else:
            if status != "completed":
                eligible.append(video)

    if not eligible:
        print("Nothing to do — all videos are completed")
        return

    print(f"\n{len(eligible)} videos to process\n")

    for i, video in enumerate(eligible, 1):
        vimeo_uri = video["uri"]
        title = video.get("name") or "Untitled"
        file_path = os.path.join(config.DOWNLOAD_DIR, f"{vimeo_uri.split('/')[-1]}.mp4")

        if args.dry_run:
            print(f"[DRY RUN] {i}/{len(eligible)}: {title}")
            continue

        print(f"\n--- {i}/{len(eligible)}: {title} ---")

        try:
            # Download (skip if local file already exists)
            if os.path.exists(file_path):
                print(f"  Using local file: {file_path}")
            else:
                update_video_state(state, vimeo_uri, status="downloading", title=title)
                video_id = vimeo_uri.split("/")[-1]
                vimeo_url = video.get("link", f"https://vimeo.com/{video_id}")
                download_video(vimeo_url, file_path)

            # Upload
            update_video_state(state, vimeo_uri, status="uploading")
            metadata = map_metadata(video)
            youtube_id = upload_video(youtube, file_path, metadata)

            # Success
            update_video_state(
                state, vimeo_uri,
                status="completed",
                youtube_id=youtube_id,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"  Completed: https://youtu.be/{youtube_id}")

        except QuotaExceededError:
            save_state(state)
            completed = sum(1 for v in state["videos"].values() if v.get("status") == "completed")
            total = len(videos)
            print(f"\nQuota exceeded. Completed {completed}/{total}. Resume tomorrow.")
            sys.exit(0)

        except Exception as e:
            update_video_state(
                state, vimeo_uri,
                status="failed",
                error=str(e),
                failed_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"  FAILED: {e}")

        finally:
            time.sleep(3)

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def show_list(vimeo_client):
    videos = fetch_all_vimeo_videos(vimeo_client)
    state = load_state()

    for video in videos:
        uri = video["uri"]
        title = video.get("name") or "Untitled"
        duration = video.get("duration", 0)
        mins = duration // 60
        secs = duration % 60
        video_state = state["videos"].get(uri, {})
        status = video_state.get("status", "pending")
        yt_id = video_state.get("youtube_id", "")
        yt_link = f" -> https://youtu.be/{yt_id}" if yt_id else ""
        print(f"  [{status:>12}] {mins:3d}:{secs:02d}  {title}{yt_link}")


def show_status():
    state = load_state()
    videos = state["videos"]
    completed = sum(1 for v in videos.values() if v.get("status") == "completed")
    failed = sum(1 for v in videos.values() if v.get("status") == "failed")
    in_progress = sum(1 for v in videos.values() if v.get("status") in ("downloading", "uploading"))
    total = len(videos)

    print(f"Migration state ({STATE_FILE}):")
    print(f"  Completed:   {completed}")
    print(f"  Failed:      {failed}")
    print(f"  In progress: {in_progress}")
    print(f"  Total known: {total}")
    if state.get("last_run"):
        print(f"  Last run:    {state['last_run']}")


def add_to_playlist(youtube, playlist_id):
    """Add all completed videos to a YouTube playlist, skipping those already in it."""
    state = load_state()

    # Get videos already in the playlist
    existing = set()
    next_page = None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet", playlistId=playlist_id, maxResults=50, pageToken=next_page
        ).execute()
        for item in resp["items"]:
            existing.add(item["snippet"]["resourceId"]["videoId"])
        next_page = resp.get("nextPageToken")
        if not next_page:
            break

    to_add = [
        (uri, v) for uri, v in state["videos"].items()
        if v.get("status") == "completed" and v.get("youtube_id")
        and v["youtube_id"] not in existing
    ]

    if not to_add:
        print("All completed videos are already in the playlist")
        return

    print(f"{len(to_add)} videos to add to playlist...")
    for i, (uri, v) in enumerate(to_add, 1):
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": v["youtube_id"],
                        },
                    },
                },
            ).execute()
            print(f"  {i}/{len(to_add)}: {v.get('title', uri)}")
        except HttpError as e:
            if "quotaExceeded" in str(e):
                print(f"\nQuota exceeded after {i-1}/{len(to_add)}. Resume tomorrow.")
                return
            print(f"  {i}/{len(to_add)} FAILED: {v.get('title', uri)}: {e}")
        time.sleep(0.5)
    print("Done!")


def set_recording_dates(youtube):
    """Set recording date and update description with original Vimeo publish date."""
    state = load_state()

    if not os.path.exists(METADATA_FILE):
        print(f"Missing {METADATA_FILE} — run --download-only first")
        return

    with open(METADATA_FILE) as f:
        vimeo_meta = {v["uri"]: v for v in json.load(f)}

    completed = [
        (uri, v) for uri, v in state["videos"].items()
        if v.get("status") == "completed" and v.get("youtube_id")
    ]

    print(f"Updating {len(completed)} videos with recording dates...")
    for i, (uri, v) in enumerate(completed, 1):
        meta = vimeo_meta.get(uri)
        if not meta or not meta.get("created_time"):
            print(f"  {i}/{len(completed)}: SKIP {v.get('title')} (no date)")
            continue

        date_str = meta["created_time"][:10]

        try:
            resp = youtube.videos().list(
                part="snippet,recordingDetails", id=v["youtube_id"],
            ).execute()

            if not resp["items"]:
                print(f"  {i}/{len(completed)}: NOT FOUND {v.get('title')}")
                continue

            item = resp["items"][0]
            snippet = item["snippet"]
            desc = snippet.get("description", "")

            if "Originally published on Vimeo:" in desc and f"({date_str})" not in desc:
                desc = desc.replace(
                    "Originally published on Vimeo:",
                    f"Originally published on Vimeo ({date_str}):",
                )
                snippet["description"] = desc[:5000]

            youtube.videos().update(
                part="snippet,recordingDetails",
                body={
                    "id": v["youtube_id"],
                    "snippet": snippet,
                    "recordingDetails": {"recordingDate": date_str},
                },
            ).execute()
            print(f"  {i}/{len(completed)}: {v.get('title', uri)[:60]} -> {date_str}")

        except HttpError as e:
            if "quotaExceeded" in str(e):
                print(f"\nQuota exceeded after {i-1}/{len(completed)}. Resume tomorrow.")
                return
            print(f"  {i}/{len(completed)} FAILED: {v.get('title')}: {e}")

        time.sleep(0.5)
    print("Done!")


def main():
    parser = argparse.ArgumentParser(description="Migrate videos from Vimeo to YouTube")
    parser.add_argument("--list", action="store_true", help="List all Vimeo videos with migration status")
    parser.add_argument("--dry-run", action="store_true", help="Simulate migration without downloading/uploading")
    parser.add_argument("--publish", action="store_true", help="Set all completed videos to public")
    parser.add_argument("--retry-failed", action="store_true", help="Only retry previously failed videos")
    parser.add_argument("--download-only", action="store_true", help="Download all videos and metadata locally (no YouTube)")
    parser.add_argument("--status", action="store_true", help="Show migration progress summary")
    parser.add_argument("--add-to-playlist", metavar="PLAYLIST_ID", help="Add completed videos to a YouTube playlist")
    parser.add_argument("--set-dates", action="store_true", help="Set recording dates from original Vimeo publish dates")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.list:
        vimeo_client = get_vimeo_client()
        show_list(vimeo_client)
        return

    if args.download_only:
        vimeo_client = get_vimeo_client()
        run_download_only(vimeo_client)
        return

    if args.publish:
        youtube = get_youtube_service()
        state = load_state()
        publish_videos(youtube, state)
        return

    if args.add_to_playlist:
        youtube = get_youtube_service()
        add_to_playlist(youtube, args.add_to_playlist)
        return

    if args.set_dates:
        youtube = get_youtube_service()
        set_recording_dates(youtube)
        return

    # Default: run migration
    vimeo_client = get_vimeo_client()
    youtube = get_youtube_service()
    run_migration(args, vimeo_client, youtube)


if __name__ == "__main__":
    main()
