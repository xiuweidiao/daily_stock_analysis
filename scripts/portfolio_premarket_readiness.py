#!/usr/bin/env python3
"""Decide whether today's valid premarket snapshot already exists."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.check_portfolio_snapshot_ready import check_snapshot_ready
from scripts.portfolio_config import (
    DEFAULT_CONFIG_PATH,
    PortfolioConfig,
    load_portfolio_config,
)
from scripts.portfolio_market_data import _phase_data_date
from scripts.portfolio_phase_policy import SHANGHAI_TZ, as_shanghai_time
from src.core.trading_calendar import is_market_open


LOGGER = logging.getLogger("portfolio_premarket_readiness")
DEFAULT_PREMARKET_PATH = REPOSITORY_ROOT / "data" / "portfolio" / "premarket.json"


@dataclass(frozen=True)
class PremarketReadiness:
    fresh: bool
    should_generate: bool
    freshness: str
    reason: str
    generated_at: str | None
    data_date: str | None
    market_phase: str | None
    portfolio_status: str | None
    today_cn: str
    latest_completed_trading_day: str


def _metadata(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def inspect_premarket_snapshot(
    path: Path,
    *,
    portfolio: PortfolioConfig,
    now: datetime | None = None,
) -> PremarketReadiness:
    """Inspect one snapshot without fetching data or changing the file."""
    current = as_shanghai_time(now or datetime.now(SHANGHAI_TZ))
    expected_data_date = _phase_data_date("premarket", current).isoformat()
    metadata = _metadata(path)
    common = {
        "generated_at": metadata.get("generated_at"),
        "data_date": metadata.get("data_date"),
        "market_phase": metadata.get("market_phase"),
        "portfolio_status": metadata.get("portfolio_status"),
        "today_cn": current.date().isoformat(),
        "latest_completed_trading_day": expected_data_date,
    }

    if not is_market_open("cn", current.date()):
        return PremarketReadiness(
            fresh=False,
            should_generate=False,
            freshness="stale",
            reason="non_trading_day",
            **common,
        )

    readiness = check_snapshot_ready(
        path,
        phase="premarket",
        portfolio=portfolio,
        now=current,
    )
    if readiness.ready:
        return PremarketReadiness(
            fresh=True,
            should_generate=False,
            freshness="fresh",
            reason="already_fresh",
            **common,
        )

    freshness = (
        "stale"
        if readiness.reason in {"missing_snapshot", "stale_snapshot"}
        else "invalid"
    )
    return PremarketReadiness(
        fresh=False,
        should_generate=True,
        freshness=freshness,
        reason=readiness.reason,
        **common,
    )


def _single_line(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ")


def _write_github_output(result: PremarketReadiness) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    values = {
        "fresh": "true" if result.fresh else "false",
        "should_generate": "true" if result.should_generate else "false",
        "freshness": result.freshness,
        "reason": result.reason,
        "generated_at": result.generated_at,
        "data_date": result.data_date,
        "market_phase": result.market_phase,
        "portfolio_status": result.portfolio_status,
        "today_cn": result.today_cn,
        "latest_completed_trading_day": result.latest_completed_trading_day,
    }
    with Path(output_path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={_single_line(value)}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_PREMARKET_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        portfolio = load_portfolio_config(args.config)
        result = inspect_premarket_snapshot(args.path, portfolio=portfolio)
    except Exception as exc:
        current = datetime.now(SHANGHAI_TZ)
        result = PremarketReadiness(
            fresh=False,
            should_generate=True,
            freshness="invalid",
            reason=f"readiness_error: {exc}",
            generated_at=None,
            data_date=None,
            market_phase=None,
            portfolio_status=None,
            today_cn=current.date().isoformat(),
            latest_completed_trading_day="unavailable",
        )
    print(f"PREMARKET_FRESH={'true' if result.fresh else 'false'}")
    print(f"reason={result.reason}")
    LOGGER.info(
        "freshness=%s should_generate=%s expected_data_date=%s",
        result.freshness,
        result.should_generate,
        result.latest_completed_trading_day,
    )
    _write_github_output(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
