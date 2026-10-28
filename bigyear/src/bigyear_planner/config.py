from dataclasses import asdict, dataclass, fields
from datetime import date
import json
import math
from pathlib import Path

STATES = {
    "US-" + s
    for s in "AL AZ AR CA CO CT DE FL GA ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
}


@dataclass(frozen=True)
class Config:
    year: int = 2024
    start_date: str | None = None
    end_date: str | None = None
    local_miles: float = 30
    mixed_road_birding: bool = False
    activity_minutes: float = 480
    drive_minutes: float = 480
    birding_minutes: float = 120
    walking_speed_kmh: float | None = None
    walking_threshold_m: float = 100
    start_location: str | None = None
    flights: bool = False
    target_locations: int = 1000
    rare_opportunities: int = 5
    short_window_days: int = 7
    grid_degrees: float = 2
    seconds: float = 600
    threads: int = 1
    seed: int = 2024
    alternatives: int = 5
    near_species: int = 2
    memory_limit: str = "2GB"

    def __post_init__(self):
        if not isinstance(self.mixed_road_birding, bool):
            raise ValueError('mixed_road_birding must be a boolean')
        if not isinstance(self.year, int) or not 1900 <= self.year <= 2100:
            raise ValueError("year must be between 1900 and 2100")
        if self.first_date.year != self.year or self.last_date.year != self.year:
            raise ValueError("experiment dates must fall in the configured year")
        if self.first_date > self.last_date:
            raise ValueError("experiment start must not follow its end")
        for name in (
            "local_miles",
            "activity_minutes",
            "drive_minutes",
            "birding_minutes",
            "seconds",
            "grid_degrees",
        ):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name in ("target_locations", "threads", "alternatives"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("near_species", "rare_opportunities", "short_window_days"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.birding_minutes > self.activity_minutes:
            raise ValueError("birding duration exceeds the activity budget")
        if (not math.isfinite(self.walking_threshold_m) or self.walking_threshold_m < 0):
            raise ValueError('walking threshold must be finite and nonnegative')
        if self.walking_speed_kmh is not None and (
                not math.isfinite(self.walking_speed_kmh) or self.walking_speed_kmh <= 0):
            raise ValueError('walking speed must be positive and finite, or null for legacy allowance')
        if self.activity_minutes > 1440 or self.drive_minutes > 1440:
            raise ValueError("daily budgets cannot exceed 24 hours")
        if self.mixed_road_birding and self.drive_minutes > self.activity_minutes:
            raise ValueError('mixed-day driving cannot exceed the total activity budget')

    @property
    def days(self):
        return (self.last_date - self.first_date).days + 1

    @property
    def first_date(self):
        return date.fromisoformat(self.start_date) if self.start_date else date(self.year, 1, 1)

    @property
    def last_date(self):
        return date.fromisoformat(self.end_date) if self.end_date else date(self.year, 12, 31)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def read(cls, path):
        raw = json.loads(Path(path).read_text()) if path else {}
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown settings: {sorted(unknown)}")
        return cls(**raw)
