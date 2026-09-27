"""Business-day calendars.

Wraps ``exchange_calendars`` sessions. Outside the range the library can evaluate
(e.g. XTKS before 1997) we fall back to Mon-Fri weekdays and record that the fallback was
used, because a weekday calendar cannot distinguish holidays from missing data.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

_RANGE_START = pd.Timestamp("1970-01-01")
_RANGE_END = pd.Timestamp("2030-12-31")


class BusinessCalendar:
    def __init__(self, name: str | None):
        self.name = name
        self.fallback_ranges: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        sessions = pd.DatetimeIndex([])
        lib_start = lib_end = None
        if name:
            import exchange_calendars as xc

            cls = xc.calendar_utils.get_calendar
            try:
                cal = cls(name, start=_RANGE_START)
            except ValueError:
                # Find the earliest start the library accepts for this calendar.
                cal = cls(name)
                probe = cal.first_session
                try:
                    cal = cls(name, start=_bound_start(name))
                except Exception:
                    cal = cls(name, start=probe)
            sessions = pd.DatetimeIndex(cal.sessions).tz_localize(None).normalize()
            lib_start, lib_end = sessions[0], sessions[-1]
        weekdays = pd.bdate_range(_RANGE_START, _RANGE_END)
        if lib_start is None:
            self.sessions = weekdays
            self.fallback_ranges.append((_RANGE_START, _RANGE_END))
        else:
            before = weekdays[weekdays < lib_start]
            after = weekdays[weekdays > lib_end]
            if len(before):
                self.fallback_ranges.append((before[0], before[-1]))
            if len(after):
                self.fallback_ranges.append((after[0], after[-1]))
            self.sessions = before.append(sessions).append(after)
        self._set = set(self.sessions.values.astype("datetime64[D]").tolist())
        self._arr = self.sessions.values.astype("datetime64[D]")

    def is_session(self, day) -> bool:
        return np.datetime64(pd.Timestamp(day).date(), "D").item() in self._set

    def is_fallback(self, day) -> bool:
        d = pd.Timestamp(day)
        return any(a <= d <= b for a, b in self.fallback_ranges)

    def sessions_in_range(self, start, end) -> pd.DatetimeIndex:
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        return self.sessions[(self.sessions >= s) & (self.sessions <= e)]

    def offset(self, day, n: int) -> pd.Timestamp:
        """n-th session after (n>0) or before (n<0) ``day``. n=0 → day if session else previous session."""
        d = np.datetime64(pd.Timestamp(day).date(), "D")
        if n >= 0:
            idx = np.searchsorted(self._arr, d, side="right" if n > 0 else "left")
            if n == 0:
                if idx < len(self._arr) and self._arr[idx] == d:
                    return pd.Timestamp(d)
                return pd.Timestamp(self._arr[idx - 1])
            return pd.Timestamp(self._arr[idx + n - 1])
        idx = np.searchsorted(self._arr, d, side="left")
        return pd.Timestamp(self._arr[idx + n])

    def previous_or_same(self, day) -> pd.Timestamp:
        return self.offset(day, 0)

    def next_or_same(self, day) -> pd.Timestamp:
        d = np.datetime64(pd.Timestamp(day).date(), "D")
        idx = np.searchsorted(self._arr, d, side="left")
        return pd.Timestamp(self._arr[idx])

    def last_session_of_month(self, year: int, month: int) -> pd.Timestamp:
        end = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)
        return self.previous_or_same(end)

    def sessions_between(self, a, b) -> int:
        """Number of sessions in (a, b]."""
        da = np.datetime64(pd.Timestamp(a).date(), "D")
        db = np.datetime64(pd.Timestamp(b).date(), "D")
        return int(np.searchsorted(self._arr, db, side="right") - np.searchsorted(self._arr, da, side="right"))


def _bound_start(name: str) -> pd.Timestamp:
    import exchange_calendars as xc

    cal_cls = xc.calendar_utils._default_calendar_factories[name]  # type: ignore[attr-defined]
    return pd.Timestamp(cal_cls.bound_min()) if hasattr(cal_cls, "bound_min") else pd.Timestamp("1990-01-01")


@lru_cache(maxsize=32)
def get_calendar(name: str | None) -> BusinessCalendar:
    return BusinessCalendar(name)
