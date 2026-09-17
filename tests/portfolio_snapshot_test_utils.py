from __future__ import annotations

from datetime import date, datetime
from typing import Any

from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_snapshot_contract import (
    OPTIONAL_FIELDS,
    TECHNICAL_FIELDS,
    apply_business_times,
    apply_data_quality,
)


SNAPSHOT_KIND = {
    "premarket": "previous_close_context",
    "midday": "morning_close",
    "close": "official_close",
}


def finalize_snapshot_fixture(
    payload: dict[str, Any],
    *,
    phase: str,
    portfolio: PortfolioConfig,
    trading_date: date,
    data_date: date,
    generated_at: datetime,
) -> dict[str, Any]:
    payload.update(
        {
            "schema_version": "2.0",
            "trading_date": trading_date.isoformat(),
            "snapshot_kind": SNAPSHOT_KIND[phase],
            "generation_mode": payload.get("generation_mode", "live"),
            "status": payload.get("status", "ok"),
            "completeness": payload.get("completeness", "full"),
        }
    )
    for item in payload.get("stocks", []):
        item.setdefault("data_date", data_date.isoformat())
        item.setdefault("source_details", {"history": "fixture", "realtime": None})
        for field in (*TECHNICAL_FIELDS, *OPTIONAL_FIELDS):
            item.setdefault(field, 1.0)
    for item in payload.get("benchmarks", []):
        item.setdefault("data_date", data_date.isoformat())
    payload["holdings"] = [
        item for item in payload.get("stocks", []) if item.get("tracking_type") == "holding"
    ]
    payload["watchlist"] = [
        item for item in payload.get("stocks", []) if item.get("tracking_type") == "watchlist"
    ]
    apply_business_times(
        payload,
        phase=phase,
        trading_date=trading_date,
        data_date=data_date,
        generated_at=generated_at,
    )
    apply_data_quality(
        payload,
        phase=phase,
        portfolio=portfolio,
        trading_date=trading_date,
        expected_data_date=data_date,
    )
    return payload
