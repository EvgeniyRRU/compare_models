# tests/unit/test_initdata.py
import pytest

from app.api.auth import AuthError, validate_init_data
from tests.fakes import make_init_data

TOKEN = "123456789:AAHtesttesttesttesttesttesttesttest1"


def test_valid_init_data():
    data = make_init_data(111, TOKEN, username="user111")
    user = validate_init_data(data, TOKEN, 86400)
    assert user["id"] == 111
    assert user["username"] == "user111"


def test_tampered_hash_rejected():
    data = make_init_data(111, TOKEN)
    user_part, _ = data.rsplit("&hash=", 1)
    with pytest.raises(AuthError):
        validate_init_data(user_part + "&hash=" + "0" * 64, TOKEN, 86400)


def test_tampered_payload_rejected():
    data = make_init_data(111, TOKEN)
    user_part, hash_part = data.rsplit("&hash=", 1)
    # подменяем user: меняем id
    needle = "id%22%3A111"
    tampered = user_part.replace(needle, "id%22%3A999", 1) if needle in user_part else user_part
    with pytest.raises(AuthError):
        validate_init_data(tampered + f"&hash={hash_part}", TOKEN, 86400)


def test_stale_auth_date_rejected():
    data = make_init_data(111, TOKEN, age_seconds=90000)
    with pytest.raises(AuthError):
        validate_init_data(data, TOKEN, 86400)


def test_wrong_token_rejected():
    data = make_init_data(111, TOKEN)
    with pytest.raises(AuthError):
        validate_init_data(data, "999999999:AABotherothertokenothertokenother1", 86400)


def test_missing_hash_rejected():
    with pytest.raises(AuthError):
        validate_init_data("user=%7B%7D", TOKEN, 86400)
