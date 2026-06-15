"""Decide the lookback window + digest mode based on the day of week.

- Tue–Fri  -> "daily": look back at the previous day.
- Monday   -> "weekly": look back across the weekend + the prior week, and
              additionally assess progress (done vs. follow-up) against her PRs.
- Sat/Sun  -> "daily" on the previous day (rarely run, but defined for safety).
"""

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class Window:
    mode: str          # "daily" or "weekly"
    after: date        # Slack `after:` is exclusive, so this is the day BEFORE the range start
    before: date | None  # Slack `before:` is exclusive; None = up to now
    label: str         # human description for the digest header


def compute_window(today: date | None = None) -> Window:
    today = today or date.today()
    weekday = today.weekday()  # Mon=0 ... Sun=6

    if weekday == 0:  # Monday -> cover the prior week incl. weekend
        # Range we want: last Monday 00:00 through Sunday 23:59 (the full prior week).
        last_monday = today - timedelta(days=7)
        # after: is exclusive, so subtract one more day to include last Monday itself.
        after = last_monday - timedelta(days=1)
        before = today  # up to (but not including) today
        label = f"{last_monday.isoformat()} → {(today - timedelta(days=1)).isoformat()} (last week)"
        return Window(mode="weekly", after=after, before=before, label=label)

    # Otherwise: previous calendar day.
    prev = today - timedelta(days=1)
    after = prev - timedelta(days=1)  # exclusive lower bound
    before = today                     # exclusive upper bound -> just `prev`
    return Window(mode="daily", after=after, before=before, label=prev.isoformat())
