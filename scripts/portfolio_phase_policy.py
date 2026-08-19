"""Shared Shanghai-time policy for official portfolio snapshots."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PREMARKET_END = time(8, 50)
MIDDAY_TARGET = time(11, 32)
MIDDAY_END = time(13, 0)
CLOSE_START = time(15, 0)
CLOSE_TARGET = time(15, 5)
CLOSE_END = time(18, 0)


class PhaseTimeError(ValueError):
    """Raised when a snapshot phase does not match the Shanghai market clock."""


@dataclass(frozen=True)
class PhaseWaitPlan:
    """Decision made from the real workflow start time."""

    phase: str
    current: datetime
    target: datetime | None
    cutoff: datetime
    wait_seconds: int


def as_shanghai_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def _is_intraday_session(now: datetime) -> bool:
    market_time = as_shanghai_time(now).timetz().replace(tzinfo=None)
    return (
        time(9, 30) <= market_time <= time(11, 30)
        or time(13, 0) <= market_time < time(15, 0)
    )


def validate_phase_time(phase: str, now: datetime) -> None:
    """Protect every official snapshot with its Shanghai report window."""
    market_time = as_shanghai_time(now).timetz().replace(tzinfo=None)
    if phase == "premarket" and not (time(0, 0) <= market_time <= PREMARKET_END):
        raise PhaseTimeError(
            "premarket snapshot requires Asia/Shanghai time between 00:00 and 08:50"
        )
    if phase == "midday" and not (time(11, 30) <= market_time < MIDDAY_END):
        raise PhaseTimeError(
            "midday snapshot requires Asia/Shanghai time from 11:30 until before 13:00"
        )
    if phase == "close" and not (CLOSE_START <= market_time < CLOSE_END):
        raise PhaseTimeError(
            "close snapshot requires Asia/Shanghai time from 15:00 until before 18:00"
        )
    if phase == "intraday" and not _is_intraday_session(now):
        raise PhaseTimeError(
            "intraday snapshot requires an A-share session: "
            "09:30-11:30 or 13:00-15:00 Asia/Shanghai"
        )


def plan_scheduled_phase(phase: str, now: datetime) -> PhaseWaitPlan:
    """Return the scheduled workflow wait without changing or faking the clock."""
    current = as_shanghai_time(now)
    current_time = current.timetz().replace(tzinfo=None)
    if phase == "premarket":
        cutoff = datetime.combine(current.date(), PREMARKET_END, SHANGHAI_TZ)
        if current_time > PREMARKET_END:
            raise PhaseTimeError(
                "premarket schedule missed the 08:50 Asia/Shanghai cutoff; "
                "current snapshot unavailable"
            )
        return PhaseWaitPlan(phase, current, None, cutoff, 0)
    if phase == "midday":
        target = datetime.combine(current.date(), MIDDAY_TARGET, SHANGHAI_TZ)
        cutoff = datetime.combine(current.date(), MIDDAY_END, SHANGHAI_TZ)
    elif phase == "close":
        target = datetime.combine(current.date(), CLOSE_TARGET, SHANGHAI_TZ)
        cutoff = datetime.combine(current.date(), CLOSE_END, SHANGHAI_TZ)
    else:
        raise ValueError(f"unsupported scheduled market phase: {phase}")

    if current >= cutoff:
        raise PhaseTimeError(
            f"{phase} schedule missed the {cutoff:%H:%M} Asia/Shanghai cutoff; "
            "current snapshot unavailable"
        )
    # Round up so sub-second workflow start times can never release before target.
    wait_seconds = max(0, math.ceil((target - current).total_seconds()))
    return PhaseWaitPlan(phase, current, target, cutoff, wait_seconds)
