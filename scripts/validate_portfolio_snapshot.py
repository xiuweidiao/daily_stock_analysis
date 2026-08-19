#!/usr/bin/env python3
"""Validate a newly generated portfolio snapshot before it can be committed."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_config import DEFAULT_CONFIG_PATH, PortfolioConfig, load_portfolio_config
from scripts.portfolio_market_data import PHASES, TIMEZONE_NAME, _phase_data_date
from scripts.portfolio_phase_policy import SHANGHAI_TZ, PhaseTimeError, validate_phase_time
from src.core.trading_calendar import is_market_open


LOGGER = logging.getLogger("validate_portfolio_snapshot")
MAX_GENERATION_AGE = timedelta(minutes=30)


class SnapshotContractError(ValueError):
    """Raised when a formal snapshot is stale, mislabeled or has the wrong universe."""


CORE_QUOTE_FIELDS = (
    "latest_price",
    "prev_close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
)


def _finite_numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_generated_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise SnapshotContractError("generated_at must be an ISO-8601 string")
    try:
        generated_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SnapshotContractError("generated_at is not valid ISO-8601") from exc
    if generated_at.tzinfo is None:
        raise SnapshotContractError("generated_at must include a timezone offset")
    if generated_at.utcoffset() != timedelta(hours=8):
        raise SnapshotContractError("generated_at must use the Asia/Shanghai UTC+08:00 offset")
    return generated_at.astimezone(SHANGHAI_TZ)


def validate_snapshot_contract(
    payload: Mapping[str, Any],
    *,
    phase: str,
    portfolio: PortfolioConfig,
    now: datetime | None = None,
    max_generation_age: timedelta | None = MAX_GENERATION_AGE,
) -> None:
    """Validate phase, clock, trading date and configured security classification."""
    if phase not in PHASES:
        raise SnapshotContractError(f"unsupported market phase: {phase}")
    current = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    if payload.get("timezone") != TIMEZONE_NAME:
        raise SnapshotContractError(f"timezone must be {TIMEZONE_NAME}")
    if payload.get("market_phase") != phase:
        raise SnapshotContractError(f"market_phase must be {phase}")

    generated_at = _parse_generated_at(payload.get("generated_at"))
    if generated_at.date() != current.date():
        raise SnapshotContractError("current snapshot unavailable: generated_at is not today")
    generation_age = current - generated_at
    if generation_age < -timedelta(minutes=1):
        raise SnapshotContractError("generated_at cannot be in the future")
    if max_generation_age is not None and generation_age > max_generation_age:
        raise SnapshotContractError(
            "current snapshot unavailable: generated_at is not from this workflow execution"
        )
    try:
        validate_phase_time(phase, generated_at)
    except PhaseTimeError as exc:
        raise SnapshotContractError(str(exc)) from exc
    if not is_market_open("cn", generated_at.date()):
        raise SnapshotContractError("current snapshot unavailable: generated_at is not a trading day")

    expected_data_date = _phase_data_date(phase, generated_at).isoformat()
    if payload.get("data_date") != expected_data_date:
        raise SnapshotContractError(
            f"data_date must be {expected_data_date} for {phase}, got {payload.get('data_date')!r}"
        )

    expected_holdings = list(portfolio.holdings)
    expected_watchlist = list(portfolio.watchlist)
    if payload.get("holdings_codes") != expected_holdings:
        raise SnapshotContractError("holdings_codes does not match config/portfolio.json")
    if payload.get("watchlist_codes") != expected_watchlist:
        raise SnapshotContractError("watchlist_codes does not match config/portfolio.json")

    stocks = payload.get("stocks")
    benchmarks = payload.get("benchmarks")
    errors = payload.get("errors")
    if not isinstance(stocks, list):
        raise SnapshotContractError("stocks must be an array")
    if not isinstance(benchmarks, list):
        raise SnapshotContractError("benchmarks must be an array")
    if not isinstance(errors, list):
        raise SnapshotContractError("errors must be an array")
    if errors:
        raise SnapshotContractError("snapshot contains unresolved data errors")

    expected_tracking = list(portfolio.tracked_securities())
    actual_tracking = []
    for stock in stocks:
        if not isinstance(stock, Mapping):
            raise SnapshotContractError("each stocks item must be an object")
        actual_tracking.append((stock.get("code"), stock.get("tracking_type")))
    if actual_tracking != expected_tracking:
        raise SnapshotContractError("stocks code/tracking_type does not match portfolio config")
    for stock in stocks:
        if stock.get("status") not in {"ok", "partial"}:
            raise SnapshotContractError(
                f"stock {stock.get('code')!r} is not usable"
            )
        numeric_fields = {}
        for field in CORE_QUOTE_FIELDS:
            number = _finite_numeric(stock.get(field))
            if number is None:
                raise SnapshotContractError(
                    f"stock {stock.get('code')!r} field {field} must be a finite number"
                )
            numeric_fields[field] = number
        if numeric_fields["latest_price"] <= 0:
            raise SnapshotContractError(
                f"stock {stock.get('code')!r} latest_price must be greater than zero"
            )
        if numeric_fields["prev_close"] <= 0:
            raise SnapshotContractError(
                f"stock {stock.get('code')!r} prev_close must be greater than zero"
            )
        if numeric_fields["high"] < numeric_fields["low"]:
            raise SnapshotContractError(
                f"stock {stock.get('code')!r} high must be greater than or equal to low"
            )
        if numeric_fields["volume"] < 0:
            raise SnapshotContractError(
                f"stock {stock.get('code')!r} volume must be non-negative"
            )
        if numeric_fields["amount"] < 0:
            raise SnapshotContractError(
                f"stock {stock.get('code')!r} amount must be non-negative"
            )

    expected_benchmark_codes = {"sh000001", "sh000300", "sz399006", "sh000688"}
    actual_benchmark_codes = set()
    for benchmark in benchmarks:
        if not isinstance(benchmark, Mapping):
            raise SnapshotContractError("each benchmarks item must be an object")
        actual_benchmark_codes.add(benchmark.get("code"))
        if benchmark.get("status") != "ok":
            raise SnapshotContractError(
                f"benchmark {benchmark.get('code')!r} is not usable"
            )
    if actual_benchmark_codes != expected_benchmark_codes:
        raise SnapshotContractError("benchmarks must contain the four required indexes")

    expected_status = "ok" if expected_tracking else "empty"
    if payload.get("portfolio_status") != expected_status:
        raise SnapshotContractError(f"portfolio_status must be {expected_status}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        payload = json.loads(args.path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise SnapshotContractError("snapshot root must be a JSON object")
        portfolio = load_portfolio_config(args.config)
        validate_snapshot_contract(payload, phase=args.phase, portfolio=portfolio)
    except (OSError, json.JSONDecodeError, SnapshotContractError, ValueError) as exc:
        LOGGER.error("snapshot contract failed: %s", exc)
        return 2
    LOGGER.info("snapshot contract passed: %s", args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
