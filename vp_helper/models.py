from __future__ import annotations

from dataclasses import asdict, dataclass, field


class VPError(RuntimeError):
    """An actionable platform, configuration or data error."""


@dataclass
class Rules:
    duration: int
    penalty_minutes: int = 20
    compile_penalty: bool = False
    freeze_at: int | None = None
    unfreeze_at: int | None = None

    def __post_init__(self):
        if self.duration <= 0 or self.penalty_minutes < 0:
            raise VPError("Invalid contest duration or penalty configuration.")
        if self.freeze_at is not None and not 0 <= self.freeze_at <= self.duration:
            raise VPError("The scoreboard freeze time is outside the contest window.")
        if self.unfreeze_at is not None and self.unfreeze_at < self.duration:
            raise VPError("The original scoreboard unfreeze time precedes contest end.")

    def frozen(self, elapsed: float, released: bool = False) -> bool:
        return (
            self.freeze_at is not None and elapsed >= self.freeze_at
            and not released
            and (self.unfreeze_at is None or elapsed < self.unfreeze_at)
        )


@dataclass
class Medals:
    gold: int
    silver: int
    bronze: int
    source: str
    additional_bronze: int = 0


@dataclass
class Contest:
    platform: str
    contest_id: str
    url: str
    name: str
    directory_name: str
    problems: dict[str, str]
    rules: Rules
    pdfs: dict[str, str] = field(default_factory=dict)
    medals: Medals | None = None
    original_start: float | None = None
    reference_url: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> Contest:
        data = dict(value)
        data["rules"] = Rules(**data["rules"])
        if data.get("medals"):
            data["medals"] = Medals(**data["medals"])
        return cls(**data)


@dataclass
class Submission:
    id: str
    team: str
    problem: str
    time: float
    verdict: str


@dataclass
class Cell:
    tries: int = 0
    pending: int = 0
    solved: bool = False
    solve_time: int | None = None


@dataclass
class TeamScore:
    id: str
    name: str
    cells: dict[str, Cell]
    eligible: bool = True
    penalty_minutes: int = 20
    known_penalty: int | None = None

    @property
    def solved(self) -> int:
        return sum(c.solved for c in self.cells.values())

    @property
    def penalty(self) -> int:
        if self.known_penalty is not None:
            return self.known_penalty
        return sum(
            (c.solve_time or 0) + self.penalty_minutes * max(0, c.tries - 1)
            for c in self.cells.values() if c.solved
        )

    @property
    def last_correct(self) -> int:
        return max((c.solve_time or 0 for c in self.cells.values() if c.solved), default=0)

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return -self.solved, self.penalty, self.last_correct

    @property
    def rank_key(self) -> tuple[int, int]:
        # Last acceptance orders equal scores without splitting their shared rank.
        return -self.solved, self.penalty


@dataclass
class Reference:
    rows: list[TeamScore] = field(default_factory=list)
    complete: bool = False
    statistics_complete: bool = False
    released: bool = False
    message: str = ""
    replay: object | None = None
