import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.api.auth import validate_init_data


def signed(token, when):
    data = {"auth_date": str(when), "user": json.dumps({"id": 123})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_signed_init_data_and_expiry():
    now = int(time.time())
    assert validate_init_data(signed("token", now), "token", 60)["id"] == 123
    with pytest.raises(ValueError):
        validate_init_data(signed("token", now - 100), "token", 60)
    with pytest.raises(ValueError):
        validate_init_data(signed("token", now), "wrong", 60)
