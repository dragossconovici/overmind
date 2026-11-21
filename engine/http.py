"""Hardened HTTP client.

Security rules enforced here, for every outgoing request:
- HTTPS only, and only towards an explicit allowlist of hosts (redirects included).
- Hard timeout and maximum response size (no memory exhaustion from a hostile/broken API).
- Error messages never contain the URL, query string or headers, so API keys
  can't leak into logs or into the published JSON. Every message also passes
  through `redact()` as a second line of defence.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 8_000_000
USER_AGENT = "Mozilla/5.0 (compatible; Overmind/0.1; personal research tool)"

ALLOWED_HOSTS = frozenset({
    "api.twelvedata.com",
    "query1.finance.yahoo.com",
    "query2.finance.yahoo.com",
    "api.stlouisfed.org",
    "publicreporting.cftc.gov",
    "generativelanguage.googleapis.com",
    "nfs.faireconomy.media",
})

_SECRETS: set[str] = set()
_QUERY_SECRET = re.compile(r"(api_?key|apikey|key|token|access_token)=([^&\s\"']+)", re.I)


class FetchError(Exception):
    """Network/API failure. The message is always safe to publish."""


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text: object) -> str:
    s = str(text)
    for secret in _SECRETS:
        s = s.replace(secret, "***")
    return _QUERY_SECRET.sub(lambda m: f"{m.group(1)}=***", s)


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise FetchError("destinazione non consentita")
    if parts.username or parts.password or (parts.port not in (None, 443)):
        raise FetchError("destinazione non consentita")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirect)


def request_json(url: str, *, params: dict | None = None, headers: dict | None = None,
                 body: dict | None = None, timeout: float = 25.0):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    _check_url(url)
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data else "GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", "errore di rete")
        raise FetchError(redact(f"rete non raggiungibile ({type(reason).__name__})")) from None
    except TimeoutError:
        raise FetchError("timeout") from None
    except OSError as e:
        raise FetchError(f"errore di rete ({type(e).__name__})") from None
    if len(raw) > MAX_BYTES:
        raise FetchError("risposta troppo grande")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FetchError("risposta non valida (non JSON)") from None
