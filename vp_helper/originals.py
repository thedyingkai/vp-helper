from __future__ import annotations

from contextlib import nullcontext
import hashlib
import base64
import json
import math
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import requests

from .models import Contest, Medals, Reference, Submission, TeamScore, VPError
from .parsing import timestamp
from .scoring import score_submissions, verdict
from .storage import read_json, write_json

_github_api_preferred = False


def archive_json(web, url: str):
    """Read identical archive files through either GitHub public endpoint."""
    global _github_api_preferred
    match = re.fullmatch(r"/xcpcio/board-data/([a-f0-9]{40}|main)/(data/[A-Za-z0-9_./-]+\.json)", urlsplit(url).path)
    alternative = ("https://api.github.com/repos/xcpcio/board-data/contents/" + match[2]
                   if urlsplit(url).hostname == "raw.githubusercontent.com" and match else None)
    choices = [False, True] if alternative else [False]
    if alternative and _github_api_preferred:
        choices.reverse()
    retry = set()
    for attempt in range(2):
        for use_api in choices:
            if attempt and use_api not in retry:
                continue
            try:
                options = {"params": {"ref": match[1]}, "headers": {"Accept": "application/vnd.github.raw+json"}} if use_api else {}
                response = web.get(alternative if use_api else url, timeout=(10, 30), **options)
                response.raise_for_status()
                data = response.json()
                if use_api and isinstance(data, dict) and data.get("encoding") == "base64" and "content" in data:
                    data = json.loads(base64.b64decode(data["content"]).decode("utf8"))
                _github_api_preferred = use_api
                return data
            except (requests.ConnectionError, requests.Timeout):
                retry.add(use_api)
            except requests.RequestException as exc:
                if exc.response is not None and exc.response.status_code in (500, 502, 503, 504):
                    retry.add(use_api)
            except ValueError:
                continue
    raise VPError("The public original archive could not be downloaded through GitHub.")


def identity(name: str) -> str:
    text = BeautifulSoup(name, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def archive_path(contest: Contest) -> str | None:
    match = re.fullmatch(r"(20\d\d) (ICPC|CCPC) ([A-Za-z][A-Za-z .-]*) Reg", contest.name)
    if not match:
        return None
    year, kind, site = match.groups()
    edition = int(year) - (1975 if kind == "ICPC" else 2014)
    suffix = "th" if 11 <= edition % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(edition % 10, "th")
    slug = re.sub(r"[ .]+", "-", site.lower())
    slug = {"hong-kong": "hongkong"}.get(slug, slug)
    return f"data/{kind.lower()}/{edition}{suffix}/" + slug


def board_metadata(config: dict, teams: list | dict, contest: Contest, source: str,
                   organizations: list | dict | None = None) -> dict:
    def at(value):
        if isinstance(value, str):
            return timestamp(value)
        numeric = float(value)
        return numeric / 1000 if abs(numeric) >= 100_000_000_000 else numeric

    def name(value):
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            if "name" in value:
                return name(value["name"])
            texts = value.get("texts", {})
            selected = texts.get("en") or texts.get(value.get("fallback_lang"))
            if isinstance(selected, str):
                return selected
        raise ValueError("unsupported localized name")

    try:
        duration = int(at(config["end_time"]) - at(config["start_time"]))
        penalty = int(config["penalty"])
        if penalty % 60:
            raise ValueError("non-minute penalty")
        labels = [p["label"] for p in config["problems"]] if config.get("problems") else config["problem_id"]
        organizations_by_id = {}
        if organizations is not None:
            values = organizations.items() if isinstance(organizations, dict) else enumerate(organizations)
            for key, organization in values:
                ident = str(organization.get("id", key))
                if ident in organizations_by_id:
                    raise ValueError("duplicate organization identity")
                organizations_by_id[ident] = name(organization)
        projected = []
        items = teams.items() if isinstance(teams, dict) else enumerate(teams)
        for key, team in items:
            group = set(team.get("group", [])) & {"official", "unofficial"}
            if isinstance(team.get("official"), bool):
                official = team["official"]
            elif isinstance(team.get("unofficial"), bool):
                official = not team["unofficial"]
            elif len(group) == 1:
                official = "official" in group
            else:
                raise ValueError("missing explicit original team eligibility")
            team_name = name(team.get("name", team.get("team_name")))
            organization = (name(team["organization"]) if "organization" in team else
                            organizations_by_id[str(team["organization_id"])])
            if not isinstance(team.get("members"), list):
                raise ValueError("unsupported team identity fields")
            members = [name(member) for member in team["members"]]
            projected.append({"id": str(team.get("team_id", team.get("id", key))),
                              "name": f"{organization} - {team_name} - {', '.join(members)}", "official": official})
        return {"version": 1, "contest_name": contest.name, "duration": duration,
                "penalty_minutes": penalty // 60, "problems": labels,
                "problem_ids": {str(p.get("id", i)): p["label"] for i, p in enumerate(config["problems"])} if config.get("problems") else {str(i): label for i, label in enumerate(labels)},
                "source": source, "teams": projected,
                "medals": config.get("medal", {}).get("official") if isinstance(config.get("medal"), dict) else None}
    except (KeyError, TypeError, ValueError):
        raise VPError("The original archive lacks supported explicit team eligibility or contest rules.") from None


def load_metadata(contest: Contest, *, web=None) -> dict:
    cached = contest.extra.get("original_eligibility") or read_json(cache_path("eligibility", contest.url))
    if cached:
        return cached
    path = archive_path(contest)
    if path is None:
        raise VPError("Original team eligibility is unavailable; starred teams cannot be verified.")
    source = "https://raw.githubusercontent.com/xcpcio/board-data/main/" + path
    try:
        # Public archive reads use a separate session, without the OJ's credentials.
        with (nullcontext(web) if web is not None else requests.Session()) as web:
            files = []
            for filename in ("config.json", "team.json"):
                files.append(archive_json(web, source + "/" + filename))
            teams = list(files[1].values()) if isinstance(files[1], dict) else files[1]
            organizations = None
            if any("organization" not in team and "organization_id" in team for team in teams):
                if files[0].get("organizations", {}).get("url") != "organizations.json":
                    raise VPError("The original archive's organization reference is unsupported.")
                organizations = archive_json(web, source + "/organizations.json")
        return board_metadata(*files, contest, source, organizations)
    except (requests.RequestException, ValueError):
        raise VPError("Original team eligibility could not be read from XCPCIO; the official rank remains unavailable.") from None


def classify(rows: list[TeamScore], contest: Contest, metadata: dict | None = None) -> None:
    metadata = metadata or contest.extra.get("original_eligibility")
    if metadata is None:
        raise VPError("Original team eligibility is unavailable; imported ghost status alone does not identify starred teams.")
    if (metadata.get("version") != 1 or metadata.get("contest_name") != contest.name
            or metadata.get("duration") != contest.rules.duration
            or metadata.get("penalty_minutes") != contest.rules.penalty_minutes
            or metadata.get("problems") != list(contest.problems)):
        raise VPError("The original eligibility archive does not match this contest's rules and problems.")
    teams = metadata.get("teams", [])
    lookup = {identity(t["name"]): t for t in teams}
    if not teams or len(lookup) != len(teams) or not all(isinstance(t.get("official"), bool) for t in teams):
        raise VPError("The original archive has missing or ambiguous team identities or eligibility.")

    def import_group(ident: str) -> str | None:
        # QOJ keeps independently imported contest datasets in separate reserved
        # ID namespaces. Select one only through exact original-team identities.
        match = re.fullmatch(r"\$DEFAULT_DAT_PREFIX_(?:(\d+)_)?\d+", ident)
        return (match[1] or "1") if match else None

    groups = {import_group(row.id) for row in rows
              if (team := lookup.get(identity(row.name))) and team["official"]}
    if None not in groups and len(groups) > 1:
        raise VPError("The original teams appear in multiple imported contest datasets.")
    original_group = next(iter(groups)) if len(groups) == 1 and None not in groups else None
    seen = set()
    resolved = []
    for row in rows:
        team = lookup.get(identity(row.name))
        if team is None:
            group = import_group(row.id)
            if original_group is not None and group is not None and group != original_group:
                continue
            if not row.eligible:  # QOJ's explicit unofficial import can be excluded directly.
                resolved.append((row, False))
                continue
            raise VPError("An imported original team could not be matched to its official eligibility record.")
        if team["id"] in seen or not row.eligible and team["official"]:
            raise VPError("The original scoreboard has duplicate or inconsistent team eligibility records.")
        seen.add(team["id"])
        resolved.append((row, team["official"]))
    if not all(t["id"] in seen for t in teams if t["official"]):
        raise VPError("The original scoreboard is missing a formally participating team.")
    for row, eligible in resolved:
        row.eligible = eligible
    rows[:] = [row for row, _ in resolved]
    contest.extra["original_eligibility"] = metadata
    write_json(cache_path("eligibility", contest.url), metadata)
    medals = metadata.get("medals")
    if isinstance(medals, dict) and all(isinstance(medals.get(k), int) and medals[k] >= 0 for k in ("gold", "silver", "bronze")):
        contest.medals = Medals(medals["gold"], medals["silver"], medals["bronze"], metadata["source"] + "/config.json")


def cache_path(kind: str, source: str) -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "vp" / "originals"
    return root / (kind + "-" + hashlib.sha256(source.encode()).hexdigest()[:24] + ".json")


def archive_submissions(runs: list, metadata: dict, contest: Contest) -> list[Submission]:
    """XCPCIO run timestamps are milliseconds relative to the original start."""
    if not isinstance(runs, list):
        raise VPError("The original archive did not provide a submission event list.")
    teams = {str(t["id"]) for t in metadata["teams"]}
    problems = metadata.get("problem_ids") or {str(i): label for i, label in enumerate(metadata["problems"])}
    result = []
    seen = set()
    try:
        for run in runs:
            sid = str(run.get("submission_id", run.get("id", "")))
            team = str(run["team_id"])
            problem = problems[str(run["problem_id"])]
            at = float(run["timestamp"]) / 1000
            if (not sid or (team, sid) in seen or team not in teams or problem not in contest.problems
                    or not math.isfinite(at) or not 0 <= at < contest.rules.duration):
                raise ValueError("invalid event identity or time")
            status = verdict(run["status"])
            if status in ("PENDING", "TOO-LATE"):
                raise ValueError("the original events are not final")
            seen.add((team, sid))
            result.append(Submission(sid, team, problem, at, status))
    except (KeyError, TypeError, ValueError, VPError):
        raise VPError("The original submission events contain unsupported identities, timestamps or verdicts.") from None
    return result


def load_archive_submissions(contest: Contest, metadata: dict, *, web=None) -> list[Submission]:
    source = metadata["source"] + "/run.json"
    path = cache_path("runs", source)
    cached = read_json(path)
    if cached and cached.get("source") == source:
        runs = cached.get("runs")
    else:
        try:
            # These requests intentionally carry no judge session or API key.
            with (nullcontext(web) if web is not None else requests.Session()) as web:
                runs = archive_json(web, source)
        except (requests.RequestException, ValueError, VPError):
            raise VPError("Original submission events unavailable; only the final scoreboard can be shown.") from None
    return archive_submissions(runs, metadata, contest)


def cache_archive_submissions(metadata: dict, submissions: list[Submission]) -> None:
    problems = metadata.get("problem_ids") or {str(i): label for i, label in enumerate(metadata["problems"])}
    ids = {label: pid for pid, label in problems.items()}
    source = metadata["source"] + "/run.json"
    write_json(cache_path("runs", source), {"source": source, "runs": [
        {"id": s.id, "team_id": s.team, "problem_id": ids[s.problem],
         "timestamp": s.time * 1000, "status": s.verdict} for s in submissions]})


class OriginalReplay:
    """An event timeline accepted only when every final scoring cell agrees."""

    def __init__(self, contest: Contest, teams: list[dict], submissions: list[Submission], expected: list[TeamScore]):
        self.problems = list(contest.problems)
        self.teams = teams
        self.events = {str(t["id"]): [] for t in teams}
        if len(self.events) != len(teams) or "me" in self.events:
            raise VPError("The original timeline has duplicate team identities.")
        seen = set()
        for event in submissions:
            key = event.team, event.id
            if (event.team not in self.events or event.problem not in self.problems or key in seen
                    or not math.isfinite(event.time) or not 0 <= event.time < contest.rules.duration
                    or event.verdict in ("PENDING", "TOO-LATE")):
                raise VPError("The original timeline has unsupported or incomplete submission events.")
            seen.add(key)
            self.events[event.team].append(event)
        self._cache_key = None
        self._cache = None
        final = self._rows(contest, contest.rules.duration, released=True)
        lookup = {identity(row.name): row for row in final}
        if len(lookup) != len(final) or len(final) != len(expected):
            raise VPError("The original timeline and scoreboard have different teams.")
        for row in expected:
            actual = lookup.get(identity(row.name))
            if (actual is None or actual.eligible != row.eligible or actual.rank_key != row.rank_key
                    or any(actual.cells[p] != row.cells[p] for p in self.problems)):
                raise VPError("Original submission events do not reproduce the judge's final scoreboard; live ranking is unavailable.")

    def _rows(self, contest: Contest, elapsed: float, *, released: bool) -> list[TeamScore]:
        result = []
        for team in self.teams:
            row = score_submissions(self.events[str(team["id"])], self.problems, contest.rules, elapsed,
                                    public=True, released=released, team_id=str(team["id"]), name=team["name"])
            row.eligible = team["official"]
            result.append(row)
        return result

    def at(self, contest: Contest, elapsed: float, *, released: bool = False) -> Reference:
        key = int(min(elapsed, contest.rules.duration)), contest.rules.frozen(elapsed, released), released
        if key != self._cache_key:
            self._cache = Reference(self._rows(contest, key[0], released=released),
                                    complete=True, statistics_complete=True, released=released,
                                    message="Scoreboard frozen" if key[1] else "Original submissions replayed at VP time", replay=self)
            self._cache_key = key
        return self._cache
