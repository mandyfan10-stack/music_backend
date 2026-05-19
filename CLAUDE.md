# CLAUDE.md — music_backend (бэкенд XXII SOUND)

API для Telegram Mini App «XXII SOUND»: каталог музыкальных релизов с
рецензиями, оценками по критериям, лайками, реакциями, комментариями и
push-уведомлениями. Парный репозиторий с UI — `music-bot` (ванильный JS).

## Стек

- Python 3 + FastAPI + Uvicorn.
- MongoDB через Motor (асинхронный драйвер).
- `httpx` (внешние запросы), `beautifulsoup4` (парсинг страниц),
  `groq` (LLM-разбор метаданных ссылок).
- Весь код — один файл `server.py` (~1560 строк), без разбиения на модули.

## Структура

- `server.py` — всё приложение: конфиг, модели, авторизация, эндпоинты.
- `test_server.py` — тесты (pytest + pytest-asyncio, MongoDB замокан).
- `migrate_unescape.py` — разовая миграция (снятие HTML-экранирования).
- `requirements.txt` / `requirements-dev.txt`, `pytest.ini`, `README.md`.
- `AUDIT_BACKEND_*.md` — отчёты прошлых аудитов.

## API-эндпоинты

- `GET /api/data` — каталог (`releases`, `reviews`, `comments`) + `currentUser`
  (роль, блокировка, `notificationsEnabled`) + `syncCursor`. Авторизация
  опциональна. Размер выборки — `releasesLimit`/`reviewsLimit`/`commentsLimit`
  (env-дефолты `DATA_*_LIMIT`); `totalReleases`/`totalReviews`/`totalComments`
  показывают полный объём.
- `GET /api/sync/releases?since=&limit=&waitMs=` — инкрементальная
  синхронизация через long-poll (события из `sync_events`). Дельта несёт
  изменения релизов, рецензий **и** комментариев.
- `POST /api/releases`, `DELETE /api/releases/{id}` — релизы (только админ).
  Удаление каскадно сносит рецензии, лайки, реакции, комментарии.
- `POST /api/releases/{id}/share-message` — готовит нативное Telegram-сообщение
  для шеринга (Bot API `savePreparedInlineMessage`); клиент отдаёт `id` в
  `Telegram.WebApp.shareMessage`.
- `POST /api/reviews`, `DELETE /api/reviews/{id}`,
  `DELETE /api/reviews/by-author/{username}` — рецензии.
- `POST /api/reviews/{id}/comments`, `DELETE /api/comments/{id}` — комментарии
  к рецензиям (несколько на рецензию; владелец или админ удаляет).
- `POST /api/likes`, `POST /api/reviews/{id}/react` — лайки и реакции.
- `POST /api/block` — блокировка пользователя (админ).
- `POST /api/notifications/subscribe` — вкл/выкл push-уведомлений.
- `POST /api/parse_link` — распознавание метаданных ссылки (админ).
- `GET /api/health`.

## Модель данных (MongoDB `raper_xxii_database`)

Коллекции: `releases`, `reviews` (6 критериев в `criteria`; `rating`/
`objectiveRating` **пересчитываются на сервере**), `review_comments`, `likes`,
`review_reactions`, `blocked_users`, `sync_events` (TTL-индекс `expireAt`),
`notification_subscribers` (`{userId, username, chatId, enabled}`).
Индексы создаются на старте в `create_indexes()`.

## Синхронизация

- Любая мутация релиза/рецензии/комментария пишет событие в `sync_events`
  (`record_release_sync_event` / `record_review_sync_event` /
  `record_comment_sync_event`) — иначе клиенты не получат обновление.
- `sync_releases` ждёт новые события через `wait_for_release_sync_events`
  (long-poll до `SYNC_MAX_WAIT_MS`), затем подтягивает изменённые документы.
- Курсор (`since` в запросе, `cursor`/`syncCursor` в ответе) — **строка**:
  токены `time_ns()` превышают `Number.MAX_SAFE_INTEGER`, числом JS терял бы
  точность.

## Авторизация

- Telegram `initData` валидируется через HMAC-SHA256 в
  `validate_telegram_init_data()` (плюс проверка свежести `auth_date`).
- Зависимости: `get_current_user`, `get_optional_user`, `require_admin`,
  `check_not_blocked`. Rate limiting — `RateLimiter` (20 req/min); ключ —
  проверенный `user_id` или правый хоп `X-Forwarded-For` (`client_rate_key`).
- Без `TELEGRAM_BOT_TOKEN` работает dev-режим (только если `DEV_MODE=true` и
  `ENV != production`).

## Push-уведомления

При **вставке** нового релиза (`add_release`, `update_one` с `upserted_id`)
фоном вызывается `send_release_notifications()`: рассылка подписчикам через
Telegram Bot API `sendMessage`. Модель opt-out (нет записи = подписан).
Заблокировавших бота (HTTP 403) автоматически отписываем. Требует
`TELEGRAM_BOT_TOKEN`; `MINI_APP_URL` добавляет кнопку «Открыть в приложении».

## Переменные окружения

Обязательные: `MONGO_URL`; в production — `TELEGRAM_BOT_TOKEN`,
`ADMIN_USERNAMES`. Прочие (`ENV`, `DEV_MODE`, `GROQ_*`, `YANDEX_*`, `SYNC_*`,
`DATA_*_LIMIT`, `MINI_APP_URL`, `MONGO_*_POOL_SIZE` и т.д.) — см. раздел
Environment Variables в `README.md`.

## Команды

- Установка dev-зависимостей: `python -m pip install -r requirements-dev.txt`
- Тесты: `python -m pytest -q`
- Синтаксис: `python -m py_compile server.py`
- Запуск: `python server.py` (порт из `PORT`, по умолчанию 8000)

## Конвенции

- Рейтинги рецензий и критерии **нормализуются и пересчитываются на сервере**
  (`normalize_criteria`, `compute_review_ratings`) — клиентским значениям не
  доверяем (защита от накрутки).
- Каждая мутация пишет событие в `sync_events` (см. раздел «Синхронизация»).
- Парсер ссылок защищён от SSRF (`is_safe_public_url` — проверка на каждом
  redirect-хопе; блокирующий DNS-резолв уводится в `asyncio.to_thread`).
- Новые async-фоновые задачи не должны блокировать ответ эндпоинта — через
  `spawn_background()` (держит strong-ref, чтобы задачу не собрал GC).
