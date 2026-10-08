from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata

from bs4 import BeautifulSoup
import requests

from .models import Contest, Medals, TeamScore, VPError
from .parsing import timestamp


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


def bundled_metadata(contest: Contest) -> dict | None:
    match = re.fullmatch(r"(20\d\d) (ICPC|CCPC) ([A-Za-z][A-Za-z .-]*) Reg", contest.name)
    if not match:
        return None
    filename = "-".join(match.groups()).lower().replace(" ", "-") + ".json"
    path = Path(__file__).with_name("data") / filename
    return json.loads(path.read_text(encoding="utf8")) if path.is_file() else None


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
                "source": source, "teams": projected,
                "medals": config.get("medal", {}).get("official") if isinstance(config.get("medal"), dict) else None}
    except (KeyError, TypeError, ValueError):
        raise VPError("The original archive lacks supported explicit team eligibility or contest rules.") from None


def load_metadata(contest: Contest) -> dict:
    cached = contest.extra.get("original_eligibility") or bundled_metadata(contest)
    if cached:
        return cached
    path = archive_path(contest)
    if path is None:
        raise VPError("Original team eligibility is unavailable; starred teams cannot be verified.")
    source = "https://raw.githubusercontent.com/xcpcio/board-data/main/" + path
    try:
        # Public archive reads use a separate session, without the OJ's credentials.
        with requests.Session() as web:
            files = []
            for filename in ("config.json", "team.json"):
                response = web.get(source + "/" + filename, timeout=(10, 25))
                response.raise_for_status()
                files.append(response.json())
            teams = list(files[1].values()) if isinstance(files[1], dict) else files[1]
            organizations = None
            if any("organization" not in team and "organization_id" in team for team in teams):
                if files[0].get("organizations", {}).get("url") != "organizations.json":
                    raise VPError("The original archive's organization reference is unsupported.")
                response = web.get(source + "/organizations.json", timeout=(10, 25))
                response.raise_for_status()
                organizations = response.json()
        return board_metadata(*files, contest, source, organizations)
    except (requests.RequestException, ValueError):
        raise VPError("Original team eligibility could not be read from XCPCIO; the official rank remains unavailable.") from None


def classify(rows: list[TeamScore], contest: Contest, metadata: dict | None = None) -> None:
    metadata = metadata or contest.extra.get("original_eligibility") or bundled_metadata(contest)
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
    medals = metadata.get("medals")
    if isinstance(medals, dict) and all(isinstance(medals.get(k), int) and medals[k] >= 0 for k in ("gold", "silver", "bronze")):
        contest.medals = Medals(medals["gold"], medals["silver"], medals["bronze"], metadata["source"] + "/config.json")
