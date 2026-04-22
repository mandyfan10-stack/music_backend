## 2024-04-18 - [Fix Fail Open Authentication in Telegram initData Verification]
**Vulnerability:** The application had a "Fail Open" authentication logic. If the `TELEGRAM_BOT_TOKEN` environment variable was missing (e.g., due to configuration drift or misconfiguration in production), the application would automatically enter a "DEV MODE" and skip HMAC-SHA256 signature verification. This allowed unauthenticated users to completely bypass authentication and assume admin roles by injecting fake Telegram `initData` or headers.
**Learning:** Environmental fallbacks must never degrade security by default. Fallbacks to developer mode or bypassing signature checks must be explicitly opted into (e.g., via a `DEV_MODE=true` flag) rather than implicitly activated by a missing secret.
**Prevention:** Always enforce the "Fail Securely" principle. If a critical configuration variable needed for authentication is missing, the application must hard-fail (e.g., raise a 500 Error) and deny access, rather than falling back to an insecure state.
## 2024-05-18 - [HIGH] Fix Stored XSS in User Reviews
**Vulnerability:** Found Stored XSS vulnerability where users could submit malicious HTML/JavaScript in the `text` field of `Review` models, and `name`/`artist`/`genre` in `Release` models. The `img` and `link` fields were also susceptible to `javascript:` scheme attacks.
**Learning:** Pydantic models validate types and constraints but do not automatically sanitize input strings. In a FastAPI application taking user input that is later rendered in a UI, this creates a major XSS risk if the UI doesn't strictly escape content.
**Prevention:** Use Pydantic's `@field_validator` with `mode="before"` to run `html.escape()` on string fields before they are accepted, and explicitly check URL fields using `startswith(("http://", "https://"))`.
## 2024-05-24 - Pydantic Nested Dictionary XSS Vulnerability
**Vulnerability:** XSS payload allowed through nested dictionaries (`criteria: dict = {}`) in Pydantic models.
**Learning:** Pydantic's standard `dict` fields bypass custom string sanitization validators defined for string fields. If a string inside a nested structure (dict or list) is not explicitly sanitized, it can carry XSS payloads.
**Prevention:** Implement a recursive sanitization function for dict/list fields or use customized Pydantic models/types that sanitize strings implicitly.
