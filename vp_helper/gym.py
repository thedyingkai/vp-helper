from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import secrets
import time
from urllib.parse import urlencode, urljoin, urlsplit

from .http import WebSession, form_values
from .models import Cell, Contest, Medals, Reference, Rules, Submission, TeamScore, VPError
from .parsing import directory_name, medals_from_html
from .scoring import verdict


def signed_parameters(method: str, params: dict, key: str, secret: str,
                      at: int | None = None, nonce: str | None = None) -> dict:
    p = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items()}
    p.update(apiKey=key, time=str(at if at is not None else int(time.time())))
    prefix = nonce or secrets.token_hex(3)
    canonical = "&".join(f"{k}={v}" for k, v in sorted(p.items()))
    p["apiSig"] = prefix + hashlib.sha512(f"{prefix}/{method}?{canonical}#{secret}".encode()).hexdigest()
    return p


class Gym:
    def __init__(self, url: str, account: dict):
        self.url = url
        self.id = urlsplit(url).path.rsplit("/", 1)[-1]
        self.platform = "gym" if urlsplit(url).path.startswith("/gym/") else "cf"
        self.origin = "https://codeforces.com"
        self.account = account
        self.handle = account.get("handle", "")
        self.web = WebSession(self.origin, account)
        self.start_time: float | None = None
        self._last_api = 0.0
        self._original = None
        self.submissions_complete = False

    def api(self, method: str, **params):
        if not self.account.get("api_key") or not self.account.get("api_secret"):
            raise VPError("Gym standings require your Codeforces API key and secret in ~/.config/vp/config.json.")
        remaining = 2.1 - (time.monotonic() - self._last_api)
        if remaining > 0:
            time.sleep(remaining)
        self._last_api = time.monotonic()
        p = signed_parameters(method, params, self.account["api_key"], self.account["api_secret"])
        r = self.web.get(self.origin + "/api/" + method, params=p)
        try:
            response = r.json()
        except ValueError:
            raise VPError("Codeforces API did not return JSON.") from None
        if response.get("status") != "OK":
            comment = str(response.get("comment", "request failed"))
            for field in ("api_key", "api_secret", "cookie"):
                value = self.account.get(field)
                if value:
                    comment = comment.replace(value, "[redacted]")
            raise VPError("Codeforces API: " + comment)
        return response["result"]

    def prepare(self) -> Contest:
        data = self.api("contest.standings", contestId=self.id, showUnofficial=False, participantTypes="CONTESTANT")
        raw = data["contest"]
        if raw["type"] != "ICPC":
            raise VPError("This Gym contest does not use ICPC scoring.")
        if raw["phase"] != "FINISHED":
            raise VPError("Only completed original contests can be started as a native VP.")
        soup = self.web.soup(self.url, params={"locale": "en"})
        dashboard_html = str(soup)
        names = [raw["name"]] + [h.get_text(" ", strip=True) for h in soup.select("h1,h2")]
        name = directory_name(names)
        pdfs = {}
        for a in soup.select("a[href]"):
            label = a.get_text(" ", strip=True)
            href = urljoin(self.url, a["href"])
            if re.search(r"statement|problem.*pdf|题面", label, re.I) and (".pdf" in href.lower() or "attachments" in href):
                pdfs["Statements.pdf"] = href
                break
        if not pdfs:
            soup = self.web.soup(self.url + "/attachments", params={"locale": "en"})
            for a in soup.select("a[href]"):
                href = urljoin(self.url, a["href"])
                if re.search(r"\.pdf(?:$|\?)", href, re.I) and re.search(r"statement|problem", a.get_text(" ", strip=True), re.I):
                    pdfs["Statements.pdf"] = href
                    break
        if not pdfs:
            for problem in data["problems"]:
                label = problem["index"]
                p = self.web.soup(self.url + f"/problem/{label}", params={"locale": "en"})
                for a in p.select("a[href]"):
                    href = urljoin(self.url, a["href"])
                    if re.search(r"\.pdf(?:$|\?)", href, re.I):
                        pdfs[f"{label}.pdf"] = href
                        break
        if not pdfs:
            raise VPError("This Gym does not expose a PDF statement attachment.")
        duration = int(raw["durationSeconds"])
        freeze = raw.get("freezeDurationSeconds")
        medals = medals_from_html(dashboard_html, self.url)
        self._original = data
        return Contest(self.platform, self.id, self.url, name, name,
                       {p["index"]: p["index"] for p in data["problems"]},
                       Rules(duration, freeze_at=duration-int(freeze) if freeze else None),
                       pdfs, medals, raw.get("startTimeSeconds"), raw.get("websiteUrl"))

    def _virtual_party(self):
        data = self.api("contest.standings", contestId=self.id, handles=self.handle,
                        showUnofficial=True, participantTypes="VIRTUAL")
        parties = [r["party"] for r in data["rows"]
                   if r["party"]["participantType"] == "VIRTUAL"
                   and any(m["handle"].lower() == self.handle.lower() for m in r["party"]["members"])
                   and r["party"].get("startTimeSeconds") is not None]
        return max(parties, key=lambda p: p["startTimeSeconds"], default=None)

    def start(self, contest: Contest) -> float:
        soup = self.web.soup(self.url, params={"locale": "en"})
        profile = soup.select_one("#header")
        if not profile or not any(urlsplit(a.get("href", "")).path.lower() == f"/profile/{self.handle}".lower() for a in profile.select("a")):
            raise VPError("The Codeforces login cookie does not belong to the configured handle.")
        old = self._virtual_party()
        if old and time.time() < old["startTimeSeconds"] + contest.rules.duration:
            self.start_time = float(old["startTimeSeconds"])
            return self.start_time
        registration = self.web.soup(self.origin + f"/contestRegistration/{self.id}/virtual/true", params={"locale": "en"})
        form = registration.select_one("form#registerForm")
        if form is None:
            form = next((f for f in registration.select("form") if f.select_one('[name="startTime"], [name="virtualStartTime"]')), None)
        if form is None:
            raise VPError("Codeforces did not return its virtual participation registration form.")
        payload = form_values(form)
        submit = form.select_one('input[type="submit"][name], button[type="submit"][name]')
        if submit:
            payload[submit["name"]] = submit.get("value", "")
        if not payload.get("csrf_token"):
            raise VPError("Codeforces did not provide a registration CSRF token.")
        # Preserve the platform's observed start/timezone fields. In particular,
        # never use a local-clock guess for the native VP start.
        self.web.post(urljoin(self.origin, form.get("action") or f"/contestRegistration/{self.id}/virtual/true"), payload)
        party = self._virtual_party()
        if not party or (old and party["startTimeSeconds"] == old["startTimeSeconds"]):
            raise VPError("Codeforces did not confirm a new native VIRTUAL participation and start time.")
        self.start_time = float(party["startTimeSeconds"])
        return self.start_time

    def submissions(self, contest: Contest) -> list[Submission]:
        if self.start_time is None:
            raise VPError("The native Gym VP start has not been verified.")
        result = {}
        offset = 1
        while True:
            data = self.api("contest.status", contestId=self.id, handle=self.handle, **{"from": offset, "count": 1000})
            reached_start = False
            for raw in data:
                author = raw["author"]
                if raw["creationTimeSeconds"] < self.start_time:
                    reached_start = True
                    continue
                if not any(m["handle"].lower() == self.handle.lower() for m in author["members"]):
                    continue
                native = author["participantType"] == "VIRTUAL" and author.get("startTimeSeconds") == self.start_time
                practice = (author["participantType"] == "PRACTICE"
                            and raw["creationTimeSeconds"] >= self.start_time + contest.rules.duration)
                if not (native or practice):
                    continue
                label = raw["problem"]["index"]
                if label not in contest.problems:
                    continue
                sid = str(raw["id"])
                relative = raw["relativeTimeSeconds"] if native else raw["creationTimeSeconds"] - self.start_time
                result[sid] = Submission(sid, "me", label, float(relative), verdict(raw.get("verdict")))
            if len(data) < 1000 or reached_start:
                self.submissions_complete = True
                break
            offset += 1000
        return list(result.values())

    @staticmethod
    def rows_from_api(data: dict, contest: Contest) -> list[TeamScore]:
        labels = [p["index"] for p in data["problems"]]
        rows = []
        for raw in data["rows"]:
            party = raw["party"]
            if party["participantType"] != "CONTESTANT":
                continue
            members = ";".join(m["handle"] for m in party["members"])
            ident = "team:" + str(party["teamId"]) if "teamId" in party else "ghost:" + party.get("teamName", "") if party.get("ghost") else "user:" + members
            cells = {}
            for label, result in zip(labels, raw["problemResults"]):
                solved = result.get("points", 0) > 0
                t = result.get("bestSubmissionTimeSeconds")
                if solved and t is None:
                    raise VPError("The Gym original scoreboard lacks solve times needed for DOMjudge ties.")
                cells[label] = Cell(int(result.get("rejectedAttemptCount", 0)) + int(solved), 0,
                                    solved, int(t // 60) if t is not None else None)
            rows.append(TeamScore(ident, party.get("teamName") or members, cells,
                                  penalty_minutes=contest.rules.penalty_minutes,
                                  known_penalty=int(raw["penalty"])))
        return rows

    def reference(self, contest: Contest, elapsed: float) -> Reference:
        if elapsed < contest.rules.duration:
            return Reference(message="Original scoreboard is final-only; live statistics are unavailable.")
        if self._original is None:
            self._original = self.api("contest.standings", contestId=self.id, showUnofficial=False, participantTypes="CONTESTANT")
        released = contest.rules.freeze_at is None
        if not released:
            # Original historic 'frozen=false' cannot release the current VP.
            soup = self.web.soup(self.url + "/standings", params={"locale": "en"})
            text = soup.get_text(" ", strip=True)
            native_finished = bool(re.search(r"virtual (?:participation|contest).*?(?:has ended|finished|completed)", text, re.I))
            released = native_finished and not re.search(r"scoreboard (?:is |remains )?frozen", text, re.I)
        if contest.rules.frozen(elapsed, released):
            return Reference(released=released, message="Waiting for the native VP scoreboard to unfreeze.")
        final = self.api("contest.standings", contestId=self.id, showUnofficial=False, participantTypes="CONTESTANT")
        self._original = final
        complete = final["contest"]["phase"] == "FINISHED" and not final["contest"].get("frozen", False)
        rows = self.rows_from_api(final, contest)
        return Reference(rows, complete, complete, released, "" if complete else "Waiting for original final standings.")
