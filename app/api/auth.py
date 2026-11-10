import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, token: str, max_age: int) -> dict:
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("duplicate fields")
    received = values.pop("hash", "")
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError("invalid signature")
    auth_date = int(values["auth_date"])
    age = time.time() - auth_date
    if age < 0 or age > max_age:
        raise ValueError("expired init data")
    user = json.loads(values["user"])
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise ValueError("invalid user")
    return user
