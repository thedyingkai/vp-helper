from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .models import VPError

# ---------------------------------------------------------------------------
# Transport selection.
#
# qoj.ac and codeforces.com sit behind Cloudflare, which fingerprints the TLS
# handshake.  Plain requests/urllib3 has a non-browser fingerprint and always
# receives "HTTP 403 / Just a moment..." — no combination of headers, matching
# User-Agent or replayed cookies changes that (verified empirically).
#
# curl_cffi bundles libcurl-impersonate and reproduces a real browser's TLS and
# HTTP/2 fingerprint, which passes the check.  When curl_cffi is unavailable the
# original requests behaviour is kept, so the tool still degrades gracefully.
# ---------------------------------------------------------------------------
_IMPERSONATE = "firefox147"

# "firefox" is a version-dependent alias: curl_cffi 0.11 resolves it to Firefox
# 135 while 0.16 resolves it to Firefox 147.  Pairing that alias with a
# hardcoded User-Agent would send a 147 header over a 135 fingerprint whenever an
# older curl_cffi is installed.  Naming the profile explicitly keeps the two in
# step; pyproject.toml requires a curl_cffi new enough to know this profile.
#
# curl_cffi keeps the profile's headers (User-Agent included) to itself rather
# than in session.headers, but cli.py and qoj.py read session.headers
# ["User-Agent"] as metadata for the submit script, so the key has to exist.  It
# holds exactly the string this profile sends, which leaves the request on the
# wire byte-for-byte identical.
_IMPERSONATED_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) "
    "Gecko/20100101 Firefox/147.0"
)

# Bounded retry for GETs, mirroring the urllib3 policy this transport replaced:
# two retries with a 1s/2s backoff on transient statuses and on connection
# errors.  curl_cffi's own retry= only covers transport errors, not status
# codes, so the loop below covers both transports.  POSTs are never retried --
# starting a native VP must not be repeated behind the user's back.
_RETRY_TOTAL = 2
_RETRY_STATUS = frozenset({429, 502, 503, 504})
_RETRY_BACKOFF = 1.0

try:
    from curl_cffi import requests as _requests

    try:
        from curl_cffi.requests.errors import RequestsError as _RequestException
    except Exception:  # pragma: no cover - defensive
        _RequestException = Exception
except ImportError:  # pragma: no cover - curl_cffi not installed
    import requests as _requests

    _IMPERSONATE = None
    _RequestException = _requests.RequestException


class WebSession:
    def __init__(self, origin: str, account: dict):
        self.origin = origin.rstrip("/")
        if _IMPERSONATE:
            # curl_cffi supplies the browser headers that match the impersonated
            # fingerprint.  Do not override them with the config's user_agent --
            # that would break the disguise.  Only mirror the profile's own
            # User-Agent into session.headers so the lookups in cli.py/qoj.py work.
            self.session = _requests.Session(impersonate=_IMPERSONATE)
            self.session.headers["User-Agent"] = _IMPERSONATED_USER_AGENT
        else:
            self.session = _requests.Session()
            self.session.headers["User-Agent"] = account.get(
                "user_agent", "Mozilla/5.0 (X11; Linux x86_64) VP/0.1"
            )
        host = urlsplit(self.origin).hostname
        cookie = SimpleCookie()
        cookie.load(account.get("cookie", ""))
        for key, value in cookie.items():
            try:
                self.session.cookies.set(key, value.value, domain=host, path="/")
            except TypeError:
                # Some backends expose a narrower Cookies API.
                self.session.cookies.set(key, value.value)

    def get(self, url: str, **kwargs) -> object:
        host = urlsplit(url).hostname
        for attempt in range(_RETRY_TOTAL + 1):
            if attempt:
                # 1s then 2s -- the backoff the replaced urllib3 Retry used.
                time.sleep(_RETRY_BACKOFF * 2 ** (attempt - 1))
            try:
                r = self.session.get(url, timeout=(10, 40), **kwargs)
            except _RequestException as exc:
                if attempt == _RETRY_TOTAL:
                    raise VPError(f"Connection failed: {host} ({type(exc).__name__})") from None
                continue
            if r.status_code in _RETRY_STATUS and attempt < _RETRY_TOTAL:
                continue
            return self.check(r)
        # The final iteration always returns or raises; this keeps the flow total.
        raise VPError(f"Connection failed: {host}")  # pragma: no cover

    def post(self, url: str, data: dict):
        try:
            # Never retry a native contest start automatically.
            r = self.session.post(url, data=data, timeout=(10, 40))
        except _RequestException:
            raise VPError("The server did not confirm starting the VP. Run the same URL to check the remote state.") from None
        return self.check(r)

    def check(self, r):
        if r.status_code != 200:
            raise VPError(f"{urlsplit(r.url).hostname} returned HTTP {r.status_code}")
        if urlsplit(r.url).path in ("/login", "/enter"):
            raise VPError("Login session expired; update the account cookie in ~/.config/vp/config.json.")
        # Cloudflare's interstitial is only meaningful for textual responses.
        # A PDF or other binary body must not be decoded here: requests happens
        # to tolerate it, but curl_cffi reports such a response with a non-codec
        # encoding ("binary") and raises LookupError/UnicodeDecodeError.
        content_type = (r.headers.get("Content-Type") or "").lower()
        if not content_type or "text/" in content_type or "html" in content_type:
            try:
                body = r.text[:3000]
            except Exception:
                body = ""
            if "Just a moment..." in body or "cf-chl-" in body:
                raise VPError("The platform requests browser verification; refresh the login session in the Ubuntu browser.")
        return r

    def soup(self, url: str, **kwargs) -> BeautifulSoup:
        r = self.get(url, **kwargs)
        try:
            r.encoding = "utf8"
        except Exception:
            pass
        return BeautifulSoup(r.text, "html.parser")

    def pdf(self, url: str, target: Path) -> None:
        # Cookies are scoped to the OJ host, including for external attachments.
        r = self.get(url)
        if not r.content[:1024].lstrip().startswith(b"%PDF-"):
            raise VPError(f"The statement download is not a PDF: {target.name}")
        target.write_bytes(r.content)


def form_values(form) -> dict:
    result = {}
    for node in form.select("input[name], select[name], textarea[name]"):
        name = node.get("name")
        if node.get("type") in ("submit", "button", "file"):
            continue
        if node.get("type") in ("checkbox", "radio") and not node.has_attr("checked"):
            continue
        if node.name == "select":
            option = node.select_one("option[selected]") or node.select_one("option")
            result[name] = option.get("value", "") if option else ""
        elif node.name == "textarea":
            result[name] = node.get_text()
        else:
            result[name] = node.get("value", "")
    return result
