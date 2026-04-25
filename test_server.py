import pytest
import os
import importlib
from fastapi import HTTPException
import asyncio
from unittest.mock import AsyncMock, patch

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
