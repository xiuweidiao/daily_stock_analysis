#!/usr/bin/env python3
"""Resolve a portfolio workflow trigger into one explicit business-date context."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_phase_policy import (
    CLOSE_END,
    CLOSE_TARGET,
    MIDDAY_END,
    PREMARKET_END,
    SHANGHAI_TZ,
    as_shanghai_time,
)
from scripts.portfolio_schedule_map import SCHEDULE_PHASES, resolve_phase
from src.core.trading_calendar import get_effective_trading_date, is_market_open


@dataclass(frozen=True)
class ScheduleContext:
    phase: str
    schedule: str | None
    expected_slot: str
    target_date: str
    expected_data_date: str
    target_is_trading_day: bool
    current_beijing_time: str
    lateness_minutes: int
    cutoff: str | None
    generation_mode: str
    can_generate: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _cron_weekdays(field: str) -> set[int]:
    """Convert the limited GitHub cron weekday syntax used here to Python weekdays."""
    values: set[int] = set()
    for part in field.split(","):
        if "-" in part:
            start, end = (int(value) for value in part.split("-", 1))
            cron_days = range(start, end + 1)
        else:
            cron_days = (int(part),)
        for cron_day in cron_days:
            # Cron: Sunday=0/7; Python: Monday=0, Sunday=6.
            values.add(6 if cron_day in {0, 7} else cron_day - 1)
    return values


def scheduled_slot(schedule: str, current: datetime) -> datetime:
    """Return the latest nominal UTC slot for this exact schedule at/before current."""
    if schedule not in SCHEDULE_PHASES:
        raise ValueError(f"unknown portfolio schedule: {schedule!r}")
    minute_field, hour_field, _, _, weekday_field = schedule.split()
    minute = int(minute_field)
    hour = int(hour_field)
    allowed_weekdays = _cron_weekdays(weekday_field)
    current_utc = current.astimezone(timezone.utc)
    for days_back in range(8):
        candidate_date = current_utc.date() - timedelta(days=days_back)
        if candidate_date.weekday() not in allowed_weekdays:
            continue
        candidate = datetime.combine(
            candidate_date, time(hour, minute), timezone.utc
        )
        if candidate <= current_utc:
            return candidate.astimezone(SHANGHAI_TZ)
    raise ValueError(f"could not resolve nominal slot for {schedule!r}")


def _expected_data_date(phase: str, target_date: date) -> date:
    if phase == "premarket":
        reference = datetime.combine(target_date, time(8, 0), SHANGHAI_TZ)
        return get_effective_trading_date("cn", current_time=reference)
    if phase == "midday":
        return target_date
    if phase == "close":
        reference = datetime.combine(target_date, CLOSE_TARGET, SHANGHAI_TZ)
        return get_effective_trading_date("cn", current_time=reference)
    return target_date


def build_schedule_context(
    *,
    phase: str,
    current: datetime,
    schedule: str | None = None,
) -> ScheduleContext:
    """Separate nominal task date, expected market date and actual start time."""
    current_cn = as_shanghai_time(current)
    if schedule:
        resolved_phase = resolve_phase(schedule)
        if resolved_phase != phase:
            raise ValueError(
                f"schedule {schedule!r} maps to {resolved_phase}, not {phase}"
            )
        slot = scheduled_slot(schedule, current_cn)
    else:
        slot = current_cn
    target_date = slot.date()
    target_is_trading_day = is_market_open("cn", target_date)
    expected_data_date = _expected_data_date(phase, target_date)
    lateness = max(0, int((current_cn - slot).total_seconds() // 60))

    cutoff_at: datetime | None = None
    generation_mode = "live"
    can_generate = True
    reason = "ready"
    if phase == "premarket":
        cutoff_at = datetime.combine(target_date, PREMARKET_END, SHANGHAI_TZ)
        if not target_is_trading_day:
            can_generate = False
            reason = "non_trading_day"
        elif current_cn.date() != target_date or current_cn > cutoff_at:
            can_generate = False
            reason = "SCHEDULE_MISSED_PHASE_WINDOW"
    elif phase == "midday":
        cutoff_at = datetime.combine(target_date, MIDDAY_END, SHANGHAI_TZ)
        if not target_is_trading_day:
            can_generate = False
            reason = "non_trading_day"
        elif current_cn.date() != target_date or current_cn >= cutoff_at:
            can_generate = False
            reason = "SCHEDULE_MISSED_PHASE_WINDOW"
    elif phase == "close":
        expected_close = datetime.combine(
            expected_data_date, CLOSE_TARGET, SHANGHAI_TZ
        )
        generation_mode = (
            "live"
            if current_cn.date() == expected_data_date
            and current_cn < datetime.combine(
                expected_data_date, CLOSE_END, SHANGHAI_TZ
            )
            else "recovery"
        )
        if current_cn < expected_close:
            reason = "waiting_for_market_target"
    elif phase == "intraday":
        expected_data_date = current_cn.date()
    else:
        raise ValueError(f"unsupported phase: {phase}")

    return ScheduleContext(
        phase=phase,
        schedule=schedule,
        expected_slot=slot.isoformat(),
        target_date=target_date.isoformat(),
        expected_data_date=expected_data_date.isoformat(),
        target_is_trading_day=target_is_trading_day,
        current_beijing_time=current_cn.isoformat(),
        lateness_minutes=lateness,
        cutoff=cutoff_at.isoformat() if cutoff_at else None,
        generation_mode=generation_mode,
        can_generate=can_generate,
        reason=reason,
    )


def _write_github_output(context: ScheduleContext) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    values = context.as_dict()
    with Path(output_path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = "" if value is None else str(value)
            output.write(f"{key}={rendered}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=("premarket", "midday", "close", "intraday"), required=True
    )
    parser.add_argument("--schedule")
    parser.add_argument("--now", help="ISO-8601 test/diagnostic clock")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    current = datetime.fromisoformat(args.now) if args.now else datetime.now(SHANGHAI_TZ)
    context = build_schedule_context(
        phase=args.phase, current=current, schedule=args.schedule or None
    )
    print(json.dumps(context.as_dict(), ensure_ascii=False, separators=(",", ":")))
    _write_github_output(context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
