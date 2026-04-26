import pytest
import os
import importlib
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
