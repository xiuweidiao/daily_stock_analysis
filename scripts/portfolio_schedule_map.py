#!/usr/bin/env python3
"""Single dependency-free mapping from Portfolio Market Data triggers to phases."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence


PREMARKET_CRONS = (
    "37 22 * * 0-4",
    "07 23 * * 0-4",
    "37 23 * * 0-4",
)
MIDDAY_CRONS = (
    "53 2 * * 1-5",
    "07 3 * * 1-5",
    "21 3 * * 1-5",
)
CLOSE_CRONS = (
    "23 6 * * 1-5",
    "43 7 * * 1-5",
    "43 8 * * 1-5",
    "3 9 * * 1-5",
)
SCHEDULE_PHASES = {
    **{cron: "premarket" for cron in PREMARKET_CRONS},
    **{cron: "midday" for cron in MIDDAY_CRONS},
    **{cron: "close" for cron in CLOSE_CRONS},
}


def resolve_phase(schedule: str | None, manual_phase: str | None = None) -> str:
    if manual_phase:
        if manual_phase not in {"premarket", "midday", "close", "intraday"}:
            raise ValueError(f"unsupported manual phase: {manual_phase}")
        return manual_phase
    try:
        return SCHEDULE_PHASES[schedule or ""]
    except KeyError as exc:
        raise ValueError(f"unknown portfolio schedule: {schedule!r}") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule")
    parser.add_argument("--manual-phase")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    phase = resolve_phase(args.schedule or None, args.manual_phase or None)
    print(phase)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"phase={phase}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
