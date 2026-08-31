#!/usr/bin/env python3
"""Decide whether a scheduled close watchdog must recover its target session."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_config import (
    DEFAULT_CONFIG_PATH,
    PortfolioConfig,
    load_portfolio_config,
)
from scripts.portfolio_market_data import OFFICIAL_OUTPUT_DIR
from scripts.portfolio_phase_policy import SHANGHAI_TZ, as_shanghai_time
from scripts.portfolio_schedule_context import build_schedule_context
from scripts.portfolio_snapshot_readiness import inspect_snapshot


CLOSE_ALREADY_FRESH = "CLOSE_ALREADY_FRESH"
CLOSE_RECOVERY_REQUIRED = "CLOSE_RECOVERY_REQUIRED"
NON_TRADING_DAY = "NON_TRADING_DAY"
CLOSE_RECOVERY_NOT_READY = "CLOSE_RECOVERY_NOT_READY"


@dataclass(frozen=True)
class CloseWatchdogDecision:
    target_date: str
    current_beijing_time: str
    target_is_trading_day: bool
    fresh: bool
    should_generate: bool
    readiness: str
    reason: str
    result: str
    generated_at: str | None
    data_date: str | None
    market_phase: str | None
    portfolio_status: str | None
    generation_mode: str = "recovery"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _snapshot_metadata(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def inspect_close_watchdog(
    path: Path,
    *,
    portfolio: PortfolioConfig,
    now: datetime,
    schedule: str | None = None,
) -> CloseWatchdogDecision:
    """Return one read-only, nominal-slot-aware close recovery decision."""
    current = as_shanghai_time(now)
    context = build_schedule_context(
        phase="close", current=current, schedule=schedule
    )
    target_date = date.fromisoformat(context.target_date)
    metadata = _snapshot_metadata(path)
    common = {
        "target_date": target_date.isoformat(),
        "current_beijing_time": current.isoformat(),
        "generated_at": (
            metadata.get("generated_at")
            if isinstance(metadata.get("generated_at"), str)
            else None
        ),
        "data_date": (
            metadata.get("data_date")
            if isinstance(metadata.get("data_date"), str)
            else None
        ),
        "market_phase": (
            metadata.get("market_phase")
            if isinstance(metadata.get("market_phase"), str)
            else None
        ),
        "portfolio_status": (
            metadata.get("portfolio_status")
            if isinstance(metadata.get("portfolio_status"), str)
            else None
        ),
    }

    if not context.target_is_trading_day:
        return CloseWatchdogDecision(
            target_is_trading_day=False,
            fresh=False,
            should_generate=False,
            readiness="non_trading_day",
            reason="nominal watchdog date is not an A-share trading day",
            result=NON_TRADING_DAY,
            **common,
        )

    target_close = datetime.combine(target_date, time(15, 0), SHANGHAI_TZ)
    if current < target_close:
        return CloseWatchdogDecision(
            target_is_trading_day=True,
            fresh=False,
            should_generate=False,
            readiness="not_ready",
            reason="target A-share session has not closed",
            result=CLOSE_RECOVERY_NOT_READY,
            **common,
        )

    readiness = inspect_snapshot(
        path,
        phase="close",
        portfolio=portfolio,
        target_date=target_date,
        expected_data_date=target_date,
        generation_mode="recovery",
        now=current,
        target_is_trading_day=True,
    )
    readiness_label = (
        "missing" if readiness.reason == "missing_snapshot" else readiness.freshness
    )
    return CloseWatchdogDecision(
        target_is_trading_day=True,
        fresh=readiness.fresh,
        should_generate=readiness.should_generate,
        readiness=readiness_label,
        reason=readiness.reason,
        result=(
            CLOSE_ALREADY_FRESH
            if readiness.fresh
            else CLOSE_RECOVERY_REQUIRED
        ),
        generated_at=readiness.generated_at,
        data_date=readiness.data_date,
        market_phase=readiness.market_phase,
        portfolio_status=readiness.portfolio_status,
        target_date=target_date.isoformat(),
        current_beijing_time=current.isoformat(),
    )


def _write_github_output(decision: CloseWatchdogDecision) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        for key, value in decision.as_dict().items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = "" if value is None else str(value)
            output.write(f"{key}={rendered}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path", type=Path, default=OFFICIAL_OUTPUT_DIR / "close.json"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--schedule")
    parser.add_argument("--now", help="ISO-8601 test/diagnostic clock")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    current = (
        datetime.fromisoformat(args.now)
        if args.now
        else datetime.now(SHANGHAI_TZ)
    )
    portfolio = load_portfolio_config(args.config)
    decision = inspect_close_watchdog(
        args.path,
        portfolio=portfolio,
        now=current,
        schedule=args.schedule or None,
    )
    print(
        json.dumps(
            decision.as_dict(), ensure_ascii=False, separators=(",", ":")
        )
    )
    _write_github_output(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
