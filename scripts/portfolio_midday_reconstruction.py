#!/usr/bin/env python3
"""Reconstruct the deterministic A-share 11:30 morning-close snapshot."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

import pandas as pd

from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_intraday_history import (
    AkShareSinaIntradayProvider,
    HistoricalIntradayProvider,
    IntradayHistoryResult,
    exact_morning_session,
)
from scripts.portfolio_phase_policy import as_shanghai_time
from scripts.portfolio_snapshot_contract import apply_business_times, apply_data_quality


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _round(value: Any, digits: int = 4) -> float | None:
    number = _number(value)
    return round(number, digits) if number is not None else None


def _aggregate_morning(result: IntradayHistoryResult, target_date: date) -> dict[str, Any]:
    bars = exact_morning_session(result, target_date)
    amount = bars["amount"].sum() if "amount" in bars.columns and bars["amount"].notna().all() else None
    return {
        "open": _round(bars.iloc[0]["open"]),
        "high": _round(bars["high"].max()),
        "low": _round(bars["low"].min()),
        "close": _round(bars.iloc[-1]["close"]),
        "volume": _round(bars["volume"].sum(), 0),
        "amount": _round(amount, 2),
        "provider_timestamp": bars.iloc[-1]["timestamp"].isoformat(),
        "bar_count": len(bars),
    }


def _metric_values(history: pd.DataFrame, aggregate: Mapping[str, Any], target_date: date):
    from scripts.portfolio_market_data import calculate_metrics, _normalize_history_volume

    bars = _normalize_history_volume(history.copy())
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    bars = bars[bars["date"].dt.date < target_date].sort_values("date")
    if bars.empty:
        raise ValueError("no completed daily history before reconstruction date")
    synthetic = {column: None for column in bars.columns}
    synthetic.update(
        {
            "date": pd.Timestamp(target_date),
            "open": aggregate["open"],
            "high": aggregate["high"],
            "low": aggregate["low"],
            "close": aggregate["close"],
            "volume": aggregate["volume"],
            "amount": aggregate["amount"],
        }
    )
    frame = pd.concat([bars, pd.DataFrame([synthetic])], ignore_index=True)
    return calculate_metrics(frame), bars.iloc[-1]


def _security_item(
    code: str,
    tracking_type: str,
    *,
    target_date: date,
    now: datetime,
    sources: Any,
    provider: HistoricalIntradayProvider,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    try:
        result = provider.get_intraday_history(
            code, security_type="etf" if code.startswith(("15", "51", "56", "58")) else "stock",
            trading_date=target_date,
        )
        aggregate = _aggregate_morning(result, target_date)
        history, history_source = sources.history(code, end_date=target_date, days=100)
        metrics, previous = _metric_values(history, aggregate, target_date)
        values = dict(metrics.values)
        previous_close = _number(previous.get("close"))
        values.update(
            {
                "latest_price": aggregate["close"],
                "prev_close": _round(previous_close),
                "open": aggregate["open"],
                "high": aggregate["high"],
                "low": aggregate["low"],
                "volume": aggregate["volume"],
                "amount": aggregate["amount"],
                "change_pct": _round(
                    (aggregate["close"] / previous_close - 1) * 100
                    if previous_close not in {None, 0}
                    else None
                ),
                "amplitude": _round(
                    (aggregate["high"] - aggregate["low"]) / previous_close * 100
                    if previous_close not in {None, 0}
                    else None
                ),
                "turnover_rate": None,
                "volume_ratio": None,
            }
        )
        core_missing = [
            key for key in (
                "latest_price", "prev_close", "open", "high", "low", "volume", "amount",
                "MA5", "MA10", "MA20", "MA60",
                "return_5d", "return_10d", "return_20d", "return_60d",
            )
            if values.get(key) is None
        ]
        status = "partial" if core_missing else "ok"
        item = {
            "code": code,
            "name": sources.name(code),
            "tracking_type": tracking_type,
            **values,
            "source": "akshare_sina_minute",
            "source_details": {
                "history": history_source,
                "intraday": result.route,
                "volume_ratio": None,
                "requested_date": result.requested_date,
                "actual_range": [result.actual_start, result.actual_end],
                "interval": result.interval,
                "timestamp_semantics": result.timestamp_semantics,
            },
            "fetched_at": now.isoformat(),
            "provider_timestamp": aggregate["provider_timestamp"],
            "data_timestamp": aggregate["provider_timestamp"],
            "freshness_status": "fresh",
            "data_date": target_date.isoformat(),
            "status": status,
            "unavailable_fields": ["volume_ratio", "turnover_rate"],
        }
        if core_missing:
            item["status_detail"] = f"missing reconstructed core fields: {','.join(core_missing)}"
        return item, errors
    except Exception as exc:
        errors.append({"scope": "stock", "code": code, "stage": "reconstruction", "message": str(exc)})
        return {
            "code": code,
            "name": "",
            "tracking_type": tracking_type,
            "source": "unavailable",
            "source_details": {"history": None, "intraday": None, "volume_ratio": None},
            "fetched_at": now.isoformat(),
            "provider_timestamp": None,
            "data_timestamp": None,
            "freshness_status": "unknown",
            "data_date": target_date.isoformat(),
            "status": "error",
            "status_detail": str(exc),
        }, errors


def _benchmark_item(
    code: str,
    name: str,
    *,
    target_date: date,
    now: datetime,
    sources: Any,
    provider: HistoricalIntradayProvider,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    try:
        result = provider.get_intraday_history(
            code, security_type="index", trading_date=target_date
        )
        aggregate = _aggregate_morning(result, target_date)
        history, history_source = sources.benchmark_history(code, end_date=target_date, days=5)
        bars = history.copy()
        bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
        previous = bars[bars["date"].dt.date < target_date].sort_values("date")
        previous_close = _number(previous.iloc[-1].get("close")) if not previous.empty else None
        change_pct = _round(
            (aggregate["close"] / previous_close - 1) * 100
            if previous_close not in {None, 0}
            else None
        )
        core_missing = [
            key
            for key, value in {
                "close": aggregate.get("close"),
                "open": aggregate.get("open"),
                "high": aggregate.get("high"),
                "low": aggregate.get("low"),
                "volume": aggregate.get("volume"),
                "amount": aggregate.get("amount"),
                "prev_close": previous_close,
                "change_pct": change_pct,
            }.items()
            if value is None
        ]
        return {
            "code": code,
            "name": name,
            "latest_price": aggregate["close"],
            "change_pct": change_pct,
            "open": aggregate["open"],
            "high": aggregate["high"],
            "low": aggregate["low"],
            "prev_close": _round(previous_close),
            "volume": aggregate["volume"],
            "amount": aggregate["amount"],
            "amplitude": _round((aggregate["high"] - aggregate["low"]) / previous_close * 100 if previous_close not in {None, 0} else None),
            "source": "akshare_sina_minute",
            "source_details": {
                "history": history_source,
                "intraday": result.route,
                "requested_date": result.requested_date,
                "actual_range": [result.actual_start, result.actual_end],
                "interval": result.interval,
                "timestamp_semantics": result.timestamp_semantics,
            },
            "fetched_at": now.isoformat(),
            "provider_timestamp": aggregate["provider_timestamp"],
            "data_timestamp": aggregate["provider_timestamp"],
            "freshness_status": "fresh",
            "data_date": target_date.isoformat(),
            "status": "partial" if core_missing else "ok",
        }, []
    except Exception as exc:
        return {
            "code": code,
            "name": name,
            "source": "unavailable",
            "fetched_at": now.isoformat(),
            "provider_timestamp": None,
            "data_timestamp": None,
            "freshness_status": "unknown",
            "data_date": target_date.isoformat(),
            "status": "error",
            "status_detail": str(exc),
        }, [{"scope": "benchmark", "code": code, "stage": "reconstruction", "message": str(exc)}]


def reconstruct_midday_snapshot(
    *,
    target_date: date,
    portfolio: PortfolioConfig,
    sources: Any,
    now: datetime,
    provider: HistoricalIntradayProvider | None = None,
) -> dict[str, Any]:
    from scripts.portfolio_market_data import BENCHMARKS

    current = as_shanghai_time(now)
    provider = provider or AkShareSinaIntradayProvider()
    stocks = []
    errors: list[dict[str, str]] = []
    for code, tracking_type in portfolio.tracked_securities():
        item, item_errors = _security_item(
            code,
            tracking_type,
            target_date=target_date,
            now=current,
            sources=sources,
            provider=provider,
        )
        stocks.append(item)
        errors.extend(item_errors)
    benchmarks = []
    for code, name in BENCHMARKS.items():
        item, item_errors = _benchmark_item(
            code,
            name,
            target_date=target_date,
            now=current,
            sources=sources,
            provider=provider,
        )
        benchmarks.append(item)
        errors.extend(item_errors)

    core_usable = all(item.get("status") in {"ok", "partial"} for item in stocks)
    benchmark_usable = all(item.get("status") in {"ok", "partial"} for item in benchmarks)
    core_missing = any(
        item.get("status") == "partial" for item in [*stocks, *benchmarks]
    )
    status = "ok" if core_usable and benchmark_usable and not core_missing else "partial"
    payload = {
        "schema_version": "2.0",
        "trading_date": target_date.isoformat(),
        "market_phase": "midday",
        "snapshot_kind": "morning_close",
        "generated_at": current.isoformat(),
        "timezone": "Asia/Shanghai",
        "generation_mode": "reconstructed",
        "data_date": target_date.isoformat(),
        "status": status,
        "completeness": "partial",
        "portfolio_status": "ok" if portfolio.tracked_securities() else "empty",
        "holdings_codes": list(portfolio.holdings),
        "watchlist_codes": list(portfolio.watchlist),
        "holdings": [
            item for item in stocks if item.get("tracking_type") == "holding"
        ],
        "watchlist": [
            item for item in stocks if item.get("tracking_type") == "watchlist"
        ],
        "stocks": stocks,
        "benchmarks": benchmarks,
        "errors": errors,
        "provenance": {
            "provider": "akshare",
            "route": "sina_stock_zh_a_minute",
            "requested_date": target_date.isoformat(),
            "interval": "1m",
            "timestamp_semantics": "bar_end",
            "reconstructed_at": current.isoformat(),
        },
    }
    apply_business_times(
        payload,
        phase="midday",
        trading_date=target_date,
        data_date=target_date,
        generated_at=current,
    )
    apply_data_quality(
        payload,
        phase="midday",
        portfolio=portfolio,
        trading_date=target_date,
        expected_data_date=target_date,
    )
    return payload
