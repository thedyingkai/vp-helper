from __future__ import annotations

from dataclasses import replace
from collections import Counter
from datetime import datetime
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from .http import WebSession, form_values
from .models import Cell, Contest, Reference, Rules, Submission, TeamScore, VPError
from .originals import OriginalReplay, cache_archive_submissions, classify, eligibility_lookup, identity, load_archive_submissions, load_metadata, lookup_team
from .parsing import ccpc_reveal_count, directory_name, js_value, medals_from_html, qoj_account_name, seconds, timestamp
from .provincial import provincial_parts, ranking_group
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
        self._verdict_cache = {}
        self._detail_retry = {}
        self.submission_message = ""
        self._replay = None
        self._replay_error = ""
        self._next_replay_attempt = 0.0
        self._public_web = None

    def _page(self, suffix: str = "", **params) -> BeautifulSoup:
        return self.web.soup(self.url + suffix, params={"locale": "en", **params})

    def prepare(self, *, ucup: bool = False) -> Contest:
        return self._prepare(self._page(), ucup=ucup)

    def _prepare(self, soup: BeautifulSoup, *, ucup: bool = False, duration: int | None = None) -> Contest:
        titles = [h.get_text(" ", strip=True) for h in soup.select("h1")]
        titles.extend(a.get_text(" ", strip=True) for a in soup.select('a[href*="?v="]'))
        if ucup:
            name = next((title for title in titles if re.search(
                r"\bUniversal Cup\b\.?\s+(?:Extra\s+)?(?:Stage|Semifinals?|Finals?|Warm[ -]up)\b", title, re.I)), None)
            if name is None:
                raise VPError("This QOJ contest does not have a Universal Cup title. Use its UCup contest ID.")
            directory = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name).strip(" .")
        else:
            directory = name = directory_name(titles)
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
        # Registrants can choose a shorter rated-VP window. It is not the
        # contest duration (4582 already has two-hour rated participations).
        if duration is None:
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
            duration = self._registry_duration(self._page("/registrants"))
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
        contest = Contest("qoj", self.id, self.url, name, directory, problem_map,
                       Rules(duration, int(penalty[1]) if penalty else 20,
                             freeze_at=seconds(freeze[1]) if freeze else None,
                             problem_reveal_count=ccpc_reveal_count(name)), pdfs)
        if ucup:
            contest.extra.update(ucup=True, ranking_group_label="All teams")
        elif provincial_parts(name):
            contest.extra["provincial_group"] = self.account.get("provincial_group", "official")
            ranking_group(contest)
        return contest

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

    def _matches_account(self, soup: BeautifulSoup) -> bool:
        nav = soup.select_one("ul.nav-pills")
        return bool(self.handle and nav and any(qoj_account_name(node) == self.handle
                                                for node in nav.select("a[href], .uoj-username")))

    def start(self, contest: Contest) -> float:
        current = self._registration(duration=contest.rules.duration)
        soup = self._page()
        if not self._matches_account(soup):
            raise VPError("The QOJ login cookie does not belong to the configured handle.")
        if current and time.time() < current[0] + current[1]:
            self.start_time = current[0]
            contest.rules.duration = current[1]
            return self.start_time
        form = soup.select_one("form#form-regvir")
        if form is None:
            # The user may have started in the browser just after the first read.
            current = current or self._registration(duration=contest.rules.duration)
            if current:
                self.start_time = current[0]
                contest.rules.duration = current[1]
                return self.start_time
            raise VPError("QOJ did not offer native virtual participation for this account.")
        button = form.select_one('button[name="submit-regvir"], input[type="submit"][name="submit-regvir"]')
        if form.get("method", "").lower() != "post" or button is None or button.has_attr("disabled"):
            raise VPError("QOJ did not provide an enabled VP start button in its form.")
        payload = form_values(form)
        payload[button["name"]] = button.get("value", "")
        if not payload.get("_token"):
            raise VPError("QOJ did not provide a native VP start token.")
        form_page = self.url + "?locale=en"
        action = urljoin(form_page, form.get("action") or form_page)
        post_error = None
        site_message = ""
        try:
            response = self.web.post(action, payload, form_page=form_page)
            site_message = self._start_message(response)
        except VPError as exc:
            # The request may have reached QOJ even if its response was lost.
            # Verify the registration before reporting failure; never send it again.
            post_error = exc
        confirmed = None
        registration_error = None
        for delay in (0, 1, 2):
            if delay:
                time.sleep(delay)
            try:
                confirmed = self._registration(duration=contest.rules.duration)
                registration_error = None
            except VPError as exc:
                registration_error = exc
                continue
            if confirmed and (not current or confirmed[0] > current[0]):
                self.start_time = confirmed[0]
                contest.rules.duration = confirmed[1]
                return self.start_time
            if site_message:
                break
        registry_hint = f"Check your registration in the browser at {self.url}/registrants."
        if post_error:
            raise VPError(f"{post_error} {registry_hint}") from post_error
        if site_message:
            raise VPError(f"QOJ could not start the VP: {site_message}")
        if registration_error:
            raise VPError(f"The native VP start could not be verified: {registration_error}. {registry_hint}")
        if current and confirmed and confirmed[0] <= current[0]:
            raise VPError("QOJ did not create a new native VP; the old participation is already finished.")
        raise VPError(f"QOJ did not confirm a VP start time in the account registration record. {registry_hint}")

    @staticmethod
    def _start_message(response) -> str:
        # QOJ can return a form rejection as an HTTP 200 message page.
        # Keep that explanation instead of replacing it with a generic error.
        content = getattr(response, "content", None)
        if not isinstance(content, (str, bytes)):
            return ""
        soup = BeautifulSoup(content, "html.parser")
        errors = soup.select(".alert-danger, .has-error .help-block")
        if errors:
            return " ".join(node.get_text(" ", strip=True) for node in errors)[:500]
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if not re.match(r"^(?:Error|Message|Notice|错误|消息|提示)(?:\b|\s|[-:：])", title, re.I):
            return ""
        main = soup.select_one(".uoj-content, main, #content")
        if main:
            for node in main.select("script, style, nav, header, footer, form"):
                node.decompose()
            return main.get_text(" ", strip=True)[:500] or title
        return title[:500]

    def submit_configuration(self, contest: Contest) -> dict:
        ids = [int(x) for x in contest.problems.values()]
        return {"name": contest.name, "base_url": self.url, "problems": "".join(contest.problems),
                "handle": self.handle,
                "offset": ids[0], "user_agent": self.web.session.headers["User-Agent"],
                "cookie": self.account.get("cookie", ""), "problem_ids": contest.problems}

    def submissions(self, contest: Contest) -> list[Submission]:
        if self.start_time is None:
            raise VPError("The native QOJ VP start has not been verified.")
        result = {}
        unresolved = []
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
                judging = bool(row.select_one('[id^="status_details_"]') or
                               re.search(r"update_judgement_status_details\s*\(", str(row)))
                if judging:
                    result[sid] = Submission(sid, "me", label, at, "PENDING")
                    self._verdict_cache.pop(sid, None)
                    self._detail_retry.pop(sid, None)
                    continue
                score = float(status) if re.fullmatch(r"\d+(?:\.\d+)?", status) else None
                if score is not None and score < 100:
                    # QOJ numeric zero alone does not distinguish WA/RE/MLE/CE.
                    cached = self._verdict_cache.get(sid)
                    if cached and cached[0] in (None, score) and time.monotonic() < cached[2]:
                        status = cached[1]
                    else:
                        now = time.monotonic()
                        retry_at, delay = self._detail_retry.get(sid, (0.0, 0.0))
                        if now < retry_at:
                            parsed = "UNAVAILABLE"
                        else:
                            try:
                                detail = self.web.soup(self.origin + f"/submission/{sid}",
                                                       params={"locale": "en"}, retry=False)
                                parsed = self.detail_verdict(detail, score)
                            except VPError:
                                # A zero score is final, but can mean WA, CE or
                                # another failure. Do not invent a scored WA or
                                # keep an obsolete submission receipt as pending.
                                parsed = "UNAVAILABLE"
                                delay = min(120.0, max(15.0, delay * 2))
                                self._detail_retry[sid] = (time.monotonic() + delay, delay)
                            else:
                                self._detail_retry.pop(sid, None)
                                if parsed != "PENDING":
                                    self._verdict_cache[sid] = (score, parsed, time.monotonic() + 300 + int(sid) % 300)
                        if parsed == "UNAVAILABLE":
                            unresolved.append(sid)
                            result[sid] = Submission(sid, "me", label, at, parsed)
                            continue
                        status = parsed
                result[sid] = Submission(sid, "me", label, at, verdict(status, score))
            pages = [int(m[1]) for a in soup.select("a[href]") if (m := re.search(r"[?&]page=(\d+)", a["href"]))]
            if before or not found or page >= max(pages, default=page):
                self.submissions_complete = True
                break
            page += 1
        self.submission_message = ("Result unavailable for " + ", ".join("#" + sid for sid in unresolved)
                                   + " · retrying details; other submissions are synced" if unresolved else "")
        return list(result.values())

    @staticmethod
    def detail_verdict(detail: BeautifulSoup, score: float) -> str:
        # Current QOJ nests each test's verdict inside a Bootstrap column. A
        # card header begins with "Test #N: score: ...", which is not a verdict.
        # Limit parsing to the latest Details card, excluding judging history.
        headers = [node for node in detail.select(".card-header, .panel-heading")
                   if node.get_text(" ", strip=True).lower() == "details"]
        scope = headers[-1].parent if headers else detail
        pending = False
        for node in scope.select(".uoj-status, .col-sm-2, .panel-title, .card-header, table td"):
            text = node.get_text(" ", strip=True)
            if not text:
                continue
            try:
                parsed = verdict(text)
            except VPError:
                continue
            if parsed == "PENDING":
                pending = True
            elif parsed not in ("CORRECT", "TOO-LATE"):
                return parsed
            # Passing an earlier test must never override the final zero score.
        if pending:
            return "PENDING"
        raise VPError("QOJ did not expose a supported detailed failure verdict.")

    def seed_submissions(self, submissions: list[Submission]) -> None:
        for item in submissions:
            if item.verdict not in ("PENDING", "CORRECT", "TOO-LATE", "UNAVAILABLE"):
                self._verdict_cache[item.id] = (None, item.verdict, time.monotonic() + 300 + int(item.id) % 300)

    @staticmethod
    def reference_from_html(html: str, contest: Contest, elapsed: float, *, released: bool = False,
                            handle: str = "") -> Reference:
        official = contest.platform == "ucup"
        ucup = official or bool(contest.extra.get("ucup"))
        raw_rows = js_value(html, "standings")
        raw_scores = js_value(html, "score")
        labels = js_value(html, "problems_id", list(contest.problems))
        if ucup and raw_rows == raw_scores == []:
            raw_scores = {}
        if not isinstance(raw_rows, list) or not isinstance(raw_scores, dict) or not isinstance(labels, list):
            return Reference(message="QOJ did not provide the original scoreboard data.", released=released)
        if js_value(html, "icpc_partial_scores", False):
            return Reference(message="The original contest uses partial ICPC scores; its ranking rule is unsupported.", released=released)
        if ucup and (labels != list(contest.problems) or js_value(html, "show_unofficial") is not True):
            return Reference(message="QOJ did not return the full UCup scoreboard for these problems.", released=released)
        rows = []
        seen = set()
        for raw in raw_rows:
            if ucup and (not isinstance(raw, list) or len(raw) < 3 or type(raw[1]) not in (int, float) or raw[1] < 0
                         or not isinstance(raw[2], list) or len(raw[2]) < 4):
                return Reference(message="QOJ returned an incomplete UCup team row.", released=released)
            info = raw[2]
            # Imported category 3 may include original starred teams whose flags
            # were lost during import. Restore eligibility from the original archive.
            if (ucup and not official and info[0] == handle) or (not ucup and (len(info) < 4 or info[2] not in (3, 4))):
                continue
            if ucup and (not isinstance(info[0], str) or info[0] in seen):
                return Reference(message="QOJ returned invalid or duplicate UCup team IDs.", released=released)
            seen.add(info[0])
            if info[0] not in raw_scores:
                return Reference(message="An original QOJ team is missing problem results.", released=released)
            cells = {label: Cell() for label in contest.problems}
            values = raw_scores.get(info[0], {})
            if ucup and not isinstance(values, (dict, list)):
                return Reference(message="QOJ returned incomplete UCup problem results.", released=released)
            items = values.items() if isinstance(values, dict) else enumerate(values)
            for index, col in items:
                if col is None:
                    continue  # Sparse JSON arrays use null for unattempted problems.
                if not isinstance(col, list) or not str(index).isdigit() or int(index) >= len(labels) or len(col) < 6:
                    if ucup:
                        return Reference(message="QOJ returned an unsupported UCup problem result.", released=released)
                    continue
                label = labels[int(index)]
                if label not in cells:
                    continue
                if ucup and (type(col[1]) not in (int, float) or type(col[3]) is not int or col[3] < 0):
                    return Reference(message="QOJ returned an invalid UCup result time or attempt count.", released=released)
                if ucup and not official and elapsed < contest.rules.duration and col[1] > elapsed + 5:
                    return Reference(message="QOJ has not provided a UCup scoreboard for the current VP time.", released=released)
                # QOJ's ICPC renderer marks 97 separately and still counts it
                # as one solved problem. Some UCup rounds have these results.
                solved = (col[0] == col[4] and col[4] > 0) or (ucup and col[0] == 97)
                # QOJ stores attempts before the selected submission, even
                # when that last submission failed. Its own UI renders -N
                # and ?N as col[3] + 1, not col[3].
                cells[label] = (Cell(pending=int(col[3]) + 1) if col[5] else
                                Cell(int(col[3]) + 1, 0, solved, int(col[1] // 60) if solved else None))
            penalty = int(raw[1] // 60)
            row = TeamScore(str(info[0]), info[3], cells, eligible=ucup or info[2] == 3,
                            penalty_minutes=contest.rules.penalty_minutes,
                            known_penalty=penalty if ucup else None)
            if not ucup and row.penalty != penalty:
                return Reference(message="The original QOJ penalty data does not match the contest scoring rule.", released=released)
            rows.append(row)
        if ucup:
            view = js_value(html, "ccpc_view")
            if isinstance(view, dict) and view.get("n_revealed", 0) < len(labels):
                return Reference(rows, complete=True, released=released,
                                 message="QOJ has hidden problem labels in this UCup scoreboard.")
            return Reference(rows, complete=True, statistics_complete=True, released=released)
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
        if contest.extra.get("ucup"):
            # Use the account's native snapshot and its visible results across all
            # time windows. The monitor counts the VP's own submissions separately.
            if not self.handle or js_value(html, "my_name") != self.handle:
                return Reference(message="Log in to QOJ in Firefox to read your UCup scoreboard.")
            snapshot_elapsed = max(elapsed, time.time() - self.start_time) if self.start_time is not None else elapsed
            return self.reference_from_html(html, contest, snapshot_elapsed, released=released, handle=self.handle)
        # Keep the public archive connection across metadata and event downloads.
        # This session carries none of the judge account's cookies.
        with requests.Session() as public_web:
            return self._original_reference(html, contest, elapsed, released, public_web,
                                            fetch_final=elapsed < contest.rules.duration)

    @staticmethod
    def external_reference_from_html(html: str, contest: Contest) -> Reference:
        """Read QOJ's public final table, matching only the original roster."""
        soup = BeautifulSoup(html, "html.parser")
        if "Current Time:" in soup.get_text(" ", strip=True):
            return Reference(message="QOJ returned a VP replay instead of the original final standings.")
        metadata = contest.extra["original_eligibility"]
        teams = eligibility_lookup(metadata["teams"])
        problems = list(contest.problems)
        for table in soup.select("table"):
            headers = table.select("tr th")
            labels = [node.get_text(" ", strip=True).split()[0] for node in headers]
            if labels[2:2 + len(problems)] != problems or labels[2 + len(problems):4 + len(problems)] != ["Solved", "Penalty"]:
                continue
            rows = []
            empty_duplicates = Counter(identity(name) for name in metadata.get("ignored_empty_team_names", []))
            for tr in table.select("tr"):
                columns = tr.select("td")
                if len(columns) != len(labels):
                    continue
                display_name = columns[1].get_text(" ", strip=True)
                team = lookup_team(teams, display_name)
                if team is None:
                    continue
                cells = {}
                for problem, node in zip(problems, columns[2:2 + len(problems)]):
                    value = node.get_text(" ", strip=True).lstrip("*")
                    accepted = re.fullmatch(r"\+(\d*)\s+(\d+):(\d{2})", value)
                    rejected = re.fullmatch(r"-(\d+)(?:\s+\d+:\d{2})?", value)
                    if accepted:
                        cells[problem] = Cell(int(accepted[1] or 0) + 1, 0, True,
                                              int(accepted[2]) * 60 + int(accepted[3]))
                    elif rejected:
                        cells[problem] = Cell(int(rejected[1]))
                    elif value == "-":
                        cells[problem] = Cell()
                    else:
                        return Reference(message="An original QOJ final result is frozen or unsupported.")
                row = TeamScore(team["id"], team["name"], cells, penalty_minutes=contest.rules.penalty_minutes)
                solved, penalty = (node.get_text(strip=True) for node in columns[2 + len(problems):4 + len(problems)])
                if not solved.isdigit() or not penalty.isdigit() or (row.solved, row.penalty) != (int(solved), int(penalty)):
                    return Reference(message="The public QOJ final table does not match its problem results.")
                name_key = identity(team["name"])
                if empty_duplicates[name_key] and not any(c.tries or c.pending or c.solved for c in cells.values()):
                    empty_duplicates[name_key] -= 1
                    continue
                rows.append(row)
            try:
                classify(rows, contest)
            except VPError as exc:
                return Reference(message=str(exc))
            return Reference(rows, complete=True, statistics_complete=True, released=True)
        return Reference(message="QOJ did not provide a public final table for these contest problems.")

    def _original_reference(self, html: str, contest: Contest, elapsed: float, released: bool, public_web,
                            *, fetch_final: bool = False) -> Reference:
        try:
            metadata = load_metadata(contest, web=public_web)
        except VPError as exc:
            return Reference(message=str(exc), released=released)
        contest.extra["original_eligibility"] = metadata
        duration = metadata.get("duration")
        if not isinstance(duration, int) or duration < contest.rules.duration:
            return Reference(message="The original contest duration is shorter than the native VP duration.", released=released)
        # An onsite extension can make the original longer than QOJ's native VP.
        # Validate the complete original using its own window; keep the user's
        # timer and the cutoff for their submissions at the native VP duration.
        original = replace(contest, rules=replace(contest.rules, duration=duration))
        if self._replay is not None:
            return self._replay.at(contest, elapsed, released=released)
        # Imported ghosts contain final totals. Validate a separate original
        # event stream against those totals before exposing any partial scores.
        if fetch_final:
            # The authenticated standings are already truncated to VP time.
            # The public results endpoint has the complete imported scoreboard;
            # this separate session deliberately has no account cookies.
            if self._public_web is None:
                self._public_web = WebSession(self.origin, {})
            public = self._public_web.get(self.origin + "/results/QOJ" + self.id, params={"locale": "en"})
            final = self.external_reference_from_html(public.text, original)
        else:
            final = self.reference_from_html(html, original, duration, released=True)
        native_rows = js_value(html, "standings")
        has_import = (not isinstance(native_rows, list) or any(
            not isinstance(row, list) or len(row) < 3 or not isinstance(row[2], list)
            or len(row[2]) < 3 or row[2][2] in (3, 4) for row in native_rows))
        if not final.complete and provincial_parts(contest.name) and not has_import:
            try:
                events = load_archive_submissions(original, metadata, web=public_web)
                replay = OriginalReplay.from_provincial_archive(original, metadata, events)
                verified_final = replay.at(original, duration, released=True)
                classify(verified_final.rows, original)
                contest.medals = original.medals
                contest.extra["ranking_group_label"] = metadata["ranking_group_label"]
                cache_archive_submissions(metadata, events)
                self._replay = replay
                return replay.at(contest, elapsed, released=released)
            except VPError as exc:
                return Reference(released=released, message=str(exc))
        contest.medals = original.medals
        final.released = released
        if not final.complete:
            return final
        if elapsed >= contest.rules.duration and not contest.rules.frozen(elapsed, released):
            return final
        if self._replay is None and time.monotonic() >= self._next_replay_attempt:
            self._next_replay_attempt = time.monotonic() + 120
            try:
                events = load_archive_submissions(original, metadata, web=public_web)
                try:
                    replay = OriginalReplay(original, metadata["teams"], events, final.rows)
                except VPError:
                    offset = duration - contest.rules.duration
                    if offset <= 0:
                        raise
                    # Some QOJ imports shift the entire onsite clock. Accept
                    # the shift only if every team's every final cell agrees.
                    aligned = [replace(event, time=event.time - offset) for event in events]
                    replay = OriginalReplay(original, metadata["teams"], aligned, final.rows)
                self._replay = replay
                cache_archive_submissions(metadata, events)
            except VPError as exc:
                self._replay_error = str(exc)
        if self._replay is not None:
            return self._replay.at(contest, elapsed, released=released)
        return Reference(released=released, message=self._replay_error)
