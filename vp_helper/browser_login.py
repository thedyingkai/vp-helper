"""Import only judge cookies from the local Firefox profile and verify its user."""
from __future__ import annotations

import configparser
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

_MAX_SESSION_SIZE = 16 * 1024 * 1024
_cache: dict = {}
_lock = threading.Lock()


@dataclass
class BrowserCookies:
    profile: Path
    cookie: str = field(repr=False)


def _diagnose(diagnostics: list[str] | None, message: str) -> None:
    if diagnostics is not None and message not in diagnostics:
        diagnostics.append(message)


def firefox_profiles(account: dict, *, diagnostics: list[str] | None = None) -> list[Path]:
    explicit = account.get("firefox_profile")
    if explicit:
        try:
            path = Path(explicit).expanduser().resolve()
            if path.is_dir():
                return [path]
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        _diagnose(diagnostics, "The configured firefox_profile is not an accessible directory; check the Root Directory in about:profiles.")
        return []
    if os.name != "posix":
        _diagnose(diagnostics, "Automatic Firefox profile discovery requires Linux; set firefox_profile explicitly.")
        return []
    home = Path.home()
    sudo_user = os.geteuid() == 0 and os.environ.get("SUDO_USER") not in (None, "", "root")
    if sudo_user:
        import pwd
        try:
            home = Path(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
        except KeyError:
            _diagnose(diagnostics, "Could not locate the invoking user's home directory; run vp as the Firefox user.")
            return []
    # sudo may retain root's XDG_CONFIG_HOME. Use the invoking user's default
    # location there; a nonstandard location can still be selected explicitly.
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") if not sudo_user else home / ".config"
    if not xdg.is_absolute():
        xdg = home / ".config"
    snap = home / "snap/firefox"
    flatpak = home / ".var/app/org.mozilla.firefox"
    bases = (home / ".mozilla/firefox", xdg / "mozilla/firefox",
             snap / "common/.mozilla/firefox", snap / "common/.config/mozilla/firefox",
             snap / "current/.mozilla/firefox", snap / "current/.config/mozilla/firefox",
             flatpak / ".mozilla/firefox", flatpak / "config/mozilla/firefox")
    profiles = []
    seen_bases = set()
    for base in bases:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            base = base.resolve()
            if base in seen_bases:
                continue
            seen_bases.add(base)
            with (base / "profiles.ini").open(encoding="utf8") as source:
                parser.read_file(source)
            preferred = [parser[s]["Default"] for s in parser.sections()
                         if s.startswith("Install") and parser[s].get("Default")]
            entries = [s for s in parser.sections() if s.startswith("Profile")]
            entries.sort(key=lambda s: (parser[s].get("Path") not in preferred,
                                       parser[s].get("Default") != "1"))
            for section in entries:
                name = parser[section].get("Path")
                if not name:
                    continue
                path = (base / name if parser[section].get("IsRelative", "1") == "1" else Path(name)).resolve()
                if path.is_dir() and path not in profiles:
                    profiles.append(path)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError, UnicodeError, configparser.Error) as exc:
            _diagnose(diagnostics, f"A Firefox profiles.ini could not be read ({type(exc).__name__}).")
    if not profiles:
        _diagnose(diagnostics, "No Firefox profile was found in the legacy, XDG, Snap or Flatpak locations; set firefox_profile to the Root Directory in about:profiles.")
    return profiles


def _decode_session(data: bytes) -> dict:
    """Firefox's mozLz40 header precedes a size and one LZ4 block."""
    if data[:8] != b"mozLz40\0" or len(data) < 12:
        raise ValueError("Invalid Firefox session header")
    expected = int.from_bytes(data[8:12], "little")
    if not 0 < expected <= _MAX_SESSION_SIZE:
        raise ValueError("Firefox session size is outside the supported range")
    block, position, result = memoryview(data)[12:], 0, bytearray()

    def length(value):
        nonlocal position
        if value == 15:
            while True:
                if position >= len(block):
                    raise ValueError("Truncated Firefox session")
                extra = block[position]
                position += 1
                value += extra
                if extra != 255:
                    break
        return value

    while position < len(block):
        token = block[position]
        position += 1
        literals = length(token >> 4)
        if position + literals > len(block) or len(result) + literals > expected:
            raise ValueError("Invalid Firefox session literals")
        result.extend(block[position:position + literals])
        position += literals
        if position == len(block):
            break
        if position + 2 > len(block):
            raise ValueError("Truncated Firefox session offset")
        distance = int.from_bytes(block[position:position + 2], "little")
        position += 2
        count = length(token & 15) + 4
        if not 0 < distance <= len(result) or len(result) + count > expected:
            raise ValueError("Invalid Firefox session match")
        for _ in range(count):
            result.append(result[-distance])
    if len(result) != expected:
        raise ValueError("Firefox session size mismatch")
    value = json.loads(result)
    if not isinstance(value, dict):
        raise ValueError("Invalid Firefox session object")
    return value


def _ordinary(attributes) -> bool:
    return not any(attributes.values()) if isinstance(attributes, dict) else attributes in (None, "")


def _session_cookies(profile: Path, host: str, *, diagnostics: list[str] | None = None) -> dict:
    files = [profile / "sessionstore.jsonlz4",
             profile / "sessionstore-backups/recovery.jsonlz4",
             profile / "sessionstore-backups/recovery.baklz4",
             profile / "sessionstore-backups/previous.jsonlz4"]
    try:
        existing = [p for p in files if p.is_file()]
        if not existing:
            return {}
        latest = max(existing, key=lambda p: p.stat().st_mtime_ns)
        if latest.stat().st_size > _MAX_SESSION_SIZE:
            _diagnose(diagnostics, "The Firefox session recovery file exceeds the supported 16 MiB size.")
            return {}
        state = _decode_session(latest.read_bytes())
        cookies = list(state.get("cookies", []))
        for window in state.get("windows", []):
            if not window.get("isPrivate"):
                cookies.extend(window.get("cookies", []))
        return {c["name"]: c["value"] for c in cookies
                if c.get("host", "").lstrip(".") == host and c.get("path", "/") == "/"
                and _ordinary(c.get("originAttributes")) and c.get("expiry", float("inf")) > time.time()
                and isinstance(c.get("name"), str) and isinstance(c.get("value"), str)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        _diagnose(diagnostics, f"The Firefox session recovery file could not be read ({type(exc).__name__}).")
        return {}


def _header(values: dict) -> str:
    jar = SimpleCookie()
    for name, value in values.items():
        if (isinstance(name, str) and isinstance(value, str)
                and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                and not any(ord(c) < 32 or ord(c) == 127 for c in value)):
            try:
                jar[name] = value
            except CookieError:
                continue
    return "; ".join(m.OutputString() for m in jar.values())


def browser_cookies(platform: str, account: dict, *, diagnostics: list[str] | None = None) -> list[BrowserCookies]:
    host = "qoj.ac" if platform == "qoj" else "codeforces.com"
    candidates = []
    for profile in firefox_profiles(account, diagnostics=diagnostics):
        persistent = {}
        database = profile / "cookies.sqlite"
        try:
            if database.is_file():
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.5)) as conn:
                    persistent = dict(conn.execute(
                        "SELECT name, value FROM moz_cookies WHERE host IN (?, ?) AND path='/' "
                        "AND expiry > ? AND originAttributes='' ORDER BY lastAccessed",
                        (host, "." + host, int(time.time()))))
        except (OSError, sqlite3.Error) as exc:
            _diagnose(diagnostics, f"Firefox cookies.sqlite could not be read ({type(exc).__name__}).")
        combined = {**persistent, **_session_cookies(profile, host, diagnostics=diagnostics)}
        seen = set()
        for values in (combined, persistent):
            header = _header(values)
            if header and header not in seen:
                candidates.append(BrowserCookies(profile, header))
                seen.add(header)
        if not seen:
            _diagnose(diagnostics, f"No usable {host} cookies were found in a Firefox profile; sign in in a normal tab and allow Firefox to save the session.")
    return candidates


def _verify(platform: str, candidate: BrowserCookies, expected: str, *, diagnostics: list[str] | None = None) -> dict | None:
    from .http import WebSession
    host = "qoj.ac" if platform == "qoj" else "codeforces.com"
    origin = "https://" + host
    web = None
    try:
        web = WebSession(origin, {"cookie": candidate.cookie}, auto_refresh=False)
        try:
            response = web.session.get(origin + "/", params={"locale": "en"}, timeout=(5, 15))
        except Exception as exc:
            _diagnose(diagnostics, f"The request to verify Firefox login on {host} failed ({type(exc).__name__}).")
            return None
        try:
            body = response.text[:3000]
        except Exception:
            body = ""
        if "Just a moment..." in body or "cf-chl-" in body:
            _diagnose(diagnostics, f"Cloudflare browser verification blocked the Firefox login check (HTTP {response.status_code}).")
            return None
        if response.status_code != 200:
            _diagnose(diagnostics, f"{host} returned HTTP {response.status_code} while verifying Firefox login.")
            return None
        location = urlsplit(response.url)
        if location.hostname != host:
            _diagnose(diagnostics, "The Firefox login check was redirected to a different host.")
            return None
        if location.path in ("/login", "/enter"):
            _diagnose(diagnostics, f"{host} did not accept the saved Firefox login and redirected to its login page.")
            return None
        web.check(response)
        soup = BeautifulSoup(response.text, "html.parser")
        selector = "ul.nav-pills, .navbar, nav, header, .nav-item.dropdown .dropdown-menu" if platform == "qoj" else "#header"
        prefix = "/user/profile/" if platform == "qoj" else "/profile/"
        links = [a for header in soup.select(selector) for a in header.select("a[href]")]
        handles = {urlsplit(a.get("href", "")).path[len(prefix):].rstrip("/")
                   for a in links
                   if urlsplit(a["href"]).path.startswith(prefix)}
        if not handles:
            if any(urlsplit(a["href"]).path in ("/login", "/enter") for a in links):
                _diagnose(diagnostics, f"{host} reports that the saved Firefox session is not logged in.")
            else:
                _diagnose(diagnostics, "The logged-in account could not be identified in the page navigation; the page layout may have changed.")
            return None
        if expected:
            handles = {h for h in handles if h == expected or platform != "qoj" and h.lower() == expected.lower()}
            if not handles:
                detail = "; QOJ handles are matched case-sensitively" if platform == "qoj" else ""
                _diagnose(diagnostics, "The Firefox account does not match the configured handle" + detail + ".")
                return None
        if len(handles) != 1:
            _diagnose(diagnostics, "Multiple accounts were found in the page navigation; the logged-in account could not be identified safely.")
            return None
        jar = getattr(web.session.cookies, "jar", web.session.cookies)
        values = {c.name: c.value for c in jar
                  if c.domain.lstrip(".") == host and c.path == "/"}
        cookie = _header(values)
        if not cookie:
            _diagnose(diagnostics, "The Firefox account was identified, but no usable login cookies remained.")
            return None
        return {"cookie": cookie, "handle": expected or handles.pop(),
                "cookie_source": "firefox", "firefox_profile": str(candidate.profile)}
    except Exception as exc:
        # Do not replace a configured session on network failure or another login.
        # Exception text can contain request headers, so report only its type.
        _diagnose(diagnostics, f"Firefox login verification failed ({type(exc).__name__}).")
        return None
    finally:
        if web is not None:
            try:
                web.session.close()
            except Exception:
                pass


def refresh_browser_account(platform: str, account: dict, *, force: bool = False,
                            diagnostics: list[str] | None = None) -> bool:
    if account.get("cookie_source") == "manual":
        return False
    for candidate in browser_cookies(platform, account, diagnostics=diagnostics):
        key = ("qoj" if platform == "qoj" else "cf", str(candidate.profile), account.get("handle", ""),
               hashlib.sha256(candidate.cookie.encode()).digest())
        with _lock:
            cached = _cache.get(key)
            if not force and cached and cached[1] and time.monotonic() - cached[0] < 30:
                verified = cached[1]
            else:
                verified = _verify(platform, candidate, account.get("handle", ""), diagnostics=diagnostics)
                if verified:
                    _cache[key] = (time.monotonic(), verified)
                else:
                    _cache.pop(key, None)
        if verified:
            account.update(verified)
            return True
    return False


def browser_fields(account: dict) -> dict:
    return {k: account[k] for k in ("cookie_source", "firefox_profile") if account.get(k)}


def refresh_submission_login(path: Path, config: dict, platform: str) -> None:
    from .storage import write_json
    before = dict(config)
    if refresh_browser_account(platform, config) and config != before:
        write_json(path, config)
