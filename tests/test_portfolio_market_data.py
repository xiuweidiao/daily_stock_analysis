from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from data_provider.base import BaseFetcher, DataFetchError, DataFetcherManager
from scripts.portfolio_market_data import (
    BENCHMARKS,
    PORTFOLIO_CODES,
    STOCK_FIELDS,
    FreeProjectSources,
    build_payload,
    calculate_metrics,
    validate_portfolio_codes,
    write_payload,
)


def _history_frame(rows: int = 81, end: str = "2026-08-14") -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=rows)
    closes = [float(index + 1) for index in range(rows)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [value - 0.2 for value in closes],
            "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes],
            "close": closes,
            "volume": [1000 + index for index in range(rows)],
            "amount": [(1000 + index) * value for index, value in enumerate(closes)],
            "pct_chg": pd.Series(closes).pct_change().fillna(0) * 100,
        }
    )


class _FakeSources:
    def history(self, code: str, *, end_date: date, days: int):
        assert code in PORTFOLIO_CODES
        assert days >= 61
        return _history_frame(end=end_date.isoformat()), "FakeHistory"

    def quote(self, code: str):
        quote = SimpleNamespace(
            code=code,
            name=f"证券{code}",
            price=81.5,
            change_pct=1.25,
            open_price=80.5,
            high=82.0,
            low=80.0,
            pre_close=80.5,
            volume=2500,
            amount=203750.0,
            turnover_rate=2.1,
            amplitude=2.48,
            provider_timestamp="2026-08-14T11:35:00+08:00",
        )
        return quote, "FakeRealtime", []

    def name(self, code: str) -> str:
        return f"证券{code}"

    def benchmarks(self):
        rows = [
            {
                "code": code,
                "name": name,
                "current": 3000.0 + index,
                "change_pct": 0.5,
                "open": 2990.0,
                "high": 3010.0,
                "low": 2980.0,
                "prev_close": 2985.0,
                "volume": 100000,
                "amount": 200000000.0,
                "amplitude": 1.0,
            }
            for index, (code, name) in enumerate(BENCHMARKS.items())
        ]
        return rows, {code: "FakeBenchmark" for code in BENCHMARKS}, []


def test_current_portfolio_codes_are_stable_six_digit_primary_keys() -> None:
    assert validate_portfolio_codes(PORTFOLIO_CODES) == PORTFOLIO_CODES
    assert len(set(PORTFOLIO_CODES)) == 7
    assert {code for code in PORTFOLIO_CODES if code.startswith("159")} == {"159567", "159967"}
    assert all(code.isdigit() and len(code) == 6 for code in PORTFOLIO_CODES)


def test_metric_definitions_require_full_windows() -> None:
    frame = _history_frame(rows=61)
    metrics = calculate_metrics(frame)

    assert metrics.has_60_day_ma is True
    assert metrics.has_60_day_return is True
    assert metrics.values["MA5"] == 59.0
    assert metrics.values["MA60"] == 31.5
    assert metrics.values["return_5d"] == pytest.approx(round((61 / 56 - 1) * 100, 4))
    assert metrics.values["return_60d"] == 6000.0
    assert metrics.values["volume_ratio"] == pytest.approx(round(1060 / 1057, 4))
    assert metrics.values["volume_vs_20d_avg"] == pytest.approx(round(1060 / 1049.5, 4))

    short_metrics = calculate_metrics(frame.tail(60))
    assert short_metrics.values["MA60"] == 31.5
    assert short_metrics.values["return_60d"] is None
    assert short_metrics.has_60_day_return is False


def test_payload_has_stable_schema_for_all_seven_codes(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 11, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    with patch("scripts.portfolio_market_data._phase_data_date", return_value=date(2026, 8, 14)):
        payload = build_payload("midday", sources=_FakeSources(), now=now)

    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["market_phase"] == "midday"
    assert payload["data_date"] == "2026-08-14"
    assert [stock["code"] for stock in payload["stocks"]] == list(PORTFOLIO_CODES)
    assert len(payload["benchmarks"]) == 4
    assert payload["errors"] == []
    for stock in payload["stocks"]:
        assert set(STOCK_FIELDS).issubset(stock)
        assert stock["source"] == "FakeRealtime"
        assert stock["source_details"] == {"history": "FakeHistory", "realtime": "FakeRealtime"}
        assert stock["data_timestamp"] == "2026-08-14T11:35:00+08:00"
        assert stock["status"] == "ok"

    output = tmp_path / "midday.json"
    write_payload(payload, output)
    decoded = json.loads(output.read_text(encoding="utf-8"))
    assert decoded == payload
    assert "NaN" not in output.read_text(encoding="utf-8")


class _FailingFetcher(BaseFetcher):
    name = "FailingFreeFetcher"
    priority = 0

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise DataFetchError("primary failed")

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df


class _SuccessfulFetcher(BaseFetcher):
    name = "SuccessfulFreeFetcher"
    priority = 1

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return _history_frame(rows=65, end=end_date)

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df


def test_free_history_source_failure_falls_back_without_fake_cache() -> None:
    DataFetcherManager.reset_daily_source_health()
    sources = FreeProjectSources(fetchers=[_FailingFetcher(), _SuccessfulFetcher()])

    frame, source = sources.history("159567", end_date=date(2026, 8, 14), days=100)

    assert source == "SuccessfulFreeFetcher"
    assert len(frame) == 65
    assert frame["date"].max().date() == date(2026, 8, 14)


def test_all_source_failure_is_explicit_error_payload() -> None:
    sources = FreeProjectSources(fetchers=[_FailingFetcher()])
    now = datetime(2026, 8, 14, 11, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    with patch("scripts.portfolio_market_data._phase_data_date", return_value=date(2026, 8, 14)):
        payload = build_payload("midday", sources=sources, now=now, codes=("688825",))

    stock = payload["stocks"][0]
    assert stock["status"] == "error"
    assert stock["source"] == "unavailable"
    assert stock["data_timestamp"] == now.isoformat()
    assert payload["errors"][0]["stage"] == "history"


def test_workflow_crons_and_manual_choices_are_wired() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(encoding="utf-8")
    for cron in ("48 0 * * 1-5", "35 3 * * 1-5", "10 7 * * 1-5"):
        assert cron in workflow
    for phase in (*("premarket", "midday", "close"), "all"):
        assert f"- {phase}" in workflow
    assert "python scripts/portfolio_market_data.py" in workflow


def test_script_entrypoint_is_locally_runnable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/portfolio_market_data.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--phase" in result.stdout
