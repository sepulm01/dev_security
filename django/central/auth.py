import hashlib
import hmac
import secrets
import time

from django.core import signing
from django.utils import timezone

SIGNATURE_TOLERANCE_SECONDS = 300
TOKEN_SALT = "central.node.token"


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def encrypt_token(token: str) -> str:
    return signing.dumps(token, salt=TOKEN_SALT)


def decrypt_token(encrypted: str) -> str | None:
    try:
        return signing.loads(encrypted, salt=TOKEN_SALT)
    except signing.BadSignature:
        return None


def build_signature(slug: str, token: str, timestamp: str, body: bytes) -> str:
    payload = f"{slug}.{timestamp}.".encode() + body
    return hmac.new(token.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(node, timestamp: str, body: bytes, provided: str) -> bool:
    if not node.token_encrypted or not provided:
        return False
    token = decrypt_token(node.token_encrypted)
    if not token:
        return False
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False
    now = timezone.now().timestamp()
    if abs(now - ts) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = build_signature(node.slug, token, timestamp, body)
    return hmac.compare_digest(expected, provided)
