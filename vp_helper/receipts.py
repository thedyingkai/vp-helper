"""Server-confirmed submissions shared by submit and the independent monitor."""
from __future__ import annotations

import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .models import Submission, VPError
from .storage import read_json, state_directory, write_json


def receipt_directory(state_path: Path) -> Path:
    return state_path.with_suffix(".receipts")


def record_receipt(config: dict, submission_id: str, problem: str, submitted_at: float) -> bool:
    context = config.get("vp_context")
    if not isinstance(context, dict):
        return False  # A standalone upstream submit need not be attached to a VP.
    sid = str(submission_id)
    if not re.fullmatch(r"[0-9]+", sid) or not math.isfinite(submitted_at):
        raise VPError("The server did not assign a valid submission ID or time.")
    path = Path(context["state_path"])
    if path.resolve().parent != state_directory().resolve():
        raise VPError("The submission context is outside the VP state directory.")
    state = read_json(path)
    if (state.get("start_time") != context.get("start_time")
            or state.get("url") != context.get("url")
            or state.get("handle") != context.get("handle")
            or problem not in state.get("contest", {}).get("problems", {})):
        raise VPError("This submission context no longer matches the native VP.")
    relative = submitted_at - state["start_time"]
    if relative < 0:
        raise VPError("The submission precedes this native VP.")
    write_json(receipt_directory(path) / (sid + ".json"), {
        "id": sid, "team": "me", "problem": problem, "time": relative,
        "verdict": "PENDING", "start_time": state["start_time"], "url": state["url"],
    })
    return True


class ReceiptReader:
    def __init__(self, path: Path, state: dict):
        self.directory = receipt_directory(path)
        self.start_time = state["start_time"]
        self.url = state["url"]
        self.problems = state["contest"]["problems"]
        self.stamp = None

    def read(self) -> list[Submission]:
        try:
            stamp = self.directory.stat().st_mtime_ns
        except FileNotFoundError:
            return []
        if stamp == self.stamp:
            return []
        result = []
        for path in self.directory.glob("*.json"):
            data = read_json(path)
            if (data.get("start_time") == self.start_time and data.get("url") == self.url
                    and data.get("problem") in self.problems and data.get("verdict") == "PENDING"
                    and re.fullmatch(r"[0-9]+", str(data.get("id", "")))
                    and isinstance(data.get("time"), (int, float)) and math.isfinite(data["time"])
                    and data["time"] >= 0):
                result.append(Submission(**{key: data[key] for key in ("id", "team", "problem", "time", "verdict")}))
        self.stamp = stamp
        return result


def qoj_submission_id(response, origin: str) -> str | None:
    """A success response must actually identify the newly created submission."""
    from urllib.parse import urljoin
    target = (urljoin(origin, response.headers.get("Location", ""))
              if response.status_code in (302, 303) else response.url if response.status_code == 200 else "")
    parts = urlsplit(target)
    match = re.fullmatch(r"/submission/([0-9]+)/?", parts.path)
    return match[1] if parts.scheme == "https" and parts.netloc == urlsplit(origin).netloc and match else None


def qoj_submission_ids(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    return {match[1] for row in soup.select("tr") for link in row.select('a[href]')
            if (match := re.fullmatch(r"/submission/([0-9]+)", urlsplit(link["href"]).path))}


def qoj_id_from_list(html: str, previous: set[str], base_url: str, problem_id: str, handle: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates = set()
    contest_path = urlsplit(base_url).path.rstrip("/")
    for row in soup.select("tr"):
        user = row.select_one('.uoj-username, a[href*="/user/profile/"]')
        user_id = (urlsplit(user["href"]).path.rsplit("/", 1)[-1] if user and user.get("href")
                   else user.get_text(strip=True) if user else "")
        paths = [urlsplit(link["href"]).path for link in row.select("a[href]")]
        if user_id != handle or not any(p in (contest_path + "/problem/" + str(problem_id),
                                               "/problem/" + str(problem_id)) for p in paths):
            continue
        for path in paths:
            match = re.fullmatch(r"/submission/([0-9]+)", path)
            if match and match[1] not in previous:
                candidates.add(match[1])
    return next(iter(candidates)) if len(candidates) == 1 else None


def cf_submission_ids(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    return {row["data-submission-id"] for row in soup.select("tr[data-submission-id]")}


def cf_submission_id(soup, previous: set[str], contest: str, problem: str, handle: str) -> str | None:
    candidates = []
    for row in soup.select("tr[data-submission-id]"):
        sid = row["data-submission-id"]
        if sid in previous or not re.fullmatch(r"[0-9]+", sid):
            continue
        paths = [urlsplit(a.get("href", "")).path.rstrip("/") for a in row.select("a[href]")]
        matching_problem = any(p in (f"/contest/{contest}/problem/{problem.upper()}",
                                     f"/gym/{contest}/problem/{problem.upper()}") for p in paths)
        matching_user = any(p.lower() == "/profile/" + handle.lower() for p in paths)
        if matching_problem and matching_user:
            candidates.append(sid)
    # If another client also submitted, await the authoritative API instead of
    # assigning either server receipt to this request arbitrarily.
    return candidates[0] if len(candidates) == 1 else None
