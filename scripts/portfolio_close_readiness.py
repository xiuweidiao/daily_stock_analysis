#!/usr/bin/env python3
"""Decide whether today's valid close snapshot already exists."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_config import DEFAULT_CONFIG_PATH, PortfolioConfig, load_portfolio_config
from scripts.portfolio_phase_policy import SHANGHAI_TZ
from scripts.validate_portfolio_snapshot import (
    SnapshotContractError,
    validate_snapshot_contract,
)
from src.core.trading_calendar import is_market_open


LOGGER = logging.getLogger("portfolio_close_readiness")
DEFAULT_CLOSE_PATH = REPOSITORY_ROOT / "data" / "portfolio" / "close.json"

TODAY_CLOSE_ALREADY_READY = "TODAY_CLOSE_ALREADY_READY"
TODAY_CLOSE_MISSING = "TODAY_CLOSE_MISSING"
TODAY_CLOSE_INVALID = "TODAY_CLOSE_INVALID"
NON_TRADING_DAY_SKIP = "NON_TRADING_DAY_SKIP"


@dataclass(frozen=True)
class CloseReadiness:
    status: str
    should_generate: bool
    reason: str


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _generated_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        generated_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if generated_at.tzinfo is None:
        return None
    return generated_at.astimezone(SHANGHAI_TZ).date()


def inspect_close_snapshot(
    path: Path,
    *,
    portfolio: PortfolioConfig,
    now: datetime | None = None,
) -> CloseReadiness:
    current = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    if not is_market_open("cn", current.date()):
        return CloseReadiness(
            NON_TRADING_DAY_SKIP,
            False,
            f"{current.date()} is not an A-share trading day",
        )
    if not path.exists():
        return CloseReadiness(TODAY_CLOSE_MISSING, True, f"{path} does not exist")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CloseReadiness(TODAY_CLOSE_INVALID, True, str(exc))
    if not isinstance(payload, Mapping):
        return CloseReadiness(TODAY_CLOSE_INVALID, True, "snapshot root is not an object")
    if payload.get("market_phase") != "close":
        return CloseReadiness(TODAY_CLOSE_INVALID, True, "market_phase is not close")

    data_date = _iso_date(payload.get("data_date"))
    generated_date = _generated_date(payload.get("generated_at"))
    if data_date is not None and data_date < current.date():
        return CloseReadiness(TODAY_CLOSE_MISSING, True, "data_date is older than today")
    if generated_date is not None and generated_date < current.date():
        return CloseReadiness(TODAY_CLOSE_MISSING, True, "generated_at is older than today")
    try:
        validate_snapshot_contract(
            payload,
            phase="close",
            portfolio=portfolio,
            now=current,
            max_generation_age=None,
        )
    except SnapshotContractError as exc:
        return CloseReadiness(TODAY_CLOSE_INVALID, True, str(exc))
    return CloseReadiness(
        TODAY_CLOSE_ALREADY_READY,
        False,
        "today's close snapshot passed the formal contract",
    )


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
