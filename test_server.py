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

    server.releases_col.update_one = AsyncMock()
    server.sync_events_col.insert_one = AsyncMock()
    server.next_sync_token = lambda: 123
    server.now_ms = lambda: 456.0

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
    })


@pytest.mark.asyncio
async def test_delete_release_records_sync_event(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    server.releases_col.delete_one = AsyncMock()
    server.reviews_col.delete_many = AsyncMock()
    server.likes_col.delete_many = AsyncMock()
    server.sync_events_col.insert_one = AsyncMock()
    server.next_sync_token = lambda: 222
    server.now_ms = lambda: 333.0

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

    server.reviews_col.delete_many = AsyncMock(return_value=SimpleNamespace(deleted_count=2))

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


def test_normalize_release_result_sanitizes_ai_payload(clean_env):
    os.environ["ENV"] = "test"
    os.environ["MONGO_URL"] = "mongodb://localhost"
    os.environ["ADMIN_USERNAMES"] = "admin"

    import server
    importlib.reload(server)

    result = server.normalize_release_result(
        {"artist": "<b>A</b>", "name": "Track", "genre": "hip hop"},
        "",
        "https://example.com/release",
        "",
    )

    assert result == {"artist": "&lt;b&gt;A&lt;/b&gt;", "name": "Track", "genre": "Хип-хоп"}


def test_ai_fallback_sanitizes_title_guess(clean_env):
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

    assert result["artist"] == "&lt;b&gt;Artist&lt;/b&gt;"
    assert result["name"] == "&lt;script&gt;Album&lt;/script&gt;"


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
