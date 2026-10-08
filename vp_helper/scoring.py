from __future__ import annotations

from .models import Cell, Medals, Rules, Submission, TeamScore, VPError


_VERDICTS = {
    "OK": "CORRECT", "AC": "CORRECT", "ACCEPTED": "CORRECT",
    "WRONG_ANSWER": "WRONG-ANSWER", "WA": "WRONG-ANSWER", "WRONG ANSWER": "WRONG-ANSWER",
    "RUNTIME_ERROR": "RUN-ERROR", "RE": "RUN-ERROR", "RUNTIME ERROR": "RUN-ERROR",
    "MEMORY_LIMIT_EXCEEDED": "MEMORY-LIMIT", "MLE": "MEMORY-LIMIT", "ML": "MEMORY-LIMIT", "MEMORY LIMIT EXCEEDED": "MEMORY-LIMIT",
    "COMPILATION_ERROR": "COMPILER-ERROR", "CE": "COMPILER-ERROR", "COMPILE ERROR": "COMPILER-ERROR",
    "COMPILATION ERROR": "COMPILER-ERROR", "TIME_LIMIT_EXCEEDED": "TIMELIMIT", "TLE": "TIMELIMIT", "TL": "TIMELIMIT",
    "TIME LIMIT EXCEEDED": "TIMELIMIT", "OUTPUT_LIMIT_EXCEEDED": "OUTPUT-LIMIT", "OLE": "OUTPUT-LIMIT", "OL": "OUTPUT-LIMIT",
    "OUTPUT LIMIT EXCEEDED": "OUTPUT-LIMIT", "NO OUTPUT": "NO-OUTPUT", "NO_OUTPUT": "NO-OUTPUT",
    "TESTING": "PENDING", "SUBMITTED": "PENDING", "WAITING": "PENDING", "WAITING FOR JUDGING": "PENDING",
    "JUDGING": "PENDING", "RUNNING": "PENDING", "": "PENDING", "QUEUE": "PENDING",
    "TOO LATE": "TOO-LATE", "TOO_LATE": "TOO-LATE",
}
_KNOWN = set(_VERDICTS.values())


def verdict(value: str | None, score: float | None = None) -> str:
    if score == 100:
        return "CORRECT"
    value = (value or "").strip().upper().rstrip(" \t✓✔✗✘")
    if value in _KNOWN:
        return value
    if value in _VERDICTS:
        return _VERDICTS[value]
    for label in sorted(_VERDICTS, key=len, reverse=True):
        if len(label) > 3 and value.startswith(label + " ON TEST "):
            return _VERDICTS[label]
    if value.startswith(("JUDGING", "RUNNING", "TESTING", "WAITING")):
        return "PENDING"
    # Infrastructure failures and unfamiliar results must never become a scored WA.
    raise VPError(f"Unrecognized judging result: {value!r}")


def score_submissions(
    submissions: list[Submission], problems: list[str], rules: Rules,
    elapsed: float, *, public: bool = False, released: bool = False,
    team_id: str = "me", name: str = "me",
) -> TeamScore:
    cells = {p: Cell() for p in problems}
    frozen = public and rules.frozen(elapsed, released)
    # Latest polling response replaces previous verdicts; callers deduplicate by ID.
    unique = {s.id: s for s in submissions if s.team == team_id}
    for s in sorted(unique.values(), key=lambda s: (s.time, s.id.zfill(24))):
        if s.problem not in cells or not 0 <= s.time < rules.duration or s.time > elapsed:
            continue
        c = cells[s.problem]
        if c.solved:
            continue
        if frozen and s.time >= (rules.freeze_at or 0):
            c.pending += 1
        elif s.verdict == "PENDING":
            c.pending += 1
        elif s.verdict == "TOO-LATE":
            continue
        elif s.verdict == "COMPILER-ERROR" and not rules.compile_penalty:
            continue
        else:
            c.tries += 1
            if s.verdict == "CORRECT":
                c.solved = True
                c.solve_time = int(s.time // 60)
    return TeamScore(team_id, name, cells, penalty_minutes=rules.penalty_minutes)


def rank_of(me: TeamScore, original: list[TeamScore]) -> int:
    return 1 + sum(row.rank_key < me.rank_key for row in original if row.eligible and row.id != me.id)


def rank_label(me: TeamScore, original: list[TeamScore]) -> str:
    rank = str(rank_of(me, original))
    tied = any(row.eligible and row.id != me.id and row.rank_key == me.rank_key for row in original)
    return "=" + rank if tied else rank


def submission_history(submissions: list[Submission], problems: list[str], rules: Rules,
                       elapsed: float, *, team_id: str = "me") -> list[dict]:
    """All own submissions since this VP began, including later practice."""
    unique = {s.id: s for s in submissions
              if s.team == team_id and s.problem in problems and 0 <= s.time <= elapsed}
    ordered = sorted(unique.values(), key=lambda s: (s.time, s.id.zfill(24)), reverse=True)
    return [{"id": s.id, "problem": s.problem, "time": s.time, "verdict": s.verdict,
             "practice": s.time >= rules.duration} for s in ordered]


def recent_submissions(submissions: list[Submission], problems: list[str], rules: Rules,
                       elapsed: float, *, team_id: str = "me", limit: int = 5) -> list[dict]:
    """Latest submissions in this VP, independent of poll order or verdict changes."""
    unique = {s.id: s for s in submissions
              if s.team == team_id and s.problem in problems
              and 0 <= s.time < rules.duration and s.time <= elapsed}
    latest = sorted(unique.values(), key=lambda s: (s.time, s.id.zfill(24)), reverse=True)
    return [{"id": s.id, "problem": s.problem, "time": s.time, "verdict": s.verdict}
            for s in latest[:max(0, limit)]]


def prize_of(me: TeamScore, original: list[TeamScore], medals: Medals | None) -> str:
    if me.solved == 0 or not me.eligible:
        return "None"
    if medals is None:
        return "Unknown"
    rank = rank_of(me, original)
    if rank <= medals.gold:
        return "Gold"
    if rank <= medals.gold + medals.silver:
        return "Silver"
    if rank <= medals.gold + medals.silver + medals.bronze + medals.additional_bronze:
        return "Bronze"
    return "None"


def statistics(rows: list[TeamScore], problems: list[str]) -> dict[str, dict[str, int]]:
    result = {key: dict.fromkeys(problems, 0) for key in ("submitted", "attempted", "accepted")}
    for row in rows:
        if not row.eligible:
            continue
        for p in problems:
            c = row.cells.get(p, Cell())
            result["submitted"][p] += c.tries + c.pending
            result["attempted"][p] += int(c.tries + c.pending > 0)
            result["accepted"][p] += int(c.solved)
    return result
