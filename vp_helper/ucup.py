"""The Universal Cup team's official contest window and combined standings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from urllib.parse import parse_qs, urlsplit

from .browser_login import is_ucup_team
from .models import Contest, Reference, VPError
from .parsing import UCUP_HOST, js_value, qoj_account_name, timestamp
from .qoj import QOJ


class UCup(QOJ):
    def _registration(self, first=None, duration: int | None = None) -> tuple[float, int] | None:
        path = f"/contest/{self.id}/registrants"
        page = 1
        soup = first if first is not None else self._page("/registrants")
        while True:
            for table in soup.select("table"):
                headings = {th.get_text(" ", strip=True).lower(): i for i, th in enumerate(table.select("thead th"))}
                if not {"username", "start time", "duration"} <= headings.keys():
                    continue
                for row in table.select("tbody tr"):
                    cells = row.select("td")
                    if len(cells) <= max(headings.values()):
                        continue
                    user = cells[headings["username"]].select_one('.uoj-username, a[href*="/user/profile/"]')
                    if qoj_account_name(user) != self.handle:
                        continue
                    date_cell = cells[headings["start time"]]
                    tag = date_cell.select_one("time[datetime]")
                    minutes = cells[headings["duration"]].get_text(strip=True)
                    if not minutes.isascii() or not minutes.isdigit() or int(minutes) <= 0:
                        raise VPError("UCup did not supply the team's official contest duration.")
                    started = timestamp(tag["datetime"] if tag else date_cell.get_text(strip=True))
                    return started, int(minutes) * 60
            pages = [page]
            for link in soup.select(".pagination a[href]"):
                target = urlsplit(link["href"])
                number = parse_qs(target.query).get("page", [""])[0]
                if target.path == path and number.isascii() and number.isdigit():
                    pages.append(int(number))
            if page >= max(pages):
                return None
            page += 1
            soup = self._page("/registrants", page=page)

    def prepare(self) -> Contest:
        soup = self._page()
        if not is_ucup_team(self.handle) or not self._matches_account(soup):
            raise VPError(f"Sign in with your UCup team account in Firefox at https://{UCUP_HOST}/login.")
        slot = self._registration()
        if slot is None:
            raise VPError(f"No official window was found for this team. Choose one on the contest page at {self.url}.")
        started, duration = slot
        if time.time() < started:
            scheduled = datetime.fromtimestamp(started, timezone(timedelta(hours=8)))
            raise VPError(f"Your team is registered for {scheduled:%Y-%m-%d %H:%M:%S} (UTC+8). Run ucup {self.id} when the official window starts.")
        contest = self._prepare(soup, ucup=True, duration=duration)
        contest.platform = "ucup"
        self.start_time = started
        return contest

    def start(self, contest: Contest) -> float:
        if self.start_time is None:
            raise VPError("The team's official UCup time window has not been verified.")
        return self.start_time

    def submit_configuration(self, contest: Contest) -> dict:
        return {**super().submit_configuration(contest), "platform": "ucup"}

    def reference(self, contest: Contest, elapsed: float) -> Reference:
        soup = self._page("/standings", show_unofficial="true")
        html = str(soup)
        if not is_ucup_team(self.handle) or js_value(html, "my_name") != self.handle:
            raise VPError(f"Sign in with your UCup team account in Firefox at https://{UCUP_HOST}/login.")
        result = self.reference_from_html(html, contest, elapsed)
        result.released = (elapsed >= contest.rules.duration and "Contest Ended" in soup.stripped_strings
                           and not any(cell.pending for row in result.rows for cell in row.cells.values()))
        return result
