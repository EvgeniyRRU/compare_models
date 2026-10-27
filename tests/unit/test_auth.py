import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from support_bot.api.auth import verify_init_data

TOKEN = "123456:example"


def signed(*, auth_date=1000, user=None, **extra):
    fields = {
        "auth_date": str(auth_date),
        "user": json.dumps(user if user is not None else {"id": 42, "first_name": "Анна"}),
        **extra,
    }
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_signature_authenticates_user_and_includes_signature_field():
    assert (
        verify_init_data(signed(signature="telegram-signature"), TOKEN, 3600, now=1000)["id"] == 42
    )


@pytest.mark.parametrize("raw", ["", "hash=wrong", "auth_date=1000&hash=" + "0" * 64])
def test_invalid_signature_rejected(raw):
    with pytest.raises(ValueError):
        verify_init_data(raw, TOKEN, 3600, now=1000)


def test_modified_and_duplicate_fields_rejected():
    raw = signed()
    for forged in [raw.replace("auth_date=1000", "auth_date=1001"), raw + "&auth_date=1000"]:
        with pytest.raises(ValueError):
            verify_init_data(forged, TOKEN, 3600, now=1000)


@pytest.mark.parametrize("date", [6399, 10031])
def test_expired_or_future_data_rejected(date):
    with pytest.raises(ValueError):
        verify_init_data(signed(auth_date=date), TOKEN, 3600, now=10000)


@pytest.mark.parametrize("date", [6400, 10030])
def test_age_and_clock_skew_boundaries_accepted(date):
    assert verify_init_data(signed(auth_date=date), TOKEN, 3600, now=10000)["id"] == 42


@pytest.mark.parametrize(
    "user",
    [
        {"id": True, "first_name": "a"},
        {"id": -1, "first_name": "a"},
        {"id": 42},
        [],
        {"id": 42, "first_name": "a", "username": 1},
    ],
)
def test_invalid_telegram_identity_rejected(user):
    with pytest.raises(ValueError):
        verify_init_data(signed(user=user), TOKEN, 3600, now=1000)
