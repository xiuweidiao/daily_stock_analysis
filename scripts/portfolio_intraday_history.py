#!/usr/bin/env python3
"""Historical intraday provider boundary used by snapshot reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd


class IntradayHistoryError(RuntimeError):
    """Raised when a provider cannot prove the requested historical interval."""


@dataclass(frozen=True)
class IntradayHistoryResult:
    frame: pd.DataFrame
    provider: str
    route: str
    requested_date: str
    actual_start: str
    actual_end: str
    interval: str
    timestamp_semantics: str


class HistoricalIntradayProvider(Protocol):
    def get_intraday_history(
        self,
        symbol: str,
        *,
        security_type: str,
        trading_date: date,
        interval: str = "1m",
    ) -> IntradayHistoryResult:
        ...


def sina_symbol(code: str, security_type: str) -> str:
    if security_type == "index":
        return code.lower()
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    return f"sz{code}"


class AkShareSinaIntradayProvider:
    """Recent 1-minute history from AkShare's Sina route.

    Sina exposes a rolling recent range rather than a date parameter.  The
    adapter therefore validates the actual returned range and exact target
    date instead of assuming a fixed retention window.
    """

    provider = "akshare"
    route = "sina_stock_zh_a_minute"

    def get_intraday_history(
        self,
        symbol: str,
        *,
        security_type: str,
        trading_date: date,
        interval: str = "1m",
    ) -> IntradayHistoryResult:
        if interval != "1m":
            raise IntradayHistoryError("AkShare Sina reconstruction requires 1m bars")
        import akshare as ak

        provider_symbol = sina_symbol(symbol, security_type)
        try:
            raw = ak.stock_zh_a_minute(
                symbol=provider_symbol, period="1", adjust=""
            )
        except Exception as exc:
            raise IntradayHistoryError(
                f"{self.route} failed for {provider_symbol}: {type(exc).__name__}: {exc}"
            ) from exc
        if raw is None or raw.empty:
            raise IntradayHistoryError(f"{self.route} returned no bars for {provider_symbol}")

        frame = raw.rename(
            columns={
                "day": "timestamp",
                "日期": "timestamp",
                "时间": "timestamp",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
            }
        ).copy()
        if "timestamp" not in frame.columns:
            raise IntradayHistoryError(f"{self.route} response has no timestamp column")
        required = {"open", "high", "low", "close", "volume"}
        missing = required.difference(frame.columns)
        if missing:
            raise IntradayHistoryError(
                f"{self.route} response missing columns: {sorted(missing)}"
            )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        if frame["timestamp"].dt.tz is None:
            frame["timestamp"] = frame["timestamp"].dt.tz_localize(
                ZoneInfo("Asia/Shanghai")
            )
        else:
            frame["timestamp"] = frame["timestamp"].dt.tz_convert(
                ZoneInfo("Asia/Shanghai")
            )
        for column in (*required, "amount"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        if frame.empty:
            raise IntradayHistoryError(f"{self.route} returned no usable bars")
        target = frame[frame["timestamp"].dt.date == trading_date]
        if target.empty:
            raise IntradayHistoryError(
                "RECONSTRUCTION_UNAVAILABLE: requested date is outside the actual "
                f"provider range {frame.iloc[0]['timestamp']}..{frame.iloc[-1]['timestamp']}"
            )
        return IntradayHistoryResult(
            frame=target.reset_index(drop=True),
            provider=self.provider,
            route=self.route,
            requested_date=trading_date.isoformat(),
            actual_start=frame.iloc[0]["timestamp"].isoformat(),
            actual_end=frame.iloc[-1]["timestamp"].isoformat(),
            interval=interval,
            timestamp_semantics="bar_end",
        )


def exact_morning_session(result: IntradayHistoryResult, trading_date: date) -> pd.DataFrame:
    """Return 09:31..11:30 end-labelled bars, requiring the exact close bar."""
    bars = result.frame.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], errors="coerce")
    bars = bars.dropna(subset=["timestamp"]).sort_values("timestamp")
    target = bars[
        (bars["timestamp"].dt.date == trading_date)
        & (bars["timestamp"].dt.strftime("%H:%M") >= "09:31")
        & (bars["timestamp"].dt.strftime("%H:%M") <= "11:30")
    ]
    if target.empty or target.iloc[-1]["timestamp"].strftime("%H:%M") != "11:30":
        raise IntradayHistoryError(
            "RECONSTRUCTION_UNAVAILABLE: provider did not return an exact 11:30 end-labelled bar"
        )
    return target.reset_index(drop=True)
