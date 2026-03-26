# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python script to migrate ~154 videos (~80GB) from a paid Vimeo account to the **galaxyproject** YouTube channel. See `prompt.md` for the full spec.

## Architecture

Three-stage pipeline per video: **fetch metadata → download → upload → cleanup**. One video at a time to avoid accumulating disk usage. Progress is tracked in `migration_state.json` for resumability.

### Key Files

- `migrate.py` — main migration script with CLI (argparse)
- `config.py` — credentials/settings (gitignored)
- `migration_state.json` — auto-generated progress tracker
- `client_secrets.json` / `token.pickle` — YouTube OAuth credentials (gitignored)

## Dependencies

```
pip install PyVimeo google-api-python-client google-auth-oauthlib requests tqdm
```

## CLI Usage

```bash
python migrate.py              # run migration (skip completed, retry failed)
python migrate.py --list       # show all Vimeo videos with status
python migrate.py --dry-run    # simulate without downloading/uploading
python migrate.py --publish    # flip completed unlisted videos to public
python migrate.py --retry-failed  # only retry failed videos
python migrate.py --status     # show migration progress summary
```

## API Constraints

- **YouTube quota**: Default 10,000 units/day; each upload = 1,600 units (~6/day). Handle `quotaExceeded` gracefully.
- **Vimeo download links expire**: Fetch fresh signed URLs right before each download, not in batch.
- **YouTube limits**: Title 100 chars, description 5,000 chars, tags 500 chars total.
- **Uploads go as unlisted first**, then a separate `--publish` pass flips to public.

## Auth Setup

- **Vimeo**: Personal access token with `private` + `video_files` scopes
- **YouTube**: OAuth 2.0 (Desktop app type) via `client_secrets.json`; first run opens browser for consent, caches token in `token.pickle`
