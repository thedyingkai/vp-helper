from __future__ import annotations

from datetime import datetime
import json
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .models import Medals, VPError
from .provincial import provincial_name


UCUP_HOST = "contest.ucup.ac"


def qoj_account_name(node) -> str:
    if node is None:
        return ""
    path = urlsplit(node.get("href", "")).path.rstrip("/")
    if path.startswith("/user/profile/"):
        return path.rsplit("/", 1)[-1]
    if "uoj-username" in node.get("class", []):
        return node.get_text(strip=True)
    return ""


def contest_id_url(platform: str, ident: str) -> str:
    paths = {"cf": "https://codeforces.com/contest/", "qoj": "https://qoj.ac/contest/",
             "gym": "https://codeforces.com/gym/", "ucup": f"https://{UCUP_HOST}/contest/"}
    if platform not in paths or not re.fullmatch(r"[0-9]+", ident) or int(ident) <= 0:
        raise VPError("Use vp cf ID, vp qoj ID, vp gym ID or ucup ID with a positive numeric contest ID.")
    return paths[platform] + str(int(ident))


def contest_url(url: str) -> tuple[str, str, str]:
    p = urlsplit(url)
    try:
        port = p.port
    except ValueError:
        raise VPError("Invalid contest URL port.") from None
    if p.scheme not in ("http", "https") or p.username or p.password or port:
        raise VPError("Use a QOJ, UCup, Codeforces contest or Codeforces Gym URL.")
    if p.hostname in ("qoj.ac", UCUP_HOST):
        match = re.fullmatch(r"/contest/(\d+)/?", p.path)
        platform = "ucup" if p.hostname == UCUP_HOST else "qoj"
    elif p.hostname == "codeforces.com":
        match = re.fullmatch(r"/(?:contest|gym)/(\d+)/?", p.path)
        platform = "gym" if p.path.startswith("/gym/") else "cf"
    else:
        match = None
    if not match:
        raise VPError("Only QOJ, UCup, Codeforces contest and Codeforces Gym links are supported.")
    return platform, match[1], f"https://{p.hostname}{p.path.rstrip('/')}"


def seconds(value: str) -> int:
    parts = [int(x) for x in value.strip().split(":")]
    if len(parts) == 2:
        return parts[0] * 3600 + parts[1] * 60
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise VPError(f"Invalid contest time: {value!r}")


def timestamp(value: str, timezone: str = "Asia/Shanghai") -> float:
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(timezone))
        return dt.timestamp()
    except ValueError as exc:
        raise VPError(f"Invalid platform timestamp: {value!r}") from exc


def js_value(html: str, key: str, default=None):
    # Decode JSON literals only. Never execute platform JavaScript.
    match = re.search(r"(?<![\w.])(?:var\s+)?" + re.escape(key) + r"\s*=\s*", html)
    if not match:
        return default
    try:
        return json.JSONDecoder().raw_decode(html[match.end():])[0]
    except ValueError:
        return default


def directory_name(titles: list[str]) -> str:
    for title in titles:
        province = provincial_name(title)
        if province:
            return province
        # Recent CCPC titles pair a Chinese name with a complete English name.
        english = [part for part in re.findall(r"[（(]([^（）()]+)[）)]", title)
                   if re.search(r"\b(?:ICPC|CCPC)\b", part, re.I) and re.search(r"\b20\d{2}\b", part)]
        if english:
            title = english[-1]
        year = re.search(r"\b(20\d{2})\b", title)
        series = re.search(r"\b(ICPC|CCPC)\b", title, re.I)
        if not year or not series:
            continue
        kind = "Reg" if re.search(r"Regional|区域|\bSite\b", title, re.I) else "Inv" if re.search(r"Invitational|邀请", title, re.I) else "Pro" if re.search(r"Provincial|省赛", title, re.I) else None
        if not kind:
            continue
        site = title
        patterns = [r"\b20\d{2}\b", r"\bICPC\b", r"\bCCPC\b", r"\bAsia(?:\s+East)?\b", r"\b(?:The|Contest|Regional|Invitational|Provincial|Programming|Collegiate|China|International|Chinese|University|Finals?|Championship|Site)\b", r"\b\d+(?:st|nd|rd|th)\b"]
        for pattern in patterns:
            site = re.sub(pattern, " ", site, flags=re.I)
        site = re.sub(r"[()\[\]:,/\\\-–—]+", " ", site)
        site = " ".join(site.split()).strip(" .")
        if site:
            return f"{year[1]} {series[1].upper()} {site} {kind}"
    raise VPError("The platform does not identify a supported ICPC/CCPC regional, invitational or Chinese provincial contest name and year.")


def ccpc_reveal_count(name: str) -> int | None:
    match = re.fullmatch(r"(20\d{2}) CCPC .+ Reg", name)
    return 50 if match and int(match[1]) >= 2026 else None


def medals_from_html(html: str, source: str) -> Medals | None:
    cutoffs = js_value(html, "medals_cutoff")
    if cutoffs and len(cutoffs) == 3 and all(isinstance(x, int) for x in cutoffs):
        if 0 <= cutoffs[0] <= cutoffs[1] <= cutoffs[2]:
            return Medals(cutoffs[0], cutoffs[1]-cutoffs[0], cutoffs[2]-cutoffs[1], source)
    return None
