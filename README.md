# Music Backend
Backend for music platform.

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
