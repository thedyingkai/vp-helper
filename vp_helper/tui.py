from __future__ import annotations

from dataclasses import dataclass

from rich import box
from rich.cells import set_cell_size
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from .models import Contest, TeamScore


PRACTICE_WRONG = "#d75f00"
PRACTICE_ACCEPTED = "#8700d7"
SCORE_BOX = box.Box("    \n    \n ── \n    \n ── \n ── \n    \n    \n")


def clock(value: float) -> str:
    value = max(0, int(value))
    return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}"


@dataclass
class Dashboard:
    contest: Contest
    elapsed: float
    me: TeamScore
    rank: str
    prize: str
    stats: dict | None
    submissions: list[dict]
    message: str = ""

    def fixed(self, width: int):
        # Equal side columns keep the contest title at the screen's center.
        sides = min(30, max(19, (width - 8) // 3))
        heading = Table.grid(expand=True, padding=0)
        heading.style = "default"
        heading.add_column(width=sides, no_wrap=True)
        heading.add_column(ratio=1, justify="center", no_wrap=True, overflow="ellipsis")
        heading.add_column(width=sides, justify="right", no_wrap=True)
        prize = {"Gold": "Gold Prize", "Silver": "Silver Prize", "Bronze": "Bronze Prize",
                 "None": "No Prize", "Unknown": "Unknown"}.get(self.prize, self.prize)
        result = self.rank + (f"\n{prize}" if prize else "")
        heading.add_row(Text(f" {clock(min(self.elapsed, self.contest.rules.duration))} / {clock(self.contest.rules.duration)}", style="bold default"),
                        Text(self.contest.name, style="bold"), Text(""))
        table = Table(box=SCORE_BOX, padding=(0, 1), style="default",
                      header_style="bold black on #eeeeee", border_style="#bbbbbb",
                      show_edge=False, expand=True)
        for label in ("rank", "team", "solved", "penalty", *self.contest.problems):
            table.add_column(label, justify="left" if label == "team" else "center",
                             no_wrap=label == "rank", min_width=12 if label == "rank" else None)
        cells = []
        for label in self.contest.problems:
            c = self.me.cells[label]
            tries = (f"{c.tries} + {c.pending} tries" if c.pending else
                     f"{c.tries} {'try' if c.tries == 1 else 'tries'}" if c.tries else "—")
            if width < 155:
                tries = f"{c.tries}+{c.pending}" if c.pending else str(c.tries) if c.tries else "—"
            if c.solved:
                cells.append(Text(f"{tries}\n{c.solve_time}", style="black on #60e760"))
            elif c.pending:
                cells.append(Text(tries, style="white on #6666ff"))
            elif c.tries:
                cells.append(Text(tries, style="black on #e87272"))
            else:
                cells.append(Text(tries))
        table.add_row(Text(result, style="bold default"), self.me.name, str(self.me.solved), str(self.me.penalty),
                      *cells, end_section=True)
        for key in ("submitted", "attempted", "accepted"):
            table.add_row("", key, "", "", *(str(self.stats[key][p]) if self.stats else "—"
                                             for p in self.contest.problems))
        labels = self._record_table()
        labels.add_row("problem", "submission", "time", "result", "", style="bold black on #eeeeee")
        return Group(heading, Text(" "), table, Text(" "), labels)

    @staticmethod
    def _record_table():
        results = Table.grid(expand=True, padding=(0, 1))
        results.add_column(width=9, justify="center", no_wrap=True)
        results.add_column(width=14, justify="center", no_wrap=True)
        results.add_column(width=12, justify="center", no_wrap=True)
        results.add_column(ratio=1, no_wrap=True)
        results.add_column(width=10, justify="right", no_wrap=True)
        return results

    def records(self):
        results = self._record_table()
        for item in self.submissions:
            v = item["verdict"]
            practice = item.get("practice", False)
            if practice:
                color = PRACTICE_ACCEPTED if v == "CORRECT" else PRACTICE_WRONG
                values = [item["problem"], "#" + item["id"], clock(item["time"]), v, "practice"]
                results.add_row(*values, style=color)
            else:
                color = "#008000" if v == "CORRECT" else "#808080" if v in ("PENDING", "TOO-LATE") else "#ff0000"
                results.add_row(Text(item["problem"]), Text("#" + item["id"]),
                                Text(clock(item["time"])), Text(v, style=color), "")
        if not self.submissions:
            results.add_row(Text("No submissions yet", style="#555555"))
        return results

    def __rich_console__(self, console, options):
        yield Group(self.fixed(options.max_width), self.records(), Text(self.message, style="#555555"))


def display(contest: Contest, elapsed: float, me: TeamScore, rank: str,
            prize: str, stats: dict | None, submissions: list[dict], message: str = "") -> Dashboard:
    return Dashboard(contest, elapsed, me, rank, prize, stats, submissions, message)


class TerminalDashboard:
    """Fixed scoreboard with every submission in a keyboard/mouse scroll region."""

    def __init__(self):
        self.console = Console(style="default", color_system="256", highlight=False)
        self.offset = 0
        self.page_size = 1
        self.total_lines = 0
        self.pairs = {}

    def __enter__(self):
        import curses
        self.curses = curses
        self.screen = curses.initscr()
        try:
            curses.noecho()
            curses.cbreak()
            self.screen.keypad(True)
            self.screen.nodelay(True)
            curses.start_color()
            self.default_colors = False
            try:
                curses.use_default_colors()
                self.default_colors = True
            except curses.error:
                pass
            self.background = self._attributes(None)
            self.screen.bkgd(" ", self.background)
            try:
                curses.curs_set(0)
                curses.mousemask(curses.ALL_MOUSE_EVENTS)
            except curses.error:
                pass
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        self.screen.keypad(False)
        self.curses.nocbreak()
        self.curses.echo()
        self.curses.endwin()

    def _color(self, color, default: tuple[int, int, int]) -> int:
        if (color is None or color.is_default) and self.default_colors:
            return -1
        rgb = tuple(color.get_truecolor()) if color else default
        colors = self.curses.COLORS
        if colors >= 256:
            levels = (0, 95, 135, 175, 215, 255)
            indices = [min(range(6), key=lambda i: abs(levels[i] - value)) for value in rgb]
            cube = tuple(levels[i] for i in indices)
            gray_index = min(range(24), key=lambda i: sum((8 + i * 10 - value) ** 2 for value in rgb))
            gray = 8 + gray_index * 10
            if sum((gray - value) ** 2 for value in rgb) < sum((a - b) ** 2 for a, b in zip(cube, rgb)):
                return 232 + gray_index
            return 16 + 36 * indices[0] + 6 * indices[1] + indices[2]
        palette = ((0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
                   (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229))
        return min(range(min(8, colors)), key=lambda i: sum((a - b) ** 2 for a, b in zip(palette[i], rgb)))

    def _attributes(self, style) -> int:
        if not self.curses.has_colors():
            return self.curses.A_BOLD if style and style.bold else 0
        foreground = self._color(style.color if style else None, (0, 0, 0))
        background = self._color(style.bgcolor if style else None, (255, 255, 255))
        pair = (foreground, background)
        if pair not in self.pairs and len(self.pairs) + 1 < min(self.curses.COLOR_PAIRS, 256):
            number = len(self.pairs) + 1
            self.curses.init_pair(number, *pair)
            self.pairs[pair] = number
        attributes = self.curses.color_pair(self.pairs.get(pair, 0))
        if style and style.bold:
            attributes |= self.curses.A_BOLD
        if style and style.underline:
            attributes |= self.curses.A_UNDERLINE
        return attributes

    def input(self) -> bool:
        c = self.curses
        while True:
            key = self.screen.getch()
            if key == -1:
                return True
            if key in (ord("q"), ord("Q")):
                return False
            if key in (c.KEY_UP, ord("k")):
                self.offset -= 1
            elif key in (c.KEY_DOWN, ord("j")):
                self.offset += 1
            elif key == c.KEY_PPAGE:
                self.offset -= self.page_size
            elif key == c.KEY_NPAGE:
                self.offset += self.page_size
            elif key in (c.KEY_HOME, ord("g")):
                self.offset = 0
            elif key in (c.KEY_END, ord("G")):
                self.offset = max(0, self.total_lines - self.page_size)
            elif key == c.KEY_MOUSE:
                try:
                    buttons = c.getmouse()[4]
                    if buttons & c.BUTTON4_PRESSED:
                        self.offset -= 3
                    elif buttons & getattr(c, "BUTTON5_PRESSED", 0):
                        self.offset += 3
                except c.error:
                    pass
            self.offset = max(0, min(self.offset, max(0, self.total_lines - self.page_size)))

    def _lines(self, renderable, width: int):
        options = self.console.options.update(width=width, height=None)
        return self.console.render_lines(renderable, options, pad=False)

    def _draw(self, row: int, segments, width: int, left: int = 0):
        column = 0
        for segment in segments:
            if segment.control:
                continue
            text = segment.text.replace("\n", "")
            if segment.cell_length > width - column:
                text = set_cell_size(text, width - column)
            try:
                self.screen.addstr(row, left + column, text, self._attributes(segment.style))
            except self.curses.error:
                # Writing the bottom-right cell can report ERR after succeeding.
                pass
            column += min(segment.cell_length, width - column)
            if column >= width:
                break

    def update(self, dashboard: Dashboard):
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        if height < 12 or width < 45:
            self._draw(0, self._lines(Text("Enlarge the score pane to view submissions."), width)[0], width)
            self.screen.refresh()
            return
        content_width = min(160, width - 4)
        left = (width - content_width) // 2
        fixed = self._lines(dashboard.fixed(content_width), content_width)
        records = self._lines(dashboard.records(), content_width)
        self.page_size = max(1, height - len(fixed) - 2)
        self.total_lines = len(records)
        self.offset = min(self.offset, max(0, len(records) - self.page_size))
        for row, segments in enumerate(fixed[:height - 2]):
            self._draw(row, segments, content_width, left)
        for row, segments in enumerate(records[self.offset:self.offset + self.page_size], start=len(fixed)):
            if row < height - 2:
                self._draw(row, segments, content_width, left)
        last = min(self.offset + self.page_size, len(dashboard.submissions))
        first = self.offset + 1 if dashboard.submissions else 0
        footer = Table.grid(expand=True, padding=(0, 1))
        footer.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        footer.add_column(justify="right", no_wrap=True)
        footer.add_row(Text(dashboard.message, style="#666666"), Text(f"{first}–{last} / {len(dashboard.submissions)}", style="#666666"))
        self._draw(height - 2, self._lines(footer, content_width)[0], content_width, left)
        keys = Text("↑/↓ scroll · PgUp/PgDn · Home/End", style="#999999", no_wrap=True, overflow="ellipsis")
        self._draw(height - 1, self._lines(keys, content_width)[0], content_width, left)
        self.screen.noutrefresh()
        self.curses.doupdate()
