from __future__ import annotations

from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import VPError


class WebSession:
    def __init__(self, origin: str, account: dict):
        self.origin = origin.rstrip("/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = account.get("user_agent", "Mozilla/5.0 (X11; Linux x86_64) VP/0.1")
        retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 502, 503, 504], allowed_methods=["GET"])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        cookie = SimpleCookie()
        cookie.load(account.get("cookie", ""))
        for key, value in cookie.items():
            self.session.cookies.set(key, value.value, domain=urlsplit(self.origin).hostname, path="/")

    def get(self, url: str, **kwargs) -> requests.Response:
        try:
            r = self.session.get(url, timeout=(10, 40), **kwargs)
        except requests.RequestException as exc:
            raise VPError(f"Connection failed: {urlsplit(url).hostname} ({type(exc).__name__})") from None
        return self.check(r)

    def post(self, url: str, data: dict) -> requests.Response:
        try:
            # Never retry a native contest start automatically.
            r = self.session.post(url, data=data, timeout=(10, 40))
        except requests.RequestException as exc:
            raise VPError("The server did not confirm starting the VP. Run the same URL to check the remote state.") from None
        return self.check(r)

    def check(self, r: requests.Response) -> requests.Response:
        if r.status_code != 200:
            raise VPError(f"{urlsplit(r.url).hostname} returned HTTP {r.status_code}")
        if urlsplit(r.url).path in ("/login", "/enter"):
            raise VPError("Login session expired; update the account cookie in ~/.config/vp/config.json.")
        if "Just a moment..." in r.text[:3000] or "cf-chl-" in r.text[:3000]:
            raise VPError("The platform requests browser verification; refresh the login session in the Ubuntu browser.")
        return r

    def soup(self, url: str, **kwargs) -> BeautifulSoup:
        r = self.get(url, **kwargs)
        r.encoding = "utf8"
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
