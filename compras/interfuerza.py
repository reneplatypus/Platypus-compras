"""Read-only InterFuerza v4 client.

Only "class":"GET" requests are allowed here; writes will get their own client, gated separately.
InterFuerza blocks the IP for an hour above 20 requests / 10 s, so calls are serialized and spaced.

Two quirks: the token header name is case-sensitive (exactly X-IFX-Token, which urllib would rewrite to
X-ifx-token), and Cloudflare in front of the API rejects Python's default User-Agent (error 1010).
"""
import http.client
import json
import os
import time
from pathlib import Path

HOST, PATH = "app.interfuerza.com", "/api/v4/"
TOKEN_FILE = Path(os.environ.get("INTERFUERZA_TOKEN_FILE", "/opt/interfuerza/interfuerzatoken"))
MIN_INTERVAL = 1.0   # seconds between requests: 10 per 10 s, under the 12 we allow ourselves
PAGE_SIZE = 25


class InterFuerzaError(RuntimeError):
    pass


class Client:
    def __init__(self, token: str | None = None):
        self._token = token or TOKEN_FILE.read_text().strip()
        self._last = 0.0

    def get(self, action: str, **params) -> dict:
        wait = self._last + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        body = json.dumps({"class": "GET", "action": action, **params})
        conn = http.client.HTTPSConnection(HOST, timeout=60)
        try:
            conn.request("POST", PATH, body=body, headers={
                "X-IFX-Token": self._token,
                "Content-Type": "application/json",
                "User-Agent": "platypus-compras/0.1",
            })
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
            self._last = time.monotonic()
        if resp.status != 200:
            raise InterFuerzaError(f"{action}: HTTP {resp.status} {raw[:200]!r}")
        try:
            return json.loads(raw)
        except ValueError:
            raise InterFuerzaError(f"{action}: respuesta no JSON ({len(raw)} bytes): {raw[:200]!r}") from None

    def all_pages(self, action: str, key: str, **params):
        """Yield every record of a paged list action, one page at a time."""
        page = 1
        while True:
            records = records_of(self.get(action, page=str(page), **params), key)
            yield from records
            if len(records) < PAGE_SIZE:
                return
            page += 1


def records_of(data: dict, key: str) -> list:
    if key in data:
        return data[key] or []
    inner = data.get("response")
    if isinstance(inner, dict) and key in inner:
        return inner[key] or []
    return []
