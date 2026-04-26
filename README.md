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
