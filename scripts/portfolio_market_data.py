#!/usr/bin/env python3
"""Generate source-attributed A-share portfolio snapshots as stable JSON files."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

import pandas as pd

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.baostock_fetcher import BaostockFetcher
from data_provider.base import BaseFetcher, DataFetcherManager
from data_provider.efinance_fetcher import EfinanceFetcher
from data_provider.pytdx_fetcher import PytdxFetcher
from data_provider.tencent_fetcher import TencentFetcher
from scripts.portfolio_config import (
    DEFAULT_CONFIG_PATH,
    PortfolioConfig,
    PortfolioConfigError,
    load_portfolio_config,
    validate_security_code,
)
from scripts.portfolio_phase_policy import (
    PhaseTimeError,
    SHANGHAI_TZ,
    validate_phase_time,
)
from src.core.trading_calendar import get_effective_trading_date, is_market_open

LOGGER = logging.getLogger("portfolio_market_data")
TIMEZONE_NAME = "Asia/Shanghai"

BENCHMARKS: Mapping[str, str] = {
    "sh000001": "上证指数",
    "sh000300": "沪深300",
    "sz399006": "创业板指",
    "sh000688": "科创50",
}

REPORT_PHASES = ("premarket", "midday", "close")
PHASES = (*REPORT_PHASES, "intraday")
OFFICIAL_OUTPUT_DIR = REPOSITORY_ROOT / "data" / "portfolio"
DIAGNOSTICS_OUTPUT_DIR = OFFICIAL_OUTPUT_DIR / "diagnostics"
STOCK_FIELDS = (
    "code",
    "name",
    "tracking_type",
    "latest_price",
    "change_pct",
    "open",
    "high",
    "low",
    "prev_close",
    "volume",
    "amount",
    "turnover_rate",
    "amplitude",
    "volume_ratio",
    "volume_vs_5d_avg",
    "MA5",
    "MA10",
    "MA20",
    "MA60",
    "return_5d",
    "return_10d",
    "return_20d",
    "return_60d",
    "volume_vs_20d_avg",
    "source",
    "fetched_at",
    "provider_timestamp",
    "data_timestamp",
    "freshness_status",
    "status",
)


class DiagnosticOutputError(ValueError):
    """Raised when a diagnostics run could overwrite official snapshots."""


class PortfolioSources(Protocol):
    """Dependency boundary used by deterministic tests and the live adapter."""

    def history(self, code: str, *, end_date: date, days: int) -> Tuple[pd.DataFrame, str]: ...

    def quote(self, code: str) -> Tuple[Optional[Any], Optional[str], List[str]]: ...

    def name(self, code: str) -> str: ...

    def benchmarks(self) -> Tuple[List[Dict[str, Any]], Dict[str, str], List[str]]: ...


@dataclass(frozen=True)
class MetricResult:
    values: Dict[str, Optional[float]]
    has_60_day_ma: bool
    has_60_day_return: bool


class FreeProjectSources:
    """Adapter over the repository's existing token-free source implementations."""

    def __init__(self, fetchers: Optional[Sequence[BaseFetcher]] = None) -> None:
        if fetchers is None:
            fetchers = (
                EfinanceFetcher(),
                AkshareFetcher(),
                PytdxFetcher(),
                BaostockFetcher(),
                TencentFetcher(),
            )
        self.fetchers = list(fetchers)
        self.manager = DataFetcherManager(fetchers=self.fetchers)
        self._history_cache: Dict[Tuple[str, date, int], Tuple[pd.DataFrame, str]] = {}
        self._quote_cache: Dict[str, Tuple[Optional[Any], Optional[str], List[str]]] = {}
        self._name_cache: Dict[str, str] = {}
        self._benchmark_cache: Optional[Tuple[List[Dict[str, Any]], Dict[str, str], List[str]]] = None

    def history(self, code: str, *, end_date: date, days: int) -> Tuple[pd.DataFrame, str]:
        key = (code, end_date, days)
        if key not in self._history_cache:
            self._history_cache[key] = self.manager.get_daily_data(
                code, end_date=end_date.isoformat(), days=days
            )
        frame, source = self._history_cache[key]
        return frame.copy(), source

    def quote(self, code: str) -> Tuple[Optional[Any], Optional[str], List[str]]:
        if code in self._quote_cache:
            return self._quote_cache[code]
        errors: List[str] = []
        routes: List[Tuple[str, Optional[BaseFetcher], Dict[str, Any]]] = []
        by_name = {fetcher.name: fetcher for fetcher in self.fetchers}
        efinance = by_name.get("EfinanceFetcher")
        akshare = by_name.get("AkshareFetcher")
        routes.append(("efinance", efinance, {}))
        routes.extend(
            (
                ("akshare_em", akshare, {"source": "em"}),
                ("akshare_sina", akshare, {"source": "sina"}),
                ("akshare_tencent", akshare, {"source": "tencent"}),
            )
        )

        for source, fetcher, kwargs in routes:
            if fetcher is None or not hasattr(fetcher, "get_realtime_quote"):
                errors.append(f"{source}: unavailable")
                continue
            try:
                quote = fetcher.get_realtime_quote(code, **kwargs)
                if quote is not None and quote.has_basic_data():
                    result = (quote, source, errors)
                    self._quote_cache[code] = result
                    return result
                errors.append(f"{source}: empty or incomplete quote")
            except Exception as exc:  # provider failures must not stop fallback
                errors.append(f"{source}: {type(exc).__name__}: {exc}")
        result = (None, None, errors)
        self._quote_cache[code] = result
        return result

    def name(self, code: str) -> str:
        if code not in self._name_cache:
            self._name_cache[code] = str(
                self.manager.get_stock_name(code, allow_realtime=False) or ""
            ).strip()
        return self._name_cache[code]

    def benchmarks(self) -> Tuple[List[Dict[str, Any]], Dict[str, str], List[str]]:
        if self._benchmark_cache is not None:
            rows, sources, errors = self._benchmark_cache
            return [dict(row) for row in rows], dict(sources), list(errors)
        merged: Dict[str, Dict[str, Any]] = {}
        sources: Dict[str, str] = {}
        errors: List[str] = []
        for fetcher in self.fetchers:
            if not hasattr(fetcher, "get_main_indices"):
                continue
            try:
                rows = fetcher.get_main_indices(region="cn") or []
            except Exception as exc:
                errors.append(f"{fetcher.name}: {type(exc).__name__}: {exc}")
                continue
            if not rows:
                errors.append(f"{fetcher.name}: empty benchmark response")
                continue
            for row in rows:
                code = str(row.get("code") or "").lower()
                if code in BENCHMARKS and code not in merged:
                    merged[code] = dict(row)
                    sources[code] = fetcher.name
            if len(merged) == len(BENCHMARKS):
                break
        result = (list(merged.values()), sources, errors)
        self._benchmark_cache = result
        return [dict(row) for row in result[0]], dict(result[1]), list(result[2])


def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rounded(value: Any, digits: int = 4) -> Optional[float]:
    number = _finite_number(value)
    return round(number, digits) if number is not None else None


def _iso_timestamp(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(SHANGHAI_TZ)
    else:
        parsed = parsed.tz_convert(SHANGHAI_TZ)
    return parsed.isoformat()


def _freshness_status(
    provider_timestamp: Optional[str], *, fetched_at: datetime, phase: str
) -> str:
    if provider_timestamp is None or phase == "premarket":
        return "unknown"
    try:
        provider_time = pd.Timestamp(provider_timestamp).to_pydatetime()
    except (TypeError, ValueError):
        return "unknown"
    if provider_time.tzinfo is None:
        provider_time = provider_time.replace(tzinfo=SHANGHAI_TZ)
    provider_time = provider_time.astimezone(SHANGHAI_TZ)
    age = fetched_at - provider_time
    if age < -timedelta(minutes=5):
        return "unknown"
    if phase == "close":
        is_formal_close = (
            provider_time.date() == fetched_at.date()
            and provider_time.timetz().replace(tzinfo=None) >= time(15, 0)
        )
        return "fresh" if is_formal_close else "stale"
    return "fresh" if age <= timedelta(minutes=15) else "stale"


def validate_portfolio_codes(codes: Iterable[str]) -> Tuple[str, ...]:
    """Return unique plain six-digit codes or raise on ambiguous input."""
    normalized: List[str] = []
    seen = set()
    for raw_code in codes:
        code = validate_security_code(raw_code)
        if code not in seen:
            normalized.append(code)
            seen.add(code)
    return tuple(normalized)


def calculate_metrics(frame: pd.DataFrame) -> MetricResult:
    """Calculate metrics on ascending daily bars using one consistent definition."""
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"history missing columns: {sorted(missing)}")

    bars = frame.copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
        if column in bars.columns:
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
    bars = bars.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
    if bars.empty:
        raise ValueError("history contains no usable daily bars")

    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)
    latest = bars.iloc[-1]
    previous_close = close.iloc[-2] if len(close) >= 2 else None

    values: Dict[str, Optional[float]] = {
        "latest_price": _rounded(latest.get("close")),
        "change_pct": _rounded(latest.get("pct_chg")),
        "open": _rounded(latest.get("open")),
        "high": _rounded(latest.get("high")),
        "low": _rounded(latest.get("low")),
        "prev_close": _rounded(previous_close),
        "volume": _rounded(latest.get("volume"), 0),
        "amount": _rounded(latest.get("amount"), 2),
        "turnover_rate": None,
        "volume_ratio": None,
    }
    if values["change_pct"] is None and previous_close not in (None, 0):
        values["change_pct"] = _rounded((float(close.iloc[-1]) / float(previous_close) - 1) * 100)

    high = _finite_number(latest.get("high"))
    low = _finite_number(latest.get("low"))
    if previous_close not in (None, 0) and high is not None and low is not None:
        values["amplitude"] = _rounded(
            (high - low) / float(previous_close) * 100
        )
    else:
        values["amplitude"] = None

    previous_five_volume = volume.iloc[-6:-1] if len(volume) >= 6 else pd.Series(dtype=float)
    values["volume_vs_5d_avg"] = _rounded(
        float(volume.iloc[-1]) / float(previous_five_volume.mean())
        if len(previous_five_volume) == 5 and previous_five_volume.mean() != 0
        else None
    )
    previous_twenty_volume = volume.iloc[-21:-1] if len(volume) >= 21 else pd.Series(dtype=float)
    values["volume_vs_20d_avg"] = _rounded(
        float(volume.iloc[-1]) / float(previous_twenty_volume.mean())
        if len(previous_twenty_volume) == 20 and previous_twenty_volume.mean() != 0
        else None
    )

    for window in (5, 10, 20, 60):
        values[f"MA{window}"] = _rounded(close.iloc[-window:].mean()) if len(close) >= window else None
        values[f"return_{window}d"] = (
            _rounded((float(close.iloc[-1]) / float(close.iloc[-window - 1]) - 1) * 100)
            if len(close) > window and close.iloc[-window - 1] != 0
            else None
        )

    return MetricResult(values=values, has_60_day_ma=len(close) >= 60, has_60_day_return=len(close) >= 61)


def _reconciled_quote_volume(
    frame: pd.DataFrame, quote: Any, target_date: date
) -> Optional[float]:
    """Reconcile hand/share quote units when a native volume ratio can anchor them."""
    raw_volume = _finite_number(getattr(quote, "volume", None))
    native_ratio = _finite_number(getattr(quote, "volume_ratio", None))
    if raw_volume is None or raw_volume <= 0 or native_ratio is None or native_ratio <= 0:
        return raw_volume

    bars = frame.copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    bars["volume"] = pd.to_numeric(bars["volume"], errors="coerce")
    previous = bars[bars["date"].dt.date < target_date].dropna(subset=["volume"])
    previous_five = previous.sort_values("date")["volume"].tail(5)
    average_volume = _finite_number(previous_five.mean()) if len(previous_five) == 5 else None
    if average_volume is None or average_volume <= 0:
        return raw_volume

    candidates = (raw_volume, raw_volume * 100, raw_volume / 100)
    return min(
        candidates,
        key=lambda candidate: abs(math.log((candidate / average_volume) / native_ratio)),
    )


def _overlay_quote(
    frame: pd.DataFrame, quote: Any, target_date: date, quote_volume: Optional[float]
) -> pd.DataFrame:
    bars = frame.copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    bars = bars.dropna(subset=["date"]).sort_values("date")
    quote_values = {
        "close": getattr(quote, "price", None),
        "open": getattr(quote, "open_price", None),
        "high": getattr(quote, "high", None),
        "low": getattr(quote, "low", None),
        "volume": quote_volume,
        "amount": getattr(quote, "amount", None),
        "pct_chg": getattr(quote, "change_pct", None),
    }
    target_timestamp = pd.Timestamp(target_date)
    matching = bars.index[bars["date"].dt.normalize() == target_timestamp]
    if len(matching):
        row_index = matching[-1]
        for column, value in quote_values.items():
            if _finite_number(value) is not None:
                bars.loc[row_index, column] = value
        return bars

    row: Dict[str, Any] = {column: None for column in bars.columns}
    row.update({"date": target_timestamp, **quote_values})
    return pd.concat([bars, pd.DataFrame([row])], ignore_index=True)


def _phase_data_date(phase: str, now: datetime) -> date:
    if phase == "premarket" or not is_market_open("cn", now.date()):
        return get_effective_trading_date("cn", current_time=now)
    return now.date()


def _empty_stock(code: str, tracking_type: str, fetched_at: str) -> Dict[str, Any]:
    item = {field: None for field in STOCK_FIELDS}
    item.update(
        {
            "code": code,
            "name": "",
            "tracking_type": tracking_type,
            "source": "unavailable",
            "source_details": {"history": None, "realtime": None, "volume_ratio": None},
            "fetched_at": fetched_at,
            "provider_timestamp": None,
            "data_timestamp": None,
            "freshness_status": "unknown",
            "data_date": None,
            "status": "error",
        }
    )
    return item


def build_stock_item(
    code: str,
    *,
    tracking_type: str,
    phase: str,
    expected_date: date,
    now: datetime,
    sources: PortfolioSources,
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    fetched_at = now.isoformat()
    item = _empty_stock(code, tracking_type, fetched_at)
    errors: List[Dict[str, str]] = []
    try:
        history, history_source = sources.history(code, end_date=expected_date, days=100)
    except Exception as exc:
        errors.append({"scope": "stock", "code": code, "stage": "history", "message": str(exc)})
        return item, errors

    quote, quote_source, quote_errors = sources.quote(code)
    realtime_used = phase != "premarket" and quote is not None
    quote_volume = (
        _reconciled_quote_volume(history, quote, expected_date) if realtime_used else None
    )
    calculation_frame = (
        _overlay_quote(history, quote, expected_date, quote_volume)
        if realtime_used
        else history
    )
    try:
        metrics = calculate_metrics(calculation_frame)
    except Exception as exc:
        errors.append({"scope": "stock", "code": code, "stage": "metrics", "message": str(exc)})
        return item, errors

    last_bar_date = pd.to_datetime(calculation_frame["date"], errors="coerce").dropna().max().date()
    name = str(getattr(quote, "name", "") or "").strip() if quote is not None else ""
    if not name:
        name = sources.name(code)

    values = dict(metrics.values)
    if realtime_used:
        quote_fields = {
            "latest_price": getattr(quote, "price", None),
            "change_pct": getattr(quote, "change_pct", None),
            "open": getattr(quote, "open_price", None),
            "high": getattr(quote, "high", None),
            "low": getattr(quote, "low", None),
            "prev_close": getattr(quote, "pre_close", None),
            "volume": quote_volume,
            "amount": getattr(quote, "amount", None),
            "turnover_rate": getattr(quote, "turnover_rate", None),
            "amplitude": getattr(quote, "amplitude", None),
            "volume_ratio": getattr(quote, "volume_ratio", None),
        }
        for field, raw_value in quote_fields.items():
            numeric = _rounded(raw_value, 0 if field == "volume" else 4)
            if numeric is not None:
                values[field] = numeric

    source = quote_source if realtime_used and quote_source else history_source
    provider_timestamp = _iso_timestamp(getattr(quote, "provider_timestamp", None)) if realtime_used else None
    freshness_status = _freshness_status(provider_timestamp, fetched_at=now, phase=phase)
    native_volume_ratio = values.get("volume_ratio") if realtime_used else None
    partial_reasons: List[str] = []
    if last_bar_date != expected_date:
        partial_reasons.append(f"latest bar is {last_bar_date}, expected {expected_date}")
    if not metrics.has_60_day_ma or not metrics.has_60_day_return:
        partial_reasons.append("insufficient bars for MA60/return_60d")
    if not name:
        partial_reasons.append("security name unavailable")
    if phase != "premarket" and quote is None:
        partial_reasons.append("realtime quote unavailable; daily history used")
        errors.append(
            {
                "scope": "stock",
                "code": code,
                "stage": "realtime",
                "message": "; ".join(quote_errors) or "all free realtime sources failed",
            }
        )
    missing_core_fields = [
        field
        for field in ("latest_price", "open", "high", "low", "prev_close", "volume", "amount")
        if values.get(field) is None
    ]
    if missing_core_fields:
        partial_reasons.append(f"missing core fields: {','.join(missing_core_fields)}")

    item.update(values)
    item.update(
        {
            "code": code,
            "name": name,
            "tracking_type": tracking_type,
            "source": source,
            "source_details": {
                "history": history_source,
                "realtime": quote_source if realtime_used else None,
                "volume_ratio": quote_source if native_volume_ratio is not None else None,
            },
            "fetched_at": fetched_at,
            "provider_timestamp": provider_timestamp,
            "data_timestamp": provider_timestamp,
            "freshness_status": freshness_status,
            "data_date": last_bar_date.isoformat(),
            "status": "partial" if partial_reasons else "ok",
        }
    )
    if partial_reasons:
        item["status_detail"] = "; ".join(partial_reasons)
    return item, errors


def build_benchmarks(
    *, sources: PortfolioSources, data_date: date, now: datetime, phase: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    fetched_at = now.isoformat()
    rows, source_by_code, provider_errors = sources.benchmarks()
    by_code = {str(row.get("code") or "").lower(): row for row in rows}
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for code, name in BENCHMARKS.items():
        row = by_code.get(code)
        if row is None:
            results.append(
                {
                    "code": code,
                    "name": name,
                    "latest_price": None,
                    "change_pct": None,
                    "open": None,
                    "high": None,
                    "low": None,
                    "prev_close": None,
                    "volume": None,
                    "amount": None,
                    "amplitude": None,
                    "source": "unavailable",
                    "fetched_at": fetched_at,
                    "provider_timestamp": None,
                    "data_timestamp": None,
                    "freshness_status": "unknown",
                    "data_date": data_date.isoformat(),
                    "status": "error",
                }
            )
            errors.append(
                {
                    "scope": "benchmark",
                    "code": code,
                    "stage": "quote",
                    "message": "; ".join(provider_errors) or "all free benchmark sources failed",
                }
            )
            continue
        provider_timestamp = _iso_timestamp(row.get("provider_timestamp"))
        results.append(
            {
                "code": code,
                "name": name,
                "latest_price": _rounded(row.get("current")),
                "change_pct": _rounded(row.get("change_pct")),
                "open": _rounded(row.get("open")),
                "high": _rounded(row.get("high")),
                "low": _rounded(row.get("low")),
                "prev_close": _rounded(row.get("prev_close")),
                "volume": _rounded(row.get("volume"), 0),
                "amount": _rounded(row.get("amount"), 2),
                "amplitude": _rounded(row.get("amplitude")),
                "source": source_by_code[code],
                "fetched_at": fetched_at,
                "provider_timestamp": provider_timestamp,
                "data_timestamp": provider_timestamp,
                "freshness_status": _freshness_status(
                    provider_timestamp, fetched_at=now, phase=phase
                ),
                "data_date": data_date.isoformat(),
                "status": "ok" if _finite_number(row.get("current")) is not None else "partial",
            }
        )
    return results, errors


def build_payload(
    phase: str,
    *,
    sources: PortfolioSources,
    portfolio: PortfolioConfig,
    now: Optional[datetime] = None,
    allow_phase_time_override: bool = False,
) -> Dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unsupported market phase: {phase}")
    current = now or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    else:
        current = current.astimezone(SHANGHAI_TZ)
    if not allow_phase_time_override:
        validate_phase_time(phase, current)
    generated_at = current.isoformat()
    data_date = _phase_data_date(phase, current)
    errors: List[Dict[str, str]] = []
    stocks: List[Dict[str, Any]] = []
    tracked_securities = portfolio.tracked_securities()
    if not tracked_securities:
        LOGGER.warning("portfolio config contains no holdings or watchlist codes; benchmarks only")
    for code, tracking_type in tracked_securities:
        item, item_errors = build_stock_item(
            code,
            tracking_type=tracking_type,
            phase=phase,
            expected_date=data_date,
            now=current,
            sources=sources,
        )
        stocks.append(item)
        errors.extend(item_errors)
    benchmarks, benchmark_errors = build_benchmarks(
        sources=sources, data_date=data_date, now=current, phase=phase
    )
    errors.extend(benchmark_errors)
    return {
        "generated_at": generated_at,
        "timezone": TIMEZONE_NAME,
        "market_phase": phase,
        "data_date": data_date.isoformat(),
        "portfolio_status": "ok" if tracked_securities else "empty",
        "holdings_codes": list(portfolio.holdings),
        "watchlist_codes": list(portfolio.watchlist),
        "stocks": stocks,
        "benchmarks": benchmarks,
        "errors": errors,
    }


def write_payload(payload: Mapping[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=(*PHASES, "all"), required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="generate on a non-trading day (manual diagnostics only)",
    )
    parser.add_argument(
        "--allow-phase-time-override",
        action="store_true",
        help="bypass phase clock checks (tests/diagnostics only; never used by the workflow)",
    )
    return parser.parse_args()


def resolve_output_dir(
    requested_output_dir: Optional[Path], *, diagnostics_run: bool
) -> Path:
    output_dir = requested_output_dir
    if output_dir is None:
        output_dir = DIAGNOSTICS_OUTPUT_DIR if diagnostics_run else OFFICIAL_OUTPUT_DIR
    if diagnostics_run and output_dir.resolve() == OFFICIAL_OUTPUT_DIR.resolve():
        raise DiagnosticOutputError(
            "diagnostics cannot write to the official data/portfolio directory"
        )
    return output_dir


def generate_snapshots(
    phase_selection: str,
    *,
    sources: PortfolioSources,
    portfolio: PortfolioConfig,
    now: datetime,
    output_dir: Path,
    allow_phase_time_override: bool = False,
) -> List[Path]:
    if phase_selection == "all" and not allow_phase_time_override:
        raise PhaseTimeError(
            "all is diagnostics/tests only; use --allow-phase-time-override"
        )
    phases = REPORT_PHASES if phase_selection == "all" else (phase_selection,)
    if any(phase not in PHASES for phase in phases):
        raise ValueError(f"unsupported market phase: {phase_selection}")
    if allow_phase_time_override and output_dir.resolve() == OFFICIAL_OUTPUT_DIR.resolve():
        raise DiagnosticOutputError(
            "diagnostics cannot write to the official data/portfolio directory"
        )
    if not allow_phase_time_override:
        for phase in phases:
            validate_phase_time(phase, now)

    written: List[Path] = []
    for phase in phases:
        payload = build_payload(
            phase,
            sources=sources,
            portfolio=portfolio,
            now=now,
            allow_phase_time_override=allow_phase_time_override,
        )
        output_path = output_dir / f"{phase}.json"
        write_payload(payload, output_path)
        LOGGER.info(
            "wrote %s (%d stocks, %d errors)",
            output_path,
            len(payload["stocks"]),
            len(payload["errors"]),
        )
        written.append(output_path)
    return written


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    now = datetime.now(SHANGHAI_TZ)
    try:
        portfolio = load_portfolio_config(args.config)
        diagnostics_run = args.allow_phase_time_override or args.force
        output_dir = resolve_output_dir(
            args.output_dir, diagnostics_run=diagnostics_run
        )
        if args.phase == "all" and not args.allow_phase_time_override:
            raise PhaseTimeError(
                "all is diagnostics/tests only; use --allow-phase-time-override"
            )
        if not args.force and not is_market_open("cn", now.date()):
            LOGGER.info(
                "%s is not an A-share trading day; no portfolio files were updated",
                now.date(),
            )
            return 0
        sources = FreeProjectSources()
        generate_snapshots(
            args.phase,
            sources=sources,
            portfolio=portfolio,
            now=now,
            output_dir=output_dir,
            allow_phase_time_override=args.allow_phase_time_override,
        )
    except (DiagnosticOutputError, PhaseTimeError, PortfolioConfigError) as exc:
        LOGGER.error("%s; no portfolio files were updated", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
