import pytest
import os
import importlib
import asyncio
from fastapi import HTTPException
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

@pytest.fixture
def clean_env():
    # Store old env
    old_env = dict(os.environ)
    yield
    # Restore old env
    os.environ.clear()
    os.environ.update(old_env)


class FakeCursor:
    def __init__(self, items):
        self.items = items

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, length):
        return self.items[:length]


class SlowFakeCursor(FakeCursor):
    async def to_list(self, length):
        await asyncio.sleep(0)
        return self.items[:length]


class FakeRequest:
    headers = {}
    query_params = {}
    client = None


def test_config_validation_production_no_token(clean_env):
    os.environ["ENV"] = "production"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"
    if "TELEGRAM_BOT_TOKEN" in os.environ:
        del os.environ["TELEGRAM_BOT_TOKEN"]

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN is required in production"):
        import server
        importlib.reload(server)

def test_config_validation_production_dev_mode(clean_env):
    os.environ["ENV"] = "production"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["DEV_MODE"] = "true"

    with pytest.raises(RuntimeError, match="DEV_MODE cannot be true when ENV is production"):
        import server
        importlib.reload(server)

def test_config_validation_production_no_admin(clean_env):
    os.environ["ENV"] = "production"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["DEV_MODE"] = "false"
    if "ADMIN_USERNAMES" in os.environ:
        del os.environ["ADMIN_USERNAMES"]

    with pytest.raises(RuntimeError, match="ADMIN_USERNAMES must be set in production"):
        import server
        importlib.reload(server)

def test_config_validation_success(clean_env):
    os.environ["ENV"] = "production"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["ADMIN_USERNAMES"] = "admin"
    os.environ["DEV_MODE"] = "false"

    import server
    importlib.reload(server)
    assert "admin" in server.ADMIN_USERNAMES

def test_config_validation_normalizes_admin_usernames(clean_env):
    os.environ["ENV"] = "production"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["ADMIN_USERNAMES"] = "@Admin, SecondAdmin"
    os.environ["DEV_MODE"] = "false"

    import server
    importlib.reload(server)
    assert server.ADMIN_USERNAMES == {"admin", "secondadmin"}


def _signed_init_data(token: str, fields: dict) -> str:
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields = dict(fields)
    fields["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_validate_telegram_init_data_requires_auth_date(clean_env):
    os.environ["ENV"] = "production"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["ADMIN_USERNAMES"] = "admin"
    os.environ["DEV_MODE"] = "false"

    import server
    importlib.reload(server)

    init_data = _signed_init_data("token", {
        "user": json.dumps({"id": 1, "username": "admin", "first_name": "Admin"}, separators=(",", ":")),
    })

    with pytest.raises(HTTPException) as exc_info:
        server.validate_telegram_init_data(init_data)

    assert exc_info.value.status_code == 401
    assert "auth_date" in exc_info.value.detail


def test_validate_telegram_init_data_rejects_invalid_auth_date(clean_env):
    os.environ["ENV"] = "production"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["TELEGRAM_BOT_TOKEN"] = "token"
    os.environ["ADMIN_USERNAMES"] = "admin"
    os.environ["DEV_MODE"] = "false"

    import server
    importlib.reload(server)

    init_data = _signed_init_data("token", {
        "auth_date": "not-a-timestamp",
        "user": json.dumps({"id": 1, "username": "admin", "first_name": "Admin"}, separators=(",", ":")),
    })

    with pytest.raises(HTTPException) as exc_info:
        server.validate_telegram_init_data(init_data)

    assert exc_info.value.status_code == 401
    assert "auth_date" in exc_info.value.detail

@pytest.mark.asyncio
async def test_require_admin():
    import server
    user = server.TelegramUser(user_id=1, username="test", first_name="Test", is_admin=False)
    with pytest.raises(HTTPException) as exc_info:
        await server.require_admin(user)
    assert exc_info.value.status_code == 403

@pytest.mark.asyncio
async def test_duplicate_review(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.reviews_col.insert_one = AsyncMock(side_effect=server.DuplicateKeyError("Duplicate"))
    server.releases_col.find_one = AsyncMock(return_value={"id": "1", "name": "Test"})
    server.reviews_col.find_one = AsyncMock(return_value=None)

    user = server.TelegramUser(user_id=1, username="test", first_name="Test", is_admin=False)
    rev = server.Review(id="1", relId="1", text="a"*30, rating=5, baseRating=5, criteria={}, objectiveRating=5.0)

    with pytest.raises(HTTPException) as exc_info:
        await server.add_review(rev, user)

    assert exc_info.value.status_code == 409

@pytest.mark.asyncio
async def test_add_release_records_sync_event(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.update_one = AsyncMock(return_value=SimpleNamespace(upserted_id=None))
    server.sync_events_col.insert_one = AsyncMock()
    server.next_sync_token = lambda: 123
    server.now_ms = lambda: 456.0
    server.sync_event_expiry = lambda: "EXPIRY"

    admin = server.TelegramUser(user_id=1, username="admin", first_name="Admin", is_admin=True)
    rel = server.Release(id="rel-1", name="Album", artist="Artist", link="https://example.com/album")

    result = await server.add_release(rel, admin)

    assert result == {"status": "ok", "syncToken": 123}
    server.releases_col.update_one.assert_awaited_once()
    stored = server.releases_col.update_one.await_args.args[1]["$set"]
    assert stored["timestamp"] == 456.0
    assert stored["updatedAt"] == 456.0
    assert stored["syncToken"] == 123
    server.sync_events_col.insert_one.assert_awaited_once_with({
        "kind": "release_upserted",
        "releaseId": "rel-1",
        "syncToken": 123,
        "timestamp": 456.0,
        "expireAt": "EXPIRY",
    })


@pytest.mark.asyncio
async def test_delete_release_records_sync_event(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.delete_one = AsyncMock()
    server.reviews_col.find = lambda query=None: FakeCursor([])
    server.reviews_col.delete_many = AsyncMock()
    server.likes_col.delete_many = AsyncMock()
    server.review_reactions_col.delete_many = AsyncMock()
    server.sync_events_col.insert_one = AsyncMock()
    server.next_sync_token = lambda: 222
    server.now_ms = lambda: 333.0
    server.sync_event_expiry = lambda: "EXPIRY"

    admin = server.TelegramUser(user_id=1, username="admin", first_name="Admin", is_admin=True)
    result = await server.delete_release("rel-1", admin)

    assert result == {"status": "ok", "syncToken": 222}
    server.releases_col.delete_one.assert_awaited_once_with({"id": "rel-1"})
    server.reviews_col.delete_many.assert_awaited_once_with({"relId": "rel-1"})
    server.likes_col.delete_many.assert_awaited_once_with({"releaseId": "rel-1"})
    server.sync_events_col.insert_one.assert_awaited_once_with({
        "kind": "release_deleted",
        "releaseId": "rel-1",
        "syncToken": 222,
        "timestamp": 333.0,
        "expireAt": "EXPIRY",
    })


@pytest.mark.asyncio
async def test_sync_releases_initial_snapshot(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.find = lambda query=None: FakeCursor([
        {"_id": "mongo-id", "id": "rel-1", "name": "Album", "syncToken": 10},
        {"id": "rel-2", "name": "Second", "syncToken": 20},
    ])
    server.next_sync_token = lambda: 30

    result = await server.sync_releases(since=0, limit=100)

    assert result["cursor"] == 20
    assert result["serverTime"] == 30
    assert result["deletedReleaseIds"] == []
    assert result["releases"][0] == {"id": "rel-1", "name": "Album", "syncToken": 10}


@pytest.mark.asyncio
async def test_sync_releases_incremental_changes(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    events = [
        {"kind": "release_upserted", "releaseId": "rel-1", "syncToken": 101},
        {"kind": "release_deleted", "releaseId": "rel-2", "syncToken": 102},
    ]

    def fake_release_find(query=None):
        assert query == {"id": {"$in": ["rel-1"]}}
        return FakeCursor([{"_id": "mongo-id", "id": "rel-1", "name": "Album", "syncToken": 101}])

    server.sync_events_col.find = lambda query=None: FakeCursor(events)
    server.releases_col.find = fake_release_find
    server.next_sync_token = lambda: 200

    result = await server.sync_releases(since=100, limit=100)

    assert result["cursor"] == 102
    assert result["serverTime"] == 200
    assert result["deletedReleaseIds"] == ["rel-2"]
    assert result["releases"] == [{"id": "rel-1", "name": "Album", "syncToken": 101}]


@pytest.mark.asyncio
async def test_sync_releases_long_poll_waits_for_new_events(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    calls = {"count": 0}

    def fake_event_find(query=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return SlowFakeCursor([])
        return SlowFakeCursor([
            {"kind": "release_upserted", "releaseId": "rel-1", "syncToken": 101},
        ])

    def fake_release_find(query=None):
        assert query == {"id": {"$in": ["rel-1"]}}
        return FakeCursor([{"id": "rel-1", "name": "Album", "syncToken": 101}])

    server.SYNC_POLL_INTERVAL_MS = 1
    server.sync_events_col.find = fake_event_find
    server.releases_col.find = fake_release_find
    server.next_sync_token = lambda: 200

    result = await server.sync_releases(since=100, limit=100, waitMs=100)

    assert calls["count"] == 2
    assert result["cursor"] == 101
    assert result["releases"] == [{"id": "rel-1", "name": "Album", "syncToken": 101}]


@pytest.mark.asyncio
async def test_sync_releases_long_poll_times_out_empty(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.SYNC_POLL_INTERVAL_MS = 1
    server.sync_events_col.find = lambda query=None: SlowFakeCursor([])
    server.next_sync_token = lambda: 200

    result = await server.sync_releases(since=100, limit=100, waitMs=1)

    assert result["cursor"] == 100
    assert result["releases"] == []
    assert result["deletedReleaseIds"] == []


@pytest.mark.asyncio
async def test_get_all_data_returns_sync_cursor_for_resume(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.find = lambda query=None: FakeCursor([
        {"_id": "mongo-id", "id": "rel-1", "name": "Album", "syncToken": 100},
    ])
    server.reviews_col.find = lambda query=None: FakeCursor([])
    server.sync_events_col.find = lambda query=None: FakeCursor([
        {"kind": "release_deleted", "releaseId": "rel-2", "syncToken": 150},
    ])
    server.review_reactions_col.aggregate = lambda pipeline: FakeCursor([])

    result = await server.get_all_data(FakeRequest())

    assert result["syncCursor"] == 150
    assert result["releases"] == [{"id": "rel-1", "name": "Album", "syncToken": 100}]


@pytest.mark.asyncio
async def test_sync_releases_catches_up_after_polling_pause(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    events = [
        {"kind": "release_upserted", "releaseId": "rel-2", "syncToken": 151},
        {"kind": "release_upserted", "releaseId": "rel-3", "syncToken": 152},
    ]

    def fake_release_find(query=None):
        assert query == {"id": {"$in": ["rel-2", "rel-3"]}}
        return FakeCursor([
            {"_id": "mongo-id-2", "id": "rel-2", "name": "Second", "syncToken": 151},
            {"_id": "mongo-id-3", "id": "rel-3", "name": "Third", "syncToken": 152},
        ])

    server.sync_events_col.find = lambda query=None: FakeCursor(events)
    server.releases_col.find = fake_release_find
    server.next_sync_token = lambda: 200

    result = await server.sync_releases(since=150, limit=100)

    assert result["cursor"] == 152
    assert [release["id"] for release in result["releases"]] == ["rel-2", "rel-3"]
    assert result["deletedReleaseIds"] == []


@pytest.mark.asyncio
async def test_toggle_like_requires_existing_release(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.find_one = AsyncMock(return_value=None)
    server.likes_col.update_one = AsyncMock()

    user = server.TelegramUser(user_id=1, username="test", first_name="Test", is_admin=False)
    req = server.LikeReq(releaseId="missing", isLike=True)

    with pytest.raises(HTTPException) as exc_info:
        await server.toggle_like(req, user)

    assert exc_info.value.status_code == 404
    server.likes_col.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_delete_all_reviews_by_author_normalizes_username(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.reviews_col.find = lambda query=None: FakeCursor([])
    server.reviews_col.delete_many = AsyncMock(return_value=SimpleNamespace(deleted_count=2))
    server.sync_events_col.insert_one = AsyncMock()

    admin = server.TelegramUser(user_id=1, username="admin", first_name="Admin", is_admin=True)
    result = await server.delete_all_reviews_by_author("@TargetUser", admin)

    assert result == {"status": "ok", "deleted": 2}
    server.reviews_col.delete_many.assert_awaited_once_with({"authorUsername": "targetuser"})


def test_is_safe_public_url_rejects_credentials(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    assert server.is_safe_public_url("https://user:pass@example.com/path") is False


def test_parse_yandex_music_url_extracts_album_and_track_ids(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    assert server.parse_yandex_music_url("https://music.yandex.ru/album/123/track/456") == {
        "album_id": "123",
        "track_id": "456",
    }
    assert server.parse_yandex_music_url("https://music.yandex.ru/album/123?track=789") == {
        "album_id": "123",
        "track_id": "789",
    }


def test_yandex_album_to_release_uses_real_metadata(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    result = server.yandex_album_to_release({
        "title": "Альбом",
        "artists": [{"name": "Первый"}, {"name": "Второй"}],
        "labels": [{"name": "Label"}],
        "coverUri": "avatars.yandex.net/get-music-content/1/cover/%%",
        "genre": "rap",
    })

    assert result == {
        "artist": "Первый, Второй",
        "name": "Альбом",
        "img": "https://avatars.yandex.net/get-music-content/1/cover/1000x1000",
        "genre": "Рэп",
    }


def test_yandex_track_to_release_uses_album_cover_fallback(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    result = server.yandex_track_to_release({
        "title": "Трек",
        "artists": [{"name": "Исполнитель"}],
        "albums": [{
            "title": "Альбом",
            "coverUri": "avatars.yandex.net/get-music-content/2/cover/%%",
        }],
    })

    assert result == {
        "artist": "Исполнитель",
        "name": "Трек",
        "img": "https://avatars.yandex.net/get-music-content/2/cover/1000x1000",
        "genre": "",
    }


@pytest.mark.asyncio
async def test_parse_link_prefers_yandex_api_metadata(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.is_safe_public_url = lambda url: True
    server.get_yandex_music_release = AsyncMock(return_value={
        "artist": "Исполнитель",
        "name": "Альбом",
        "img": "https://avatars.yandex.net/cover/1000x1000",
        "genre": "Рэп",
    })
    server.get_metadata_from_page = AsyncMock()
    server.ai_extract_release = AsyncMock()

    admin = server.TelegramUser(user_id=1, username="admin", first_name="Admin", is_admin=True)
    result = await server.parse_link(server.LinkRequest(link="https://music.yandex.ru/album/123"), admin)

    assert result["artist"] == "Исполнитель"
    assert result["img"] == "https://avatars.yandex.net/cover/1000x1000"
    server.get_metadata_from_page.assert_not_called()
    server.ai_extract_release.assert_not_called()


@pytest.mark.asyncio
async def test_parse_link_rejects_yandex_when_api_metadata_missing(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.is_safe_public_url = lambda url: True
    server.get_yandex_music_release = AsyncMock(return_value=None)
    server.get_metadata_from_page = AsyncMock()
    server.ai_extract_release = AsyncMock()

    admin = server.TelegramUser(user_id=1, username="admin", first_name="Admin", is_admin=True)
    with pytest.raises(HTTPException) as exc_info:
        await server.parse_link(server.LinkRequest(link="https://music.yandex.ru/album/123"), admin)

    assert exc_info.value.status_code == 502
    server.get_metadata_from_page.assert_not_called()
    server.ai_extract_release.assert_not_called()


@pytest.mark.asyncio
async def test_ai_extract_release_fallback_parses_common_title(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.client_ai = None

    result = await server.ai_extract_release(
        "Artist Name - Album Name | Spotify",
        "https://open.spotify.com/album/123",
        "",
    )

    assert result == {"artist": "Artist Name", "name": "Album Name", "genre": ""}


@pytest.mark.asyncio
async def test_parse_link_rejects_missing_page_metadata(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.is_safe_public_url = lambda url: True
    server.get_yandex_music_release = AsyncMock(return_value=None)
    server.get_metadata_from_page = AsyncMock(return_value=("", "", ""))
    server.ai_extract_release = AsyncMock()

    admin = server.TelegramUser(user_id=1, username="admin", first_name="Admin", is_admin=True)
    with pytest.raises(HTTPException) as exc_info:
        await server.parse_link(server.LinkRequest(link="https://example.com/unknown"), admin)

    assert exc_info.value.status_code == 422
    server.ai_extract_release.assert_not_called()


def test_parse_ai_json_tolerates_wrapped_json(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    assert server.parse_ai_json('```json\n{"artist":"A","name":"B"}\n```') == {"artist": "A", "name": "B"}


def test_normalize_release_result_keeps_raw_ai_payload(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    # Данные хранятся «сырыми»; экранирование выполняется только на выводе (фронтенд).
    result = server.normalize_release_result(
        {"artist": "<b>A</b>", "name": "Track", "genre": "hip hop"},
        "",
        "https://example.com/release",
        "",
    )

    assert result == {"artist": "<b>A</b>", "name": "Track", "genre": "Хип-хоп"}


def test_ai_fallback_keeps_raw_title_guess(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    result = server.normalize_release_result(
        {},
        "<b>Artist</b> - <script>Album</script>",
        "https://example.com/release",
        "",
    )

    assert result["artist"] == "<b>Artist</b>"
    assert result["name"] == "<script>Album</script>"


@pytest.mark.asyncio
async def test_parse_link_uses_async_ai_result(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.is_safe_public_url = lambda url: True
    server.get_metadata_from_page = AsyncMock(return_value=("Artist - Album", "https://img.example/cover.jpg", "rap"))
    server.ai_extract_release = AsyncMock(return_value={"artist": "Artist", "name": "Album", "genre": "Рэп"})

    admin = server.TelegramUser(user_id=1, username="admin", first_name="Admin", is_admin=True)
    result = await server.parse_link(server.LinkRequest(link="https://example.com/release"), admin)

    assert result == {
        "artist": "Artist",
        "name": "Album",
        "genre": "Рэп",
        "img": "https://img.example/cover.jpg",
    }
    server.ai_extract_release.assert_awaited_once_with("Artist - Album", "https://example.com/release", "Рэп")


def test_normalize_criteria_clamps_and_fills(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    result = server.normalize_criteria({"sound": 99, "production": -3, "bogus": 7})

    assert set(result.keys()) == set(server.CRITERIA_KEYS)
    assert result["sound"] == 10       # clamped to max
    assert result["production"] == 1   # clamped to min
    assert result["originality"] == 5  # default for missing
    assert "bogus" not in result


def test_compute_review_ratings_is_server_authoritative(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    criteria = server.normalize_criteria({k: 5 for k in server.CRITERIA_KEYS})
    objective, final = server.compute_review_ratings(10, criteria)

    assert objective == 5.0
    assert final == 7.5


@pytest.mark.asyncio
async def test_add_review_ignores_client_rating(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.find_one = AsyncMock(return_value={"id": "1", "name": "Test"})
    server.reviews_col.find_one = AsyncMock(return_value=None)
    server.reviews_col.insert_one = AsyncMock()
    server.sync_events_col.insert_one = AsyncMock()

    user = server.TelegramUser(user_id=1, username="test", first_name="Test", is_admin=False)
    # Клиент присылает накрученный rating=1.0 и objectiveRating=1.0 — сервер их игнорирует.
    rev = server.Review(
        id="1", relId="1", text="a" * 30, rating=1.0, baseRating=10,
        criteria={k: 5 for k in server.CRITERIA_KEYS}, objectiveRating=1.0,
    )

    await server.add_review(rev, user)

    stored = server.reviews_col.insert_one.await_args.args[0]
    assert stored["objectiveRating"] == 5.0
    assert stored["rating"] == 7.5
    assert stored["authorIsAdmin"] is False


@pytest.mark.asyncio
async def test_add_release_notifies_subscribers_on_insert(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.update_one = AsyncMock(return_value=SimpleNamespace(upserted_id="new-id"))
    server.sync_events_col.insert_one = AsyncMock()
    server.send_release_notifications = AsyncMock()

    admin = server.TelegramUser(user_id=1, username="admin", first_name="Admin", is_admin=True)
    rel = server.Release(id="rel-1", name="Album", artist="Artist", link="https://example.com/album")

    await server.add_release(rel, admin)
    await asyncio.sleep(0)  # дать фоновой задаче запуститься

    server.send_release_notifications.assert_called_once()


@pytest.mark.asyncio
async def test_add_release_skips_notification_on_update(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    # upserted_id=None → это правка существующего релиза, рассылки быть не должно.
    server.releases_col.update_one = AsyncMock(return_value=SimpleNamespace(upserted_id=None))
    server.sync_events_col.insert_one = AsyncMock()
    server.send_release_notifications = AsyncMock()

    admin = server.TelegramUser(user_id=1, username="admin", first_name="Admin", is_admin=True)
    rel = server.Release(id="rel-1", name="Album", artist="Artist", link="https://example.com/album")

    await server.add_release(rel, admin)
    await asyncio.sleep(0)

    server.send_release_notifications.assert_not_called()


@pytest.mark.asyncio
async def test_set_notifications_upserts_preference(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.notif_subscribers_col.update_one = AsyncMock()
    server.now_ms = lambda: 999.0

    user = server.TelegramUser(user_id=7, username="fan", first_name="Fan", is_admin=False)
    result = await server.set_notifications(server.SubscribeReq(enabled=False), user)

    assert result == {"status": "ok", "enabled": False}
    server.notif_subscribers_col.update_one.assert_awaited_once()
    args, kwargs = server.notif_subscribers_col.update_one.await_args
    assert args[0] == {"userId": 7}
    assert args[1]["$set"]["enabled"] is False
    assert args[1]["$set"]["chatId"] == 7
    assert kwargs["upsert"] is True


@pytest.mark.asyncio
async def test_send_release_notifications_messages_enabled_subscribers(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"

    import server
    importlib.reload(server)

    server.notif_subscribers_col.find = lambda query=None: FakeCursor([
        {"userId": 11, "chatId": 11, "enabled": True},
        {"userId": 22, "chatId": 22, "enabled": True},
    ])

    sent = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            sent.append(json)
            return SimpleNamespace(status_code=200)

    server.httpx.AsyncClient = lambda *a, **k: FakeClient()

    await server.send_release_notifications({"id": "rel-1", "artist": "A", "name": "B"})

    assert len(sent) == 2
    assert {msg["chat_id"] for msg in sent} == {11, 22}
    assert all("A — B" in msg["text"] for msg in sent)


@pytest.mark.asyncio
async def test_send_release_notifications_noop_without_token(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"
    if "TELEGRAM_BOT_TOKEN" in os.environ:
        del os.environ["TELEGRAM_BOT_TOKEN"]

    import server
    importlib.reload(server)

    def fail_find(query=None):
        raise AssertionError("subscribers must not be queried without a bot token")

    server.notif_subscribers_col.find = fail_find

    # Не должно бросить исключение и не должно трогать БД.
    await server.send_release_notifications({"id": "rel-1", "artist": "A", "name": "B"})


@pytest.mark.asyncio
async def test_get_all_data_includes_notifications_enabled(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.find = lambda query=None: FakeCursor([])
    server.reviews_col.find = lambda query=None: FakeCursor([])
    server.sync_events_col.find = lambda query=None: FakeCursor([])
    server.review_reactions_col.aggregate = lambda pipeline: FakeCursor([])

    result = await server.get_all_data(FakeRequest())

    assert result["currentUser"]["notificationsEnabled"] is True


def test_client_rate_key_prefers_telegram_user(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    init_data = urlencode({"user": json.dumps({"id": 555, "username": "u"})})
    tg_request = SimpleNamespace(
        headers={"X-Telegram-Init-Data": init_data, "X-Forwarded-For": "9.9.9.9"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    assert server.client_rate_key(tg_request) == "user:555"

    # Без initData — ключуем по первому IP из X-Forwarded-For.
    ip_request = SimpleNamespace(
        headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    assert server.client_rate_key(ip_request) == "ip:1.2.3.4"


@pytest.mark.asyncio
async def test_get_all_data_marks_admin_review_authors(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.find = lambda query=None: FakeCursor([])
    server.reviews_col.find = lambda query=None: FakeCursor([
        {"id": "r1", "authorUsername": "admin", "text": "x"},
        {"id": "r2", "authorUsername": "bob", "text": "y"},
    ])
    server.sync_events_col.find = lambda query=None: FakeCursor([])
    server.review_reactions_col.aggregate = lambda pipeline: FakeCursor([{"_id": "r1", "count": 3}])

    result = await server.get_all_data(FakeRequest())

    by_id = {r["id"]: r for r in result["reviews"]}
    assert by_id["r1"]["authorIsAdmin"] is True
    assert by_id["r2"]["authorIsAdmin"] is False
    assert by_id["r1"]["reactionCount"] == 3
    assert by_id["r2"]["reactionCount"] == 0
    assert "adminUsernames" not in result


@pytest.mark.asyncio
async def test_react_to_review_adds_reaction(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.reviews_col.find_one = AsyncMock(return_value={"id": "rv-1"})
    server.review_reactions_col.update_one = AsyncMock()
    server.review_reactions_col.delete_one = AsyncMock()
    server.review_reactions_col.count_documents = AsyncMock(return_value=4)

    user = server.TelegramUser(user_id=7, username="fan", first_name="Fan", is_admin=False)
    result = await server.react_to_review("rv-1", server.ReactReq(reacted=True), user)

    assert result == {"status": "ok", "reactionCount": 4}
    server.review_reactions_col.update_one.assert_awaited_once()
    server.review_reactions_col.delete_one.assert_not_called()


@pytest.mark.asyncio
async def test_react_to_review_removes_reaction(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.reviews_col.find_one = AsyncMock(return_value={"id": "rv-1"})
    server.review_reactions_col.update_one = AsyncMock()
    server.review_reactions_col.delete_one = AsyncMock()
    server.review_reactions_col.count_documents = AsyncMock(return_value=0)

    user = server.TelegramUser(user_id=7, username="fan", first_name="Fan", is_admin=False)
    result = await server.react_to_review("rv-1", server.ReactReq(reacted=False), user)

    assert result == {"status": "ok", "reactionCount": 0}
    server.review_reactions_col.delete_one.assert_awaited_once()
    server.review_reactions_col.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_react_to_review_missing_review(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.reviews_col.find_one = AsyncMock(return_value=None)
    server.review_reactions_col.count_documents = AsyncMock()

    user = server.TelegramUser(user_id=7, username="fan", first_name="Fan", is_admin=False)
    with pytest.raises(HTTPException) as exc_info:
        await server.react_to_review("missing", server.ReactReq(reacted=True), user)

    assert exc_info.value.status_code == 404
    server.review_reactions_col.count_documents.assert_not_called()


@pytest.mark.asyncio
async def test_add_review_records_review_sync_event(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.find_one = AsyncMock(return_value={"id": "rel-1", "name": "Test"})
    server.reviews_col.find_one = AsyncMock(return_value=None)
    server.reviews_col.insert_one = AsyncMock()
    server.sync_events_col.insert_one = AsyncMock()
    server.next_sync_token = lambda: 777

    user = server.TelegramUser(user_id=1, username="test", first_name="Test", is_admin=False)
    rev = server.Review(id="rv-1", relId="rel-1", text="a" * 30, baseRating=5,
                        criteria={k: 5 for k in server.CRITERIA_KEYS})

    result = await server.add_review(rev, user)

    assert result["syncToken"] == 777
    event = server.sync_events_col.insert_one.await_args.args[0]
    assert event["kind"] == "review_added"
    assert event["reviewId"] == "rv-1"
    assert event["relId"] == "rel-1"
    assert event["syncToken"] == 777


@pytest.mark.asyncio
async def test_delete_review_records_review_sync_event(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.reviews_col.find_one = AsyncMock(return_value={"id": "rv-1", "relId": "rel-1", "authorId": 1})
    server.reviews_col.delete_one = AsyncMock()
    server.review_reactions_col.delete_many = AsyncMock()
    server.sync_events_col.insert_one = AsyncMock()
    server.next_sync_token = lambda: 888

    user = server.TelegramUser(user_id=1, username="test", first_name="Test", is_admin=False)
    result = await server.delete_review("rv-1", user)

    assert result == {"status": "ok", "syncToken": 888}
    event = server.sync_events_col.insert_one.await_args.args[0]
    assert event["kind"] == "review_deleted"
    assert event["reviewId"] == "rv-1"
    assert event["relId"] == "rel-1"


@pytest.mark.asyncio
async def test_sync_releases_returns_review_changes(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    events = [
        {"kind": "review_added", "reviewId": "rv-1", "relId": "rel-1", "syncToken": 301},
        {"kind": "review_deleted", "reviewId": "rv-2", "relId": "rel-1", "syncToken": 302},
    ]

    def fake_review_find(query=None):
        assert query == {"id": {"$in": ["rv-1"]}}
        return FakeCursor([{"_id": "m", "id": "rv-1", "relId": "rel-1", "authorUsername": "admin"}])

    server.sync_events_col.find = lambda query=None: FakeCursor(events)
    server.reviews_col.find = fake_review_find
    server.next_sync_token = lambda: 400

    result = await server.sync_releases(since=300, limit=100)

    assert result["cursor"] == 302
    assert result["deletedReviewIds"] == ["rv-2"]
    assert result["reviews"][0]["id"] == "rv-1"
    assert result["reviews"][0]["authorIsAdmin"] is True
    assert result["releases"] == []
