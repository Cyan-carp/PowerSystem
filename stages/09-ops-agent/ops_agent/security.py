"""Versioned, canonical UTF-8 HMAC messages; credentials never become task data."""
import base64
import hashlib
import hmac
import json
import time


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sign(value, key):
    payload = base64.urlsafe_b64encode(canonical(value)).decode().rstrip("=")
    signature = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def verify(token, key, audience):
    if len(key) < 32 or len(token) > 65536:
        raise ValueError("invalid_signature")
    try:
        payload, signature = token.split(".")
        expected = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        now = int(time.time())
        if value["aud"] != audience or value["kid"] != "ma-v1" or type(value["exp"]) is not int or type(value["iat"]) is not int or value["exp"] <= now or value["iat"] > now + 30 or value["exp"] - value["iat"] > 600:
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ValueError("invalid_signature") from None


def claims(actor, session_id, trace_id, request_hash):
    now = int(time.time())
    return dict(aud="ma-ops", kid="ma-v1", iat=now, exp=now + 450,
                user_id=actor["user_id"], role=actor["role"], session_id=str(session_id),
                trace_id=str(trace_id), request_hash=request_hash)
