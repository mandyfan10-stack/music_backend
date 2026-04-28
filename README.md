# Music Backend
Backend for music platform.

## Development

Install development dependencies and run tests:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## Release Sync

Clients can load the initial catalog with `GET /api/data`, store its
`syncCursor`, then poll `GET /api/sync/releases?since=<syncCursor>` for fast
incremental updates. Use the returned `cursor` value on the next request.
If polling pauses while the user is on another UI tab, resume with the last
stored cursor to catch up on missed changes. The response includes:

- `releases`: new or updated releases.
- `deletedReleaseIds`: releases removed since the previous cursor.
- `hasMore`: `true` when the client should immediately request the next page.

For near-instant updates without WebSockets, use long polling:
`GET /api/sync/releases?since=<cursor>&waitMs=25000`. The request returns as
soon as new release events are available, or empty after the wait timeout.

## Link Parsing

Yandex Music links are parsed through the Yandex Music API first, including
album/track titles, artists, labels, and cover URLs. AI parsing is only used as
a fallback when real page metadata is available; it should not invent release
data from an empty page or a URL alone.

## Environment Variables

- `MONGO_URL` (Required): MongoDB connection string.
- `TELEGRAM_BOT_TOKEN`: Telegram bot token (Required in production, enables signature verification).
- `ADMIN_USERNAMES`: Comma-separated list of admin usernames (Required in production).
- `ENV`: Environment setting. Set to `production`, `development`, or `test`. (Default: `development`).
- `DEV_MODE`: Set to `true` to enable dev mode features like mock users. Cannot be `true` in production.
- `INIT_DATA_MAX_AGE`: Maximum age in seconds for Telegram initData (Default: 86400).
- `GROQ_API_KEY`: API key for GROQ AI.
- `GROQ_MODEL_PRIMARY`: Primary GROQ model (Default: `llama-3.3-70b-versatile`).
- `GROQ_MODEL_FALLBACKS`: Comma-separated list of fallback models.
- `GROQ_MAX_RETRIES`: Number of retries for AI calls.
- `GROQ_TIMEOUT`: AI request timeout in seconds (Default: 8).
- `YANDEX_MUSIC_API_BASE`: Yandex Music API base URL (Default: `https://api.music.yandex.net`).
- `YANDEX_COVER_SIZE`: Cover size used for Yandex Music `coverUri` templates (Default: `1000x1000`).
- `SYNC_POLL_INTERVAL_MS`: Server-side long-poll check interval (Default: 500).
- `SYNC_MAX_WAIT_MS`: Maximum long-poll wait time in milliseconds (Default: 25000).
