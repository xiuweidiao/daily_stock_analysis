#!/usr/bin/env python3
"""Business-time and data-quality contract for portfolio snapshots."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping, MutableMapping

from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_phase_policy import PREMARKET_END, SHANGHAI_TZ, as_shanghai_time


CORE_QUOTE_FIELDS = (
    "latest_price",
    "prev_close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
)
TECHNICAL_FIELDS = (
    "MA5",
    "MA10",
    "MA20",
    "MA60",
    "return_5d",
    "return_10d",
    "return_20d",
    "return_60d",
)
OPTIONAL_FIELDS = ("volume_ratio", "turnover_rate")
REQUIRED_BENCHMARKS = {"sh000001", "sh000300", "sz399006", "sh000688"}


def parse_contract_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    if parsed.utcoffset() != timedelta(hours=8):
        raise ValueError(f"{field} must use the Asia/Shanghai UTC+08:00 offset")
    return parsed.astimezone(SHANGHAI_TZ)


def phase_business_times(
    phase: str,
    *,
    trading_date: date,
    data_date: date,
    generated_at: datetime,
) -> tuple[datetime, datetime]:
    """Return the market time represented and the report information cutoff."""
    if phase == "premarket":
        return (
            datetime.combine(data_date, time(15, 0), SHANGHAI_TZ),
            datetime.combine(trading_date, PREMARKET_END, SHANGHAI_TZ),
        )
    if phase == "midday":
        morning_close = datetime.combine(data_date, time(11, 30), SHANGHAI_TZ)
        return morning_close, morning_close
    if phase == "close":
        official_close = datetime.combine(data_date, time(15, 0), SHANGHAI_TZ)
        return official_close, official_close
    if phase == "intraday":
        actual = as_shanghai_time(generated_at)
        return actual, actual
    raise ValueError(f"unsupported market phase: {phase}")


def apply_business_times(
    payload: MutableMapping[str, Any],
    *,
    phase: str,
    trading_date: date,
    data_date: date,
    generated_at: datetime,
) -> None:
    snapshot_as_of, information_cutoff = phase_business_times(
        phase,
        trading_date=trading_date,
        data_date=data_date,
        generated_at=generated_at,
    )
    payload["snapshot_as_of"] = snapshot_as_of.isoformat()
    payload["information_cutoff"] = information_cutoff.isoformat()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _issue(
    *,
    reason: str,
    code: str | None = None,
    field: str | None = None,
    scope: str = "snapshot",
) -> dict[str, str]:
    result = {"scope": scope, "reason": reason}
    if code is not None:
        result["code"] = code
    if field is not None:
        result["field"] = field
    return result


def assess_data_quality(
    payload: Mapping[str, Any],
    *,
    phase: str,
    portfolio: PortfolioConfig,
    trading_date: date,
    expected_data_date: date,
) -> dict[str, Any]:
    """Classify usability independently from optional-field completeness."""
    warnings: list[dict[str, str]] = []
    blocking_errors: list[dict[str, str]] = []

    if payload.get("market_phase") != phase:
        blocking_errors.append(_issue(field="market_phase", reason="phase_mismatch"))
    if payload.get("data_date") != expected_data_date.isoformat():
        blocking_errors.append(_issue(field="data_date", reason="wrong_trading_date"))
    if payload.get("holdings_codes") != list(portfolio.holdings):
        blocking_errors.append(_issue(field="holdings_codes", reason="portfolio_mismatch"))
    if payload.get("watchlist_codes") != list(portfolio.watchlist):
        blocking_errors.append(_issue(field="watchlist_codes", reason="portfolio_mismatch"))

    try:
        expected_as_of, expected_cutoff = phase_business_times(
            phase,
            trading_date=trading_date,
            data_date=expected_data_date,
            generated_at=parse_contract_timestamp(payload.get("generated_at"), "generated_at"),
        )
        snapshot_as_of = parse_contract_timestamp(payload.get("snapshot_as_of"), "snapshot_as_of")
        information_cutoff = parse_contract_timestamp(
            payload.get("information_cutoff"), "information_cutoff"
        )
        if snapshot_as_of != expected_as_of:
            blocking_errors.append(
                _issue(field="snapshot_as_of", reason="wrong_business_time")
            )
        if information_cutoff != expected_cutoff:
            blocking_errors.append(
                _issue(field="information_cutoff", reason="wrong_information_cutoff")
            )
    except ValueError as exc:
        blocking_errors.append(_issue(reason=str(exc), field="business_time"))
        expected_as_of = None

    require_exact_provider_time = (
        phase == "midday" and payload.get("generation_mode") == "reconstructed"
    )

    stocks = payload.get("stocks")
    expected_tracking = list(portfolio.tracked_securities())
    if not isinstance(stocks, list):
        blocking_errors.append(_issue(field="stocks", reason="invalid_structure"))
        stocks = []
    actual_tracking = [
        (item.get("code"), item.get("tracking_type"))
        for item in stocks
        if isinstance(item, Mapping)
    ]
    if actual_tracking != expected_tracking:
        blocking_errors.append(_issue(field="stocks", reason="portfolio_mismatch"))

    for stock in stocks:
        if not isinstance(stock, Mapping):
            blocking_errors.append(_issue(scope="stock", reason="invalid_structure"))
            continue
        code = str(stock.get("code") or "")
        if stock.get("status") == "error":
            blocking_errors.append(_issue(scope="stock", code=code, reason="source_failure"))
        if stock.get("data_date") != expected_data_date.isoformat():
            blocking_errors.append(
                _issue(scope="stock", code=code, field="data_date", reason="wrong_trading_date")
            )
        provider_timestamp = stock.get("provider_timestamp")
        if provider_timestamp is not None:
            try:
                provider_time = parse_contract_timestamp(
                    provider_timestamp, "provider_timestamp"
                )
            except ValueError:
                blocking_errors.append(
                    _issue(
                        scope="stock",
                        code=code,
                        field="provider_timestamp",
                        reason="unverifiable_market_time",
                    )
                )
            else:
                if provider_time.date() != expected_data_date:
                    blocking_errors.append(
                        _issue(
                            scope="stock",
                            code=code,
                            field="provider_timestamp",
                            reason="wrong_trading_date",
                        )
                    )
                elif (
                    require_exact_provider_time
                    and expected_as_of is not None
                    and provider_time != expected_as_of
                ):
                    blocking_errors.append(
                        _issue(
                            scope="stock",
                            code=code,
                            field="provider_timestamp",
                            reason="wrong_business_time",
                        )
                    )
        elif require_exact_provider_time:
            blocking_errors.append(
                _issue(
                    scope="stock",
                    code=code,
                    field="provider_timestamp",
                    reason="unverifiable_market_time",
                )
            )
        numeric: dict[str, float] = {}
        for field in CORE_QUOTE_FIELDS:
            number = _finite_number(stock.get(field))
            if number is None:
                blocking_errors.append(
                    _issue(scope="stock", code=code, field=field, reason="missing_core_field")
                )
            else:
                numeric[field] = number
        if numeric.get("latest_price", 1) <= 0:
            blocking_errors.append(
                _issue(scope="stock", code=code, field="latest_price", reason="invalid_core_field")
            )
        if numeric.get("prev_close", 1) <= 0:
            blocking_errors.append(
                _issue(scope="stock", code=code, field="prev_close", reason="invalid_core_field")
            )
        if "high" in numeric and "low" in numeric and numeric["high"] < numeric["low"]:
            blocking_errors.append(
                _issue(scope="stock", code=code, field="high", reason="invalid_price_range")
            )
        for field in ("volume", "amount"):
            if field in numeric and numeric[field] < 0:
                blocking_errors.append(
                    _issue(scope="stock", code=code, field=field, reason="invalid_core_field")
                )
        for field in TECHNICAL_FIELDS:
            if _finite_number(stock.get(field)) is None:
                warnings.append(
                    _issue(scope="stock", code=code, field=field, reason="insufficient_history")
                )
        for field in OPTIONAL_FIELDS:
            if _finite_number(stock.get(field)) is None:
                warnings.append(
                    _issue(scope="stock", code=code, field=field, reason="optional_field_unavailable")
                )

    benchmarks = payload.get("benchmarks")
    if not isinstance(benchmarks, list):
        blocking_errors.append(_issue(field="benchmarks", reason="invalid_structure"))
        benchmarks = []
    benchmark_codes = {
        item.get("code") for item in benchmarks if isinstance(item, Mapping)
    }
    if benchmark_codes != REQUIRED_BENCHMARKS:
        blocking_errors.append(_issue(field="benchmarks", reason="benchmark_set_mismatch"))
    for benchmark in benchmarks:
        if not isinstance(benchmark, Mapping):
            blocking_errors.append(_issue(scope="benchmark", reason="invalid_structure"))
            continue
        code = str(benchmark.get("code") or "")
        if benchmark.get("status") != "ok":
            blocking_errors.append(
                _issue(scope="benchmark", code=code, reason="required_benchmark_unavailable")
            )
        if benchmark.get("data_date") != expected_data_date.isoformat():
            blocking_errors.append(
                _issue(scope="benchmark", code=code, field="data_date", reason="wrong_trading_date")
            )
        if require_exact_provider_time:
            provider_timestamp = benchmark.get("provider_timestamp")
            try:
                provider_time = parse_contract_timestamp(
                    provider_timestamp, "provider_timestamp"
                )
            except ValueError:
                blocking_errors.append(
                    _issue(
                        scope="benchmark",
                        code=code,
                        field="provider_timestamp",
                        reason="unverifiable_market_time",
                    )
                )
            else:
                if expected_as_of is not None and provider_time != expected_as_of:
                    blocking_errors.append(
                        _issue(
                            scope="benchmark",
                            code=code,
                            field="provider_timestamp",
                            reason="wrong_business_time",
                        )
                    )

    return {
        "blocking": bool(blocking_errors),
        "warnings": warnings,
        "errors": blocking_errors,
    }


def apply_data_quality(
    payload: MutableMapping[str, Any],
    *,
    phase: str,
    portfolio: PortfolioConfig,
    trading_date: date,
    expected_data_date: date,
) -> None:
    assessment = assess_data_quality(
        payload,
        phase=phase,
        portfolio=portfolio,
        trading_date=trading_date,
        expected_data_date=expected_data_date,
    )
    warnings = assessment["warnings"]
    blocking_errors = assessment["errors"]
    blocking = assessment["blocking"]
    payload["blocking"] = blocking
    payload["warnings"] = warnings
    payload["data_quality"] = {
        "blocking": blocking,
        "warnings_count": len(warnings),
        "errors_count": len(blocking_errors),
        "errors": blocking_errors,
    }
    tracked = portfolio.tracked_securities()
    if blocking:
        payload["portfolio_status"] = "error"
        payload["status"] = "error"
    elif not tracked:
        payload["portfolio_status"] = "empty"
    elif warnings:
        payload["portfolio_status"] = "partial"
    else:
        payload["portfolio_status"] = "ok"
    if warnings and payload.get("completeness") == "full":
        payload["completeness"] = "partial"
