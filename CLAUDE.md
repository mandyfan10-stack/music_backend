# CLAUDE.md — music_backend (бэкенд XXII SOUND)

API для Telegram Mini App «XXII SOUND»: каталог музыкальных релизов с
рецензиями, оценками по критериям, лайками, реакциями и push-уведомлениями.
Парный репозиторий с UI — `music-bot` (ванильный JS Mini App).

## Стек

- Python 3 + FastAPI + Uvicorn.
- MongoDB через Motor (асинхронный драйвер).
- `httpx` (внешние запросы), `beautifulsoup4` (парсинг страниц),
  `groq` (LLM-разбор метаданных ссылок).
- Весь код — один файл `server.py` (~1300 строк), без разбиения на модули.

## Структура

- `server.py` — всё приложение: конфиг, модели, авторизация, эндпоинты.
- `test_server.py` — тесты (pytest + pytest-asyncio).
- `migrate_unescape.py` — разовая миграция (снятие HTML-экранирования).
- `requirements.txt` / `requirements-dev.txt`, `pytest.ini`, `README.md`.

## API-эндпоинты

- `GET /api/data` — полный каталог + `currentUser` (роль, блокировка,
  `notificationsEnabled`) + `syncCursor`. Авторизация опциональна.
- `GET /api/sync/releases?since=&waitMs=` — инкрементальная синхронизация
  через long-poll (события из коллекции `sync_events`).
- `POST /api/releases`, `DELETE /api/releases/{id}` — релизы (только админ).
- `POST /api/reviews`, `DELETE /api/reviews/{id}`,
  `DELETE /api/reviews/by-author/{username}` — рецензии.
- `POST /api/likes`, `POST /api/reviews/{id}/react` — лайки и реакции.
- `POST /api/block` — блокировка пользователя (админ).
- `POST /api/notifications/subscribe` — вкл/выкл push-уведомлений.
- `POST /api/parse_link` — распознавание метаданных ссылки (админ).
- `GET /api/health`.

## Модель данных (MongoDB `raper_xxii_database`)

Коллекции: `releases`, `reviews` (6 критериев в `criteria`; `rating`/
`objectiveRating` **пересчитываются на сервере**), `likes`, `review_reactions`,
`blocked_users`, `sync_events` (TTL-индекс `expireAt`),
`notification_subscribers` (`{userId, username, chatId, enabled}`).
Индексы создаются на старте в `create_indexes()`.

## Авторизация

- Telegram `initData` валидируется через HMAC-SHA256 в
  `validate_telegram_init_data()`.
- Зависимости: `get_current_user`, `get_optional_user`, `require_admin`,
  `check_not_blocked`. Rate limiting — `RateLimiter` (20 req/min на пользователя
  или IP).
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
`MINI_APP_URL` и т.д.) — см. раздел Environment Variables в `README.md`.

## Команды

- Установка dev-зависимостей: `python -m pip install -r requirements-dev.txt`
- Тесты: `python -m pytest -q`
- Запуск: `python server.py` (порт из `PORT`, по умолчанию 8000)

## Конвенции

- Рейтинги рецензий и критерии **нормализуются и пересчитываются на сервере**
  (`normalize_criteria`, `compute_review_ratings`) — клиентским значениям не
  доверяем (защита от накрутки).
- Любая мутация релизов/рецензий пишет событие в `sync_events`
  (`record_release_sync_event` / `record_review_sync_event`) — иначе клиенты
  не получат обновление через long-poll.
- Парсер ссылок защищён от SSRF (`is_safe_public_url`).
- Новые async-фоновые задачи не должны блокировать ответ эндпоинта
  (`asyncio.create_task`).
