from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import getpass
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from rich.console import Console
from rich.table import Table
from rich import box

from .models import Contest, Reference, Submission, VPError
from .parsing import contest_url, contest_id_url
from .scoring import rank_of, rank_label, prize_of, recent_submissions, score_submissions, statistics, submission_history
from .storage import config_directory, desktop, file_lock, read_history, read_json, state_directory, update_history, write_json
from .tui import display, TerminalDashboard

console = Console()


def adapter(url: str, config: dict):
    platform, _, canonical = contest_url(url)
    if platform == "qoj":
        from .qoj import QOJ
        return QOJ(canonical, config.get("qoj", {}))
    from .gym import Gym
    return Gym(canonical, config.get(platform, config.get("gym", {})))


def load_account(platform: str) -> dict:
    path = config_directory() / "config.json"
    config = read_json(path)
    key = "gym" if platform == "cf" and "cf" not in config else platform
    account = config.setdefault(key, {})
    required = ("handle", "cookie") if platform == "qoj" else ("handle", "cookie", "api_key", "api_secret")
    missing = [key for key in required if not account.get(key)]
    if missing and not sys.stdin.isatty():
        raise VPError(f"Missing {platform} account fields {', '.join(missing)} in {path}")
    if missing:
        console.print(f"First use: configure the existing {platform} login session.", markup=False)
        labels = {"handle": "Account handle", "cookie": "Cookie header from your Ubuntu browser", "api_key": "Codeforces API key", "api_secret": "Codeforces API secret"}
        for key in missing:
            value = input(labels[key] + ": ").strip() if key == "handle" else getpass.getpass(labels[key] + ": ").strip()
            if not value:
                raise VPError("An account field was empty; no native VP was started.")
            account[key] = value
        write_json(path, config)
    return config


def history() -> None:
    rows = read_history(desktop() / "vp_history.csv")
    table = Table(box=box.SIMPLE, header_style="bold")
    for key in ("start_time", "contest", "solved", "penalty", "rank", "prize"):
        table.add_column(key)
    for row in sorted(rows, key=lambda r: r["start_time"], reverse=True):
        table.add_row(*(row[key] or "—" for key in ("start_time", "contest", "solved", "penalty", "rank", "prize")))
    console.print(table)


def configure_submit(api, contest: Contest, directory: Path) -> None:
    script = api.account.get("submit_script") or shutil.which("submit")
    selected = state_directory() / "submit"
    bundled = bool(script and Path(script).is_symlink() and Path(os.readlink(script)) == selected)
    if not script or not Path(script).is_file() or not os.access(script, os.X_OK):
        raise VPError("The existing submit script is not installed; configure its path in ~/.config/vp/config.json.")
    if contest.platform == "qoj":
        ids = [int(value) for value in contest.problems.values()]
        if not bundled and ids != list(range(ids[0], ids[0] + len(ids))):
            raise VPError("This QOJ contest has nonconsecutive problem IDs. Use the installed submit script with problem_ids support.")
        write_json(directory / "contest.json", api.submit_configuration(contest))
    elif bundled:
        write_json(directory / "contest.json", {"name": contest.name, "platform": "gym",
                   "contest_id": int(contest.contest_id), "handle": api.handle,
                   "cookie": api.account["cookie"],
                   "user_agent": api.web.session.headers["User-Agent"]})
    else:
        # ehnryx/cf_submit reads a plain contestid beside cf.py, not JSON.
        location = Path(api.account.get("cf_submit_directory") or Path(script).resolve().parent)
        if (location / "cf.py").is_file() or (location / "cf_login.py").is_file():
            (location / "contestid").write_text(contest.contest_id, encoding="utf8")
        elif api.account.get("submit_config_format") == "contest.json":
            write_json(directory / "contest.json", {"name": contest.name, "platform": "gym",
                        "base_url": contest.url, "contest_id": int(contest.contest_id),
                        "problems": "".join(contest.problems), "cookie": api.account["cookie"],
                        "user_agent": api.web.session.headers["User-Agent"]})
        else:
            raise VPError("The Gym submit script's configuration format must be connected before a native VP is started.")
    if bundled:
        tool = Path("/opt/vp-helper/current/third_party") / ("qoj" if contest.platform == "qoj" else "cf_submit") / "submit"
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise VPError(f"The bundled {contest.platform} submit script is missing; reinstall vp.")
        temporary = selected.with_name("submit.new")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(tool)
        os.replace(temporary, selected)


def tmux_exists(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


def attach(name: str) -> None:
    command = ["tmux", "switch-client" if os.environ.get("TMUX") else "attach-session", "-t", name]
    subprocess.run(command, check=True)


def launch(state_path: Path, state: dict) -> None:
    name = state["tmux_session"]
    directory = state["directory"]
    monitor = shlex.join([sys.executable, "-m", "vp_helper.cli", "_monitor", str(state_path)])
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", directory, monitor], check=True)
    try:
        subprocess.run(["tmux", "set-option", "-t", name, "remain-on-exit", "on"], check=True)
        subprocess.run(["tmux", "set-option", "-t", name, "mouse", "on"], check=True)
        shell = os.environ.get("SHELL", "/bin/bash")
        subprocess.run(["tmux", "split-window", "-v", "-p", "30", "-t", name,
                        "-c", directory, "exec " + shlex.quote(shell)], check=True)
        subprocess.run(["tmux", "select-pane", "-t", name + ":0.1"], check=True)
    except subprocess.CalledProcessError:
        # Preserve the monitor/session if only the shell pane failed to start.
        raise VPError(f"VP monitoring started in tmux session {name}; the shell pane failed to start.") from None
    attach(name)


def prepare_start(url: str) -> tuple[Path, dict]:
    if sys.platform != "linux":
        raise VPError("vp is supported on Ubuntu only.")
    if not sys.stdin.isatty():
        raise VPError("Run vp in an interactive Ubuntu terminal.")
    if shutil.which("tmux") is None:
        raise VPError("tmux is missing; run the installer.")
    platform, _, canonical = contest_url(url)
    state_root = state_directory()
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(state_root / "start.lock"):
        current = read_json(state_root / "current.json")
        if current:
            old = read_json(Path(current["state_path"]))
            if old.get("url") == canonical and tmux_exists(old.get("tmux_session", "")):
                return Path(current["state_path"]), old
            if old.get("status") != "complete" and time.time() < old.get("start_time", 0) + old.get("contest", {}).get("rules", {}).get("duration", 0):
                raise VPError(f"A native VP is already running: {old.get('url')}. Open that URL to resume it.")
        config = load_account(platform)
        api = adapter(canonical, config)
        console.print("Reading contest information…", markup=False)
        contest = api.prepare()
        root = desktop()
        root.mkdir(parents=True, exist_ok=True)
        contest.directory_name = " ".join(contest.directory_name.split())
        directory = root / contest.directory_name
        directory.mkdir(exist_ok=True)
        configure_submit(api, contest, directory)
        for name, pdf in contest.pdfs.items():
            console.print("Downloading " + name, markup=False)
            api.web.pdf(pdf, directory / name)
        console.print("Starting and verifying native virtual participation…", markup=False)
        started = api.start(contest)
        name = "vp-" + platform + "-" + contest.contest_id + "-" + str(int(started))
        path = state_root / (name + ".json")
        saved = read_json(path)
        state = {"contest": contest.to_dict(), "url": canonical, "start_time": started,
                 "handle": api.handle,
                 "directory": str(directory), "tmux_session": name, "status": "running",
                 "submissions": saved.get("submissions", []), "recent": saved.get("recent", []),
                 "history_path": str(root / "vp_history.csv")}
        write_json(path, state)
        write_json(state_root / "current.json", {"state_path": str(path)})
        console.print("Prepared " + str(directory), markup=False)
        return path, state


def start(url: str) -> None:
    # Release the cross-shell preparation lock before entering an attached TUI.
    path, state = prepare_start(url)
    if tmux_exists(state["tmux_session"]):
        pane = state["tmux_session"] + ":0.0"
        dead = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_dead}"],
                              check=True, capture_output=True, text=True).stdout.strip()
        if dead == "1":
            command = shlex.join([sys.executable, "-m", "vp_helper.cli", "_monitor", str(path)])
            subprocess.run(["tmux", "respawn-pane", "-t", pane, "-c", state["directory"], command], check=True)
        subprocess.run(["tmux", "set-option", "-t", state["tmux_session"], "mouse", "on"], check=True)
        attach(state["tmux_session"])
    else:
        launch(path, state)


def poll(api, contest: Contest, elapsed: float, refresh_reference: bool):
    """Keep slow requests outside the terminal's input and redraw loop."""
    incoming = reference = None
    error = ""
    try:
        incoming = api.submissions(contest)
        if refresh_reference:
            reference = api.reference(contest, elapsed)
    except VPError as exc:
        error = str(exc)
    return incoming, reference, error


def monitor(path: Path) -> None:
    # tmux's default "screen" terminfo only exposes eight colors. Its terminal
    # supports the 256-color palette needed for orange and purple practice rows.
    if os.environ.get("TMUX") and os.environ.get("TERM") in ("screen", "tmux"):
        os.environ["TERM"] = "screen-256color"
    with file_lock(path.with_suffix(".monitor.lock"), nonblocking=True):
        state = read_json(path)
        contest = Contest.from_dict(state["contest"])
        config = read_json(config_directory() / "config.json")
        api = adapter(contest.url, config)
        if state.get("handle") and api.handle != state["handle"]:
            raise VPError("The configured account changed; restore this VP's account before resuming it.")
        api.start_time = state["start_time"]
        submissions = {x["id"]: Submission(**x) for x in state.get("submissions", [])}
        reference = Reference()
        last_poll = last_reference = 0.0
        next_poll = 0.0
        backoff = 5.0
        message = "Connecting…"
        have_polled = False
        future = None
        next_render = 0.0
        with ThreadPoolExecutor(max_workers=1) as network, TerminalDashboard() as view:
            while view.input():
                elapsed = time.time() - state["start_time"]
                now = time.monotonic()
                fresh_submissions = False
                if future is not None and future.done():
                    incoming, refreshed, error = future.result()
                    future = None
                    if incoming is not None:
                        fresh_submissions = True
                        have_polled = True
                        for submission in incoming:
                            submissions[submission.id] = submission
                        last_poll = now
                    if refreshed is not None:
                        reference = refreshed
                        last_reference = now
                    message = error or reference.message
                    backoff = min(60.0, backoff * 2) if error else 5.0
                    next_poll = now + backoff
                if future is None and now >= next_poll:
                    refresh_reference = (now - last_reference >= 30
                                         or elapsed >= contest.rules.duration and not reference.complete)
                    future = network.submit(poll, api, contest, elapsed, refresh_reference)
                frozen = contest.rules.frozen(elapsed, reference.released)
                # Rebuild from canonical submissions, including when resuming old
                # state or offline. Discovery order is not submission chronology.
                recent = recent_submissions(list(submissions.values()), list(contest.problems), contest.rules, elapsed)
                records = submission_history(list(submissions.values()), list(contest.problems), contest.rules, elapsed)
                private = score_submissions(list(submissions.values()), list(contest.problems), contest.rules,
                                            elapsed, name=api.handle)
                public = score_submissions(list(submissions.values()), list(contest.problems), contest.rules,
                                           elapsed, public=True, released=reference.released, name=api.handle)
                all_final = all(s.verdict != "PENDING" for s in submissions.values() if 0 <= s.time < contest.rules.duration)
                finalized = (elapsed >= contest.rules.duration and not frozen and reference.complete
                             and api.submissions_complete and have_polled and all_final)
                rank = "?" if frozen else str(rank_of(private, reference.rows)) if reference.complete else "—"
                shown_rank = rank_label(private, reference.rows) if reference.complete and not frozen else rank
                prize = prize_of(private, reference.rows, contest.medals) if finalized else ""
                stats = statistics(reference.rows + [public], list(contest.problems)) if reference.statistics_complete else None
                shown_message = message
                if finalized and not shown_message:
                    shown_message = "Finished"
                elif frozen and not shown_message:
                    shown_message = "Scoreboard frozen"
                if last_poll and now - last_poll > 15:
                    shown_message = "Results stale · " + shown_message
                if now >= next_render:
                    view.update(display(contest, elapsed, private, shown_rank, prize, stats, records, shown_message))
                    next_render = now + 0.25
                if fresh_submissions:
                    state.update(contest=contest.to_dict(), submissions=[asdict(x) for x in submissions.values()],
                                 recent=recent, submission_history=records, last_sync_time=time.time(),
                                 status="complete" if finalized else "running")
                    write_json(path, state)
                    row = {"start_time": datetime.fromtimestamp(state["start_time"], timezone.utc).isoformat(),
                           "contest": contest.name, "url": contest.url, "solved": private.solved,
                           "penalty": private.penalty, "rank": rank if finalized else "", "prize": prize}
                    update_history(Path(state["history_path"]), row)
                time.sleep(0.1)


def main() -> None:
    args = sys.argv[1:]
    try:
        if args == ["history"]:
            history()
        elif len(args) == 2 and args[0] == "_monitor":
            monitor(Path(args[1]))
        elif len(args) == 2 and args[0] in ("cf", "qoj", "gym"):
            start(contest_id_url(*args))
        elif len(args) == 1 and args[0] not in ("-h", "--help"):
            start(args[0])
        elif args in ([], ["-h"], ["--help"]):
            console.print("Usage: vp cf ID\n       vp qoj ID\n       vp gym ID\n       vp CONTEST_URL\n       vp history", markup=False)
        else:
            raise VPError("Usage: vp cf ID, vp qoj ID, vp gym ID or vp history")
    except (VPError, OSError, subprocess.CalledProcessError) as exc:
        console.print("vp: " + str(exc), style="red", markup=False)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
