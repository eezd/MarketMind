import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from marketmind.errors import ApiError


def encode_cursor(scope: str, timestamp: datetime, identifier: UUID) -> str:
    payload = json.dumps(
        {"v": 1, "s": scope, "t": timestamp.isoformat(), "id": str(identifier)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str, scope: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "s", "t", "id"}
            or type(payload["v"]) is not int
            or payload["v"] != 1
            or payload["s"] != scope
        ):
            raise ValueError("Cursor scope or version mismatch")
        timestamp = datetime.fromisoformat(payload["t"])
        if timestamp.tzinfo is None:
            raise ValueError("Cursor timestamp requires timezone")
        return timestamp, UUID(payload["id"])
    except (ValueError, TypeError, KeyError, AttributeError, binascii.Error, UnicodeDecodeError):
        raise ApiError(400, "invalid_cursor", "Cursor is invalid for this query") from None
