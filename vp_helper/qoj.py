from __future__ import annotations

from datetime import datetime
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .http import WebSession, form_values
from .models import Cell, Contest, Reference, Rules, Submission, TeamScore, VPError
from .originals import classify, load_metadata
from .parsing import directory_name, js_value, medals_from_html, seconds, timestamp
from .scoring import verdict


class QOJ:
    def __init__(self, url: str, account: dict):
        self.url = url
        self.id = urlsplit(url).path.rsplit("/", 1)[-1]
        self.origin = f"https://{urlsplit(url).hostname}"
        self.account = account
        self.web = WebSession(self.origin, account)
        self.handle = account.get("handle", "")
        self.start_time: float | None = None
        self.submissions_complete = False
        self._finished_start: float | None = None
        self._relative_start_verified: float | None = None

    def _page(self, suffix: str = "", **params) -> BeautifulSoup:
        return self.web.soup(self.url + suffix, params={"locale": "en", **params})

    def prepare(self) -> Contest:
        soup = self._page()
        titles = [h.get_text(" ", strip=True) for h in soup.select("h1")]
        titles.extend(a.get_text(" ", strip=True) for a in soup.select('a[href*="?v="]'))
        name = directory_name(titles)
        problem_map = {}
        for row in soup.select("tr"):
            a = row.select_one(f'a[href*="/contest/{self.id}/problem/"]')
            cells = row.select("td")
            if a and cells:
                label = cells[0].get_text(strip=True)
                if re.fullmatch(r"[A-Z][0-9]*", label):
                    problem_map[label] = urlsplit(a["href"]).path.rsplit("/", 1)[-1]
        if not problem_map:
            raise VPError("QOJ did not return the contest problem list.")
        text = soup.get_text(" ", strip=True)
        if "Ruleset: ICPC" not in text:
            raise VPError("This QOJ contest does not use ICPC scoring.")
        freeze = re.search(r"Scoreboard will be frozen after\s*(\d+:\d{2}:\d{2})", text)
        penalty = re.search(r"Penalty:\s*(\d+)\s*minute", text)
        registry = self._page("/registrants")
        duration = self._registry_duration(registry)
        if not duration:
            # The public contest list carries explicit durations, including old contests.
            listing = self.web.soup(self.origin + "/contests", params={"locale": "en"})
            for row in listing.select("tr"):
                if not any(urlsplit(a.get("href", "")).path == f"/contest/{self.id}" for a in row.select("a")):
                    continue
                raw = row.get_text(" ", strip=True)
                match = re.search(r"([\d.]+)\s*hours?", raw)
                if match:
                    duration = int(float(match[1]) * 3600)
                match = re.search(r"(\d+)\s*minutes?", raw)
                if match and not duration:
                    duration = int(match[1]) * 60
        if not duration:
            raise VPError("QOJ did not provide the contest duration; a five-hour duration will not be guessed.")
        pdfs = {}
        for a in soup.select('a[href*="download.php"]'):
            label = a.get_text(" ", strip=True)
            if re.search(r"Statements?\b|题面", label, re.I):
                suffix = "zh-cn" if "zh" in label else "en"
                pdfs[f"Statements-{suffix}.pdf"] = urljoin(self.url, a["href"])
        if not pdfs:
            # A small number of contests use individual problem attachments.
            for label, pid in problem_map.items():
                p = self._page(f"/problem/{pid}")
                link = p.select_one('a[href$=".pdf"], iframe[src*=".pdf"], object[data*=".pdf"]')
                if link:
                    pdfs[f"{label}.pdf"] = urljoin(self.url, link.get("href") or link.get("src") or link.get("data"))
        if not pdfs:
            raise VPError("QOJ did not expose a statement PDF for this contest.")
        return Contest("qoj", self.id, self.url, name, name, problem_map,
                       Rules(duration, int(penalty[1]) if penalty else 20,
                             freeze_at=seconds(freeze[1]) if freeze else None), pdfs)

    @staticmethod
    def _registry_duration(soup) -> int | None:
        for row in soup.select("tr"):
            cells = row.select("td")
            if len(cells) >= 5 and cells[-1].get_text(strip=True).isdigit():
                return int(cells[-1].get_text(strip=True)) * 60
        return None

    def _registration(self, first=None, duration: int | None = None) -> tuple[float, int] | None:
        soup = first or self._page("/registrants", show_unofficial="true")
        last = max([1] + [int(m[1]) for a in soup.select("a[href]")
                          if (m := re.search(r"[?&]page=(\d+)", a["href"]))])
        for page in [1] + list(range(last, 1, -1)):
            if page != 1:
                soup = self._page("/registrants", show_unofficial="true", page=page)
            has_virtual_table = any(len(table.select("thead th")) == 3 for table in soup.select("table"))
            records = []
            for row in soup.select("tr"):
                cells = row.select("td")
                if len(cells) not in (3, 5) or has_virtual_table and len(cells) != 3:
                    continue
                user = cells[1].select_one('.uoj-username, a[href*="/user/profile/"]')
                user_id = urlsplit(user["href"]).path.rsplit("/", 1)[-1] if user and user.get("href") else user.get_text(strip=True) if user else ""
                if user_id != self.handle:
                    continue
                date_cell = cells[2] if len(cells) == 3 else cells[-2]
                node = date_cell.select_one("time[datetime]")
                if node:
                    value = timestamp(node["datetime"])
                else:
                    value = timestamp(date_cell.get_text(strip=True))
                minutes = cells[-1].get_text(strip=True) if len(cells) == 5 else ""
                length = int(minutes) * 60 if minutes.isdigit() else duration or self._registry_duration(soup)
                if length and value > timestamp("2000-01-01 00:00:00"):
                    records.append((value, length))
            if records:
                return max(records)
        return None

    def start(self, contest: Contest) -> float:
        soup = self._page()
        nav = soup.select_one("ul.nav-pills")
        if not nav or not any(urlsplit(a.get("href", "")).path == f"/user/profile/{self.handle}" for a in nav.select("a")):
            raise VPError("The QOJ login cookie does not belong to the configured handle.")
        current = self._registration(duration=contest.rules.duration)
        if current and time.time() < current[0] + current[1]:
            self.start_time = current[0]
            contest.rules.duration = current[1]
            return self.start_time
        form = soup.select_one("form#form-regvir")
        if form is None:
            if current:
                self.start_time = current[0]
                contest.rules.duration = current[1]
                return self.start_time
            raise VPError("QOJ did not offer native virtual participation for this account.")
        payload = form_values(form)
        payload["submit-regvir"] = "regvir"
        if not payload.get("_token"):
            raise VPError("QOJ did not provide a native VP start token.")
        self.web.post(urljoin(self.url, form.get("action", self.url)), payload)
        confirmed = self._registration(duration=contest.rules.duration)
        if not confirmed:
            raise VPError("QOJ did not confirm a VP start time in the account registration record.")
        if current and confirmed[0] == current[0]:
            raise VPError("QOJ did not create a new native VP; the old participation is already finished.")
        self.start_time = confirmed[0]
        contest.rules.duration = confirmed[1]
        return self.start_time

    def submit_configuration(self, contest: Contest) -> dict:
        ids = [int(x) for x in contest.problems.values()]
        return {"name": contest.name, "base_url": self.url, "problems": "".join(contest.problems),
                "offset": ids[0], "user_agent": self.web.session.headers["User-Agent"],
                "cookie": self.account.get("cookie", ""), "problem_ids": contest.problems}

    def submissions(self, contest: Contest) -> list[Submission]:
        if self.start_time is None:
            raise VPError("The native QOJ VP start has not been verified.")
        result = {}
        inverse = {str(pid): label for label, pid in contest.problems.items()}
        page = 1
        while True:
            soup = self._page("/submissions", submitter=self.handle, page=page)
            found = 0
            before = False
            for row in soup.select("tr"):
                cells = row.select("td")
                id_link = row.select_one('a[href^="/submission/"]')
                user = row.select_one('.uoj-username, a[href*="/user/profile/"]')
                problem = row.select_one(f'a[href*="/contest/{self.id}/problem/"]')
                if not id_link or not user or not problem:
                    continue
                user_id = urlsplit(user["href"]).path.rsplit("/", 1)[-1] if user.get("href") else user.get_text(strip=True)
                if user_id != self.handle:
                    continue
                found += 1
                date_node = row.select_one("time[datetime]")
                dates = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row.get_text(" ", strip=True))
                if date_node or dates:
                    at = timestamp(date_node["datetime"] if date_node else dates[0]) - self.start_time
                elif cells and re.fullmatch(r"\d+:\d{2}:\d{2}", cells[-1].get_text(strip=True)) and any(
                        urlsplit(a.get("href", "")).path == f"/contest/{self.id}" for a in row.select("sup a[href]")):
                    # QOJ emits relative time only for submissions explicitly
                    # attached to this native contest, marked by the # link.
                    if self._relative_start_verified != self.start_time:
                        current = self._registration(duration=contest.rules.duration)
                        if not current or current[0] != self.start_time:
                            raise VPError("The current native QOJ VP start no longer matches this session.")
                        self._relative_start_verified = self.start_time
                    at = seconds(cells[-1].get_text(strip=True))
                else:
                    raise VPError("QOJ did not provide the submission timestamp.")
                if at < 0:
                    before = True
                    continue
                pid = urlsplit(problem["href"]).path.rsplit("/", 1)[-1]
                label = inverse.get(pid)
                if not label:
                    continue
                sid = urlsplit(id_link["href"]).path.rsplit("/", 1)[-1]
                # Score/status follows submitter and problem; infer column from its header.
                table = row.find_parent("table")
                headers = [h.get_text(" ", strip=True).lower() for h in table.select("thead th")]
                if not headers:
                    headers = [h.get_text(" ", strip=True).lower() for h in table.select("tr th")]
                column = next((i for i, h in enumerate(headers) if h in ("result", "score", "status")), 3)
                node = cells[column] if column < len(cells) else None
                if node is None:
                    raise VPError("QOJ submission result column is missing.")
                status = node.get_text(" ", strip=True)
                score = float(status) if re.fullmatch(r"\d+(?:\.\d+)?", status) else None
                if score is not None and score < 100:
                    # QOJ numeric zero alone does not distinguish WA/RE/MLE/CE.
                    detail = self.web.soup(self.origin + f"/submission/{sid}")
                    candidates = [x.get_text(" ", strip=True) for x in detail.select(".uoj-status, .panel-title, .card-header, .uoj-score, table td")]
                    status = next((s for s in candidates if any(t in s.upper() for t in ("WRONG", "ERROR", "LIMIT", "ACCEPTED", "JUDGING", "WAITING"))), status)
                result[sid] = Submission(sid, "me", label, at, verdict(status, score))
            pages = [int(m[1]) for a in soup.select("a[href]") if (m := re.search(r"[?&]page=(\d+)", a["href"]))]
            if before or not found or page >= max(pages, default=page):
                self.submissions_complete = True
                break
            page += 1
        return list(result.values())

    @staticmethod
    def reference_from_html(html: str, contest: Contest, elapsed: float, *, released: bool = False) -> Reference:
        raw_rows = js_value(html, "standings")
        raw_scores = js_value(html, "score")
        labels = js_value(html, "problems_id", list(contest.problems))
        if not isinstance(raw_rows, list) or not isinstance(raw_scores, dict):
            return Reference(message="QOJ did not provide the original scoreboard data.", released=released)
        if js_value(html, "icpc_partial_scores", False):
            return Reference(message="The original contest uses partial ICPC scores; its ranking rule is unsupported.", released=released)
        rows = []
        for raw in raw_rows:
            info = raw[2]
            # Imported category 3 may include original starred teams whose flags
            # were lost during import. Restore eligibility from the original archive.
            if len(info) < 4 or info[2] not in (3, 4):
                continue
            if info[0] not in raw_scores:
                return Reference(message="An original QOJ team is missing problem results.", released=released)
            cells = {label: Cell() for label in contest.problems}
            values = raw_scores.get(info[0], {})
            items = values.items() if isinstance(values, dict) else enumerate(values)
            for index, col in items:
                if not isinstance(col, list) or not str(index).isdigit() or int(index) >= len(labels) or len(col) < 6:
                    continue
                label = labels[int(index)]
                if label not in cells:
                    continue
                solved = col[0] == col[4] and col[4] > 0
                cells[label] = Cell(int(col[3]) + int(solved), int(col[5]), solved,
                                    int(col[1] // 60) if solved else None)
            row = TeamScore(str(info[0]), info[3], cells, eligible=info[2] == 3,
                            penalty_minutes=contest.rules.penalty_minutes)
            if row.penalty != int(raw[1] // 60):
                return Reference(message="The original QOJ penalty data does not match the contest scoring rule.", released=released)
            rows.append(row)
        if not rows:
            return Reference(message="QOJ has no explicitly classified original contest ghosts.", released=released)
        medals = medals_from_html(html, contest.url + "/standings")
        # A mixed UCup/ghost scoreboard's cutoff describes its combined ranks.
        # Accept it only when every ranked row belongs to the original contest.
        if medals and len(rows) == len(raw_rows):
            contest.medals = medals
        final = elapsed >= contest.rules.duration and not contest.rules.frozen(elapsed, released)
        if not final:
            return Reference(message="Original scoreboard is final-only; live statistics are unavailable.", released=released)
        try:
            classify(rows, contest)
        except VPError as exc:
            return Reference(message=str(exc), released=released)
        return Reference(rows, complete=True, statistics_complete=True, released=released)

    def reference(self, contest: Contest, elapsed: float) -> Reference:
        soup = self._page("/standings", show_unofficial="true")
        html = str(soup)
        released = False
        if elapsed >= contest.rules.duration:
            # Require the current account's own native VP end/unfreeze evidence.
            my_name = js_value(html, "my_name", "")
            text = soup.get_text(" ", strip=True)
            native_ended = bool(re.search(r"Your (?:virtual (?:contest|participation)|participation in the contest)[^.]*?(?:has ended|has been finished)", text, re.I))
            if my_name == self.handle and native_ended:
                if self._finished_start != self.start_time:
                    current = self._registration(duration=contest.rules.duration)
                    if current and current[0] == self.start_time:
                        form = soup.select_one("form#form-finish-panel")
                        if form:
                            payload = form_values(form)
                            button = form.select_one('button[type="submit"][name], input[type="submit"][name]')
                            if not payload.get("_token") or button is None:
                                raise VPError("QOJ did not provide its native finished-VP update token.")
                            payload[button["name"]] = button.get("value", "")
                            action = urljoin(self.url, form.get("action") or self.url)
                            if urlsplit(action).hostname != urlsplit(self.origin).hostname:
                                raise VPError("QOJ returned an unexpected native finished-VP update destination.")
                            self.web.post(action, payload)
                            soup = self._page("/standings", show_unofficial="true")
                            html = str(soup)
                        self._finished_start = self.start_time
            own = js_value(html, "score", {}).get(self.handle, {})
            values = own.values() if isinstance(own, dict) else own
            no_hidden = all(isinstance(col, list) and len(col) >= 6 and not col[5] for col in values)
            released = (self.start_time is not None and self._finished_start == self.start_time
                        and js_value(html, "my_name", "") == self.handle and no_hidden)
        if elapsed >= contest.rules.duration and not contest.rules.frozen(elapsed, released):
            try:
                metadata = load_metadata(contest)
            except VPError as exc:
                return Reference(message=str(exc), released=released)
            contest.extra["original_eligibility"] = metadata
        return self.reference_from_html(html, contest, elapsed, released=released)
