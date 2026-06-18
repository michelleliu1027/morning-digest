"""Decide the lookback window + digest mode based on the day of week.

- Tue–Fri  -> "daily": look back at the previous day.
- Monday   -> "weekly": look back across the weekend + the prior week, and
              additionally assess progress (done vs. follow-up) against your PRs.
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


def _weekly_window(today: date) -> Window:
    """The 7 days ending yesterday (covers the weekend + the prior week)."""
    start = today - timedelta(days=7)
    # after: is exclusive, so subtract one more day to include `start` itself.
    after = start - timedelta(days=1)
    before = today  # up to (but not including) today
    label = f"{start.isoformat()} → {(today - timedelta(days=1)).isoformat()} (last week)"
    return Window(mode="weekly", after=after, before=before, label=label)


def _daily_window(today: date) -> Window:
    prev = today - timedelta(days=1)
    after = prev - timedelta(days=1)  # exclusive lower bound -> include `prev`
    before = None                      # no upper bound -> up to NOW, so messages
                                       # posted today (before you logged in) are
                                       # caught too, not just yesterday's.
    label = f"{prev.isoformat()} → {today.isoformat()}"
    return Window(mode="daily", after=after, before=before, label=label)


def compute_window(today: date | None = None, force_mode: str | None = None) -> Window:
    """Pick the lookback window. Auto-selects weekly on Monday, daily otherwise;
    pass force_mode='daily'|'weekly' to override (and get the matching window)."""
    today = today or date.today()
    mode = force_mode or ("weekly" if today.weekday() == 0 else "daily")
    return _weekly_window(today) if mode == "weekly" else _daily_window(today)
