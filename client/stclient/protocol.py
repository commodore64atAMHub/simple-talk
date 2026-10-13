"""Wire protocol shared by client and server.

Kept intentionally self-contained (no external deps) so the Nuitka
onefile build stays dependency free.
"""

import json

PROTOCOL_VERSION = 1
MAX_NICK = 24
MAX_BODY = 2048
DEFAULT_PORT = 8765
DEFAULT_HTTP_PORT = 8080
DEFAULT_HTTP_URL = f"http://127.0.0.1:{DEFAULT_HTTP_PORT}"


def encode(payload: dict) -> bytes:
    """Serialize a message dict to a newline-terminated UTF-8 blob."""
    msg = dict(payload)
    msg.setdefault("v", PROTOCOL_VERSION)
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def decode(raw: bytes) -> dict:
    """Parse a raw line back into a message dict."""
    return json.loads(raw.decode("utf-8"))