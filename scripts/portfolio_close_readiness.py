#!/usr/bin/env python3
"""Decide whether today's valid close snapshot already exists."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_config import DEFAULT_CONFIG_PATH, PortfolioConfig, load_portfolio_config
from scripts.portfolio_phase_policy import SHANGHAI_TZ
from scripts.portfolio_schedule_context import build_schedule_context
from scripts.portfolio_snapshot_readiness import inspect_snapshot


LOGGER = logging.getLogger("portfolio_close_readiness")
DEFAULT_CLOSE_PATH = REPOSITORY_ROOT / "data" / "portfolio" / "close.json"

TODAY_CLOSE_ALREADY_READY = "TODAY_CLOSE_ALREADY_READY"
TODAY_CLOSE_MISSING = "TODAY_CLOSE_MISSING"
TODAY_CLOSE_INVALID = "TODAY_CLOSE_INVALID"
NON_TRADING_DAY_SKIP = "NON_TRADING_DAY_SKIP"  # backwards-compatible label


@dataclass(frozen=True)
class CloseReadiness:
    status: str
    should_generate: bool
    reason: str


def inspect_close_snapshot(
    path: Path,
    *,
    portfolio: PortfolioConfig,
    now: datetime | None = None,
) -> CloseReadiness:
    current = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    context = build_schedule_context(phase="close", current=current)
    result = inspect_snapshot(
        path,
        phase="close",
        portfolio=portfolio,
        target_date=date.fromisoformat(context.target_date),
        expected_data_date=date.fromisoformat(context.expected_data_date),
        generation_mode=context.generation_mode,
        now=current,
        target_is_trading_day=context.target_is_trading_day,
    )
    if result.fresh:
        return CloseReadiness(
            TODAY_CLOSE_ALREADY_READY,
            False,
            "latest completed close snapshot passed the formal contract",
        )
    status = (
        TODAY_CLOSE_MISSING
        if result.reason in {"missing_snapshot", "stale_snapshot"}
        else TODAY_CLOSE_INVALID
    )
    return CloseReadiness(status, result.should_generate, result.reason)


def _write_github_output(result: CloseReadiness) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"status={result.status}\n")
        output.write(f"should_generate={'true' if result.should_generate else 'false'}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_CLOSE_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        portfolio = load_portfolio_config(args.config)
        result = inspect_close_snapshot(args.path, portfolio=portfolio)
    except Exception as exc:
        result = CloseReadiness(TODAY_CLOSE_INVALID, True, str(exc))
    print(result.status)
    LOGGER.info("%s", result.reason)
    _write_github_output(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
