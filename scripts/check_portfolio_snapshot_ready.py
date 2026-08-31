#!/usr/bin/env python3
"""Check whether a committed portfolio snapshot is ready for consumption."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_config import (
    DEFAULT_CONFIG_PATH,
    PortfolioConfig,
    PortfolioConfigError,
    load_portfolio_config,
)
from scripts.portfolio_market_data import OFFICIAL_OUTPUT_DIR, REPORT_PHASES
from scripts.portfolio_phase_policy import SHANGHAI_TZ, as_shanghai_time
from scripts.portfolio_schedule_context import build_schedule_context
from scripts.portfolio_snapshot_readiness import inspect_snapshot


@dataclass(frozen=True)
class SnapshotReadiness:
    phase: str
    ready: bool
    reason: str
    generated_at: str | None
    data_date: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _not_ready(
    phase: str,
    reason: str,
    *,
    payload: Mapping[str, Any] | None = None,
) -> SnapshotReadiness:
    return SnapshotReadiness(
        phase=phase,
        ready=False,
        reason=reason,
        generated_at=(
            payload.get("generated_at")
            if payload and isinstance(payload.get("generated_at"), str)
            else None
        ),
        data_date=(
            payload.get("data_date")
            if payload and isinstance(payload.get("data_date"), str)
            else None
        ),
    )


def check_snapshot_ready(
    path: Path,
    *,
    phase: str,
    portfolio: PortfolioConfig,
    now: datetime | None = None,
) -> SnapshotReadiness:
    """Return a read-only readiness decision for one committed snapshot file."""
    if phase not in REPORT_PHASES:
        raise ValueError(f"unsupported report phase: {phase}")
    current = as_shanghai_time(now or datetime.now(SHANGHAI_TZ))
    context = build_schedule_context(phase=phase, current=current)
    result = inspect_snapshot(
        path,
        phase=phase,
        portfolio=portfolio,
        target_date=date.fromisoformat(context.target_date),
        expected_data_date=date.fromisoformat(context.expected_data_date),
        generation_mode=context.generation_mode,
        now=current,
        target_is_trading_day=context.target_is_trading_day,
    )
    reason = "ok" if result.fresh else result.reason
    return SnapshotReadiness(
        phase=phase,
        ready=result.fresh,
        reason=reason,
        generated_at=result.generated_at,
        data_date=result.data_date,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=REPORT_PHASES, required=True)
    parser.add_argument("--path", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None, *, now: datetime | None = None
) -> int:
    args = parse_args(argv)
    path = args.path or OFFICIAL_OUTPUT_DIR / f"{args.phase}.json"
    try:
        portfolio = load_portfolio_config(args.config)
    except PortfolioConfigError:
        result = _not_ready(args.phase, "invalid_snapshot")
    else:
        result = check_snapshot_ready(
            path, phase=args.phase, portfolio=portfolio, now=now
        )
    print(json.dumps(result.as_dict(), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
