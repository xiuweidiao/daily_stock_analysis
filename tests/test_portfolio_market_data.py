from __future__ import annotations

import json
import re
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
from scripts.portfolio_config import (
    DEFAULT_CONFIG_PATH,
    PortfolioConfig,
    load_portfolio_config,
)
from scripts.portfolio_market_data import (
    BENCHMARKS,
    DIAGNOSTICS_OUTPUT_DIR,
    OFFICIAL_OUTPUT_DIR,
    STOCK_FIELDS,
    DiagnosticOutputError,
    FreeProjectSources,
    PhaseTimeError,
    build_payload,
    calculate_metrics,
    generate_snapshots,
    main as portfolio_main,
    resolve_output_dir,
    validate_portfolio_codes,
    write_payload,
)


EXPECTED_HOLDINGS = (
    "688825",
    "300442",
    "688012",
    "300604",
    "300274",
    "159567",
    "159967",
)


def _portfolio(
    holdings: tuple[str, ...] = EXPECTED_HOLDINGS,
    watchlist: tuple[str, ...] = (),
) -> PortfolioConfig:
    return PortfolioConfig(version=1, holdings=holdings, watchlist=watchlist)


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
        assert code.isdigit() and len(code) == 6
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
            volume_ratio=1.8,
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
    config = load_portfolio_config(DEFAULT_CONFIG_PATH)

    assert config.holdings == EXPECTED_HOLDINGS
    assert config.watchlist == ()
    assert validate_portfolio_codes(config.holdings) == EXPECTED_HOLDINGS
    assert {code for code in config.holdings if code.startswith("159")} == {"159567", "159967"}


def test_metric_definitions_require_full_windows() -> None:
    frame = _history_frame(rows=61)
    metrics = calculate_metrics(frame)

    assert metrics.has_60_day_ma is True
    assert metrics.has_60_day_return is True
    assert metrics.values["MA5"] == 59.0
    assert metrics.values["MA60"] == 31.5
    assert metrics.values["return_5d"] == pytest.approx(round((61 / 56 - 1) * 100, 4))
    assert metrics.values["return_60d"] == 6000.0
    assert metrics.values["volume_ratio"] is None
    assert metrics.values["volume_vs_5d_avg"] == pytest.approx(round(1060 / 1057, 4))
    assert metrics.values["volume_vs_20d_avg"] == pytest.approx(round(1060 / 1049.5, 4))

    short_metrics = calculate_metrics(frame.tail(60))
    assert short_metrics.values["MA60"] == 31.5
    assert short_metrics.values["return_60d"] is None
    assert short_metrics.has_60_day_return is False


def test_payload_has_stable_schema_for_all_seven_codes(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 11, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    with patch("scripts.portfolio_market_data._phase_data_date", return_value=date(2026, 8, 14)):
        payload = build_payload(
            "midday", sources=_FakeSources(), portfolio=_portfolio(), now=now
        )

    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["market_phase"] == "midday"
    assert payload["data_date"] == "2026-08-14"
    assert payload["holdings_codes"] == list(EXPECTED_HOLDINGS)
    assert payload["watchlist_codes"] == []
    assert payload["portfolio_status"] == "ok"
    assert [stock["code"] for stock in payload["stocks"]] == list(EXPECTED_HOLDINGS)
    assert len(payload["benchmarks"]) == 4
    assert payload["errors"] == []
    for stock in payload["stocks"]:
        assert set(STOCK_FIELDS).issubset(stock)
        assert stock["tracking_type"] == "holding"
        assert stock["source"] == "FakeRealtime"
        assert stock["source_details"] == {
            "history": "FakeHistory",
            "realtime": "FakeRealtime",
            "volume_ratio": "FakeRealtime",
        }
        assert stock["volume_ratio"] == 1.8
        assert stock["fetched_at"] == now.isoformat()
        assert stock["provider_timestamp"] == "2026-08-14T11:35:00+08:00"
        assert stock["data_timestamp"] == "2026-08-14T11:35:00+08:00"
        assert stock["freshness_status"] == "fresh"
        assert stock["status"] == "ok"
    for benchmark in payload["benchmarks"]:
        assert benchmark["fetched_at"] == now.isoformat()
        assert benchmark["provider_timestamp"] is None
        assert benchmark["data_timestamp"] is None
        assert benchmark["freshness_status"] == "unknown"

    output = tmp_path / "midday.json"
    write_payload(payload, output)
    decoded = json.loads(output.read_text(encoding="utf-8"))
    assert decoded == payload
    assert "NaN" not in output.read_text(encoding="utf-8")


def test_watchlist_is_fetched_and_marked_separately() -> None:
    now = datetime(2026, 8, 14, 11, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = build_payload(
        "midday",
        sources=_FakeSources(),
        portfolio=_portfolio(holdings=("688012",), watchlist=("600519",)),
        now=now,
    )

    assert payload["holdings_codes"] == ["688012"]
    assert payload["watchlist_codes"] == ["600519"]
    assert [(item["code"], item["tracking_type"]) for item in payload["stocks"]] == [
        ("688012", "holding"),
        ("600519", "watchlist"),
    ]


def test_empty_portfolio_still_generates_benchmarks(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime(2026, 8, 14, 11, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = build_payload(
        "midday", sources=_FakeSources(), portfolio=_portfolio(holdings=()), now=now
    )

    assert payload["portfolio_status"] == "empty"
    assert payload["stocks"] == []
    assert len(payload["benchmarks"]) == 4
    assert "benchmarks only" in caplog.text


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
        payload = build_payload(
            "midday",
            sources=sources,
            portfolio=_portfolio(holdings=("688825",)),
            now=now,
        )

    stock = payload["stocks"][0]
    assert stock["status"] == "error"
    assert stock["source"] == "unavailable"
    assert stock["fetched_at"] == now.isoformat()
    assert stock["provider_timestamp"] is None
    assert stock["data_timestamp"] is None
    assert stock["freshness_status"] == "unknown"
    assert payload["errors"][0]["stage"] == "history"


def test_missing_provider_timestamp_never_uses_generated_at_as_market_time() -> None:
    class _NoProviderTimestampSources(_FakeSources):
        def quote(self, code: str):
            quote, source, errors = super().quote(code)
            quote.provider_timestamp = None
            return quote, source, errors

    now = datetime(2026, 8, 14, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = build_payload(
        "intraday",
        sources=_NoProviderTimestampSources(),
        portfolio=_portfolio(holdings=("159567",)),
        now=now,
    )

    stock = payload["stocks"][0]
    assert stock["fetched_at"] == payload["generated_at"] == now.isoformat()
    assert stock["provider_timestamp"] is None
    assert stock["data_timestamp"] is None
    assert stock["freshness_status"] == "unknown"


def test_native_volume_ratio_reconciles_etf_hand_and_share_units() -> None:
    class _EtfHandVolumeSources(_FakeSources):
        def quote(self, code: str):
            quote, source, errors = super().quote(code)
            quote.volume = 10
            quote.volume_ratio = 1.0
            return quote, source, errors

    now = datetime(2026, 8, 14, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = build_payload(
        "intraday",
        sources=_EtfHandVolumeSources(),
        portfolio=_portfolio(holdings=("159567",)),
        now=now,
    )

    stock = payload["stocks"][0]
    assert stock["volume_ratio"] == 1.0
    assert stock["volume"] == 1000
    assert stock["volume_vs_5d_avg"] == pytest.approx(round(1000 / 1077, 4))


def test_688825_volume_units_are_normalized_before_relative_volume_metrics() -> None:
    class _ChangxinMixedVolumeSources(_FakeSources):
        def history(self, code: str, *, end_date: date, days: int):
            assert code == "688825"
            frame = _history_frame(rows=61, end=end_date.isoformat())
            share_volume = frame["volume"].copy()
            frame["volume"] = share_volume / 100
            frame["amount"] = share_volume * frame["close"]
            return frame, "EfinanceFetcher"

        def quote(self, code: str):
            quote, _source, errors = super().quote(code)
            quote.volume = 1100
            quote.amount = quote.volume * quote.price
            quote.volume_ratio = None
            return quote, "akshare_sina", errors

    now = datetime(2026, 8, 14, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = build_payload(
        "close",
        sources=_ChangxinMixedVolumeSources(),
        portfolio=_portfolio(holdings=("688825",)),
        now=now,
    )

    stock = payload["stocks"][0]
    assert stock["volume"] == 1100
    assert stock["volume_ratio"] is None
    assert stock["volume_vs_5d_avg"] == pytest.approx(round(1100 / 1057, 4))
    assert stock["volume_vs_20d_avg"] == pytest.approx(round(1100 / 1049.5, 4))
    assert stock["volume_vs_5d_avg"] < 2


@pytest.mark.parametrize(
    ("phase", "hour", "minute", "allowed"),
    (
        ("premarket", 6, 37, True),
        ("premarket", 8, 20, True),
        ("premarket", 8, 48, True),
        ("premarket", 8, 50, True),
        ("premarket", 8, 51, False),
        ("premarket", 9, 0, False),
        ("midday", 11, 29, False),
        ("midday", 11, 35, True),
        ("midday", 13, 0, False),
        ("close", 14, 0, False),
        ("close", 15, 0, True),
        ("close", 15, 10, True),
        ("close", 18, 0, False),
        ("intraday", 10, 15, True),
        ("intraday", 12, 0, False),
    ),
)
def test_official_phase_windows_do_not_overwrite_outside_window(
    tmp_path: Path, phase: str, hour: int, minute: int, allowed: bool
) -> None:
    now = datetime(2026, 8, 14, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
    output = tmp_path / f"{phase}.json"
    if not allowed:
        output.write_text('{"existing": true}\n', encoding="utf-8")
        with pytest.raises(PhaseTimeError):
            generate_snapshots(
                phase,
                sources=_FakeSources(),
                portfolio=_portfolio(),
                now=now,
                output_dir=tmp_path,
            )
        assert output.read_text(encoding="utf-8") == '{"existing": true}\n'
        return

    written = generate_snapshots(
        phase,
        sources=_FakeSources(),
        portfolio=_portfolio(),
        now=now,
        output_dir=tmp_path,
    )
    assert written == [output]
    assert json.loads(output.read_text(encoding="utf-8"))["market_phase"] == phase


def test_close_freshness_requires_provider_time_at_or_after_1500() -> None:
    class _EarlyProviderTimestampSources(_FakeSources):
        def quote(self, code: str):
            quote, source, errors = super().quote(code)
            quote.provider_timestamp = "2026-08-14T14:59:00+08:00"
            return quote, source, errors

    now = datetime(2026, 8, 14, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = build_payload(
        "close",
        sources=_EarlyProviderTimestampSources(),
        portfolio=_portfolio(holdings=("300442",)),
        now=now,
    )

    stock = payload["stocks"][0]
    assert stock["provider_timestamp"] == "2026-08-14T14:59:00+08:00"
    assert stock["data_timestamp"] == stock["provider_timestamp"]
    assert stock["freshness_status"] == "stale"


def test_premarket_snapshot_uses_configured_pool_and_completed_data_date(
    tmp_path: Path,
) -> None:
    portfolio = load_portfolio_config(DEFAULT_CONFIG_PATH)
    now = datetime(2026, 8, 17, 6, 37, tzinfo=ZoneInfo("Asia/Shanghai"))
    output_dir = tmp_path / "data" / "portfolio"

    with patch(
        "scripts.portfolio_market_data._phase_data_date",
        return_value=date(2026, 8, 14),
    ):
        written = generate_snapshots(
            "premarket",
            sources=_FakeSources(),
            portfolio=portfolio,
            now=now,
            output_dir=output_dir,
        )

    output = output_dir / "premarket.json"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert written == [output]
    assert payload["generated_at"] == now.isoformat()
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["market_phase"] == "premarket"
    assert payload["data_date"] == "2026-08-14"
    assert payload["holdings_codes"] == list(portfolio.holdings)
    assert payload["watchlist_codes"] == list(portfolio.watchlist)
    assert [item["code"] for item in payload["stocks"]] == list(
        portfolio.holdings + portfolio.watchlist
    )
    assert all(item["source"] == "FakeHistory" for item in payload["stocks"])
    assert all(item["latest_price"] == 81.0 for item in payload["stocks"])
    assert all(
        item["source_details"]["realtime"] is None for item in payload["stocks"]
    )
    assert len(payload["benchmarks"]) == 4


def test_cli_non_trading_day_does_not_write_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "portfolio_market_data.py",
            "--phase",
            "premarket",
            "--output-dir",
            str(tmp_path),
        ],
    )
    with (
        patch("scripts.portfolio_market_data.is_market_open", return_value=False),
        patch("scripts.portfolio_market_data.FreeProjectSources") as sources_class,
    ):
        assert portfolio_main() == 0

    sources_class.assert_not_called()
    assert list(tmp_path.glob("*.json")) == []


def test_all_is_diagnostics_only_and_writes_nothing_by_default(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    with pytest.raises(PhaseTimeError, match="all is diagnostics/tests only"):
        generate_snapshots(
            "all",
            sources=_FakeSources(),
            portfolio=_portfolio(),
            now=now,
            output_dir=tmp_path,
        )

    assert list(tmp_path.glob("*.json")) == []


def test_all_with_override_can_write_three_diagnostic_snapshots(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    written = generate_snapshots(
        "all",
        sources=_FakeSources(),
        portfolio=_portfolio(),
        now=now,
        output_dir=tmp_path,
        allow_phase_time_override=True,
    )

    assert {path.name for path in written} == {
        "premarket.json",
        "midday.json",
        "close.json",
    }


def test_override_defaults_to_diagnostics_and_cannot_target_official_directory() -> None:
    assert resolve_output_dir(None, diagnostics_run=True) == DIAGNOSTICS_OUTPUT_DIR
    assert resolve_output_dir(None, diagnostics_run=False) == OFFICIAL_OUTPUT_DIR
    with pytest.raises(DiagnosticOutputError, match="cannot write to the official"):
        resolve_output_dir(OFFICIAL_OUTPUT_DIR, diagnostics_run=True)


def test_generate_override_rejects_official_directory_before_writing() -> None:
    now = datetime(2026, 8, 14, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(DiagnosticOutputError, match="cannot write to the official"):
        generate_snapshots(
            "all",
            sources=_FakeSources(),
            portfolio=_portfolio(),
            now=now,
            output_dir=OFFICIAL_OUTPUT_DIR,
            allow_phase_time_override=True,
        )


def test_short_history_for_688825_remains_partial_without_fabrication() -> None:
    class _ShortHistorySources(_FakeSources):
        def history(self, code: str, *, end_date: date, days: int):
            assert code == "688825"
            return _history_frame(rows=15, end=end_date.isoformat()), "FakeHistory"

    now = datetime(2026, 8, 14, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    payload = build_payload(
        "intraday",
        sources=_ShortHistorySources(),
        portfolio=_portfolio(holdings=("688825",)),
        now=now,
    )

    stock = payload["stocks"][0]
    assert stock["status"] == "partial"
    assert stock["MA20"] is None
    assert stock["MA60"] is None
    assert stock["return_20d"] is None
    assert stock["return_60d"] is None
    assert "insufficient bars" in stock["status_detail"]


def test_workflow_scheduled_crons_match_resolve_phase_mapping() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(encoding="utf-8")
    expected_mapping = [
        ("37 22 * * 0-4", "premarket"),
        ("53 2 * * 1-5", "midday"),
        ("23 6 * * 1-5", "close"),
        ("43 7 * * 1-5", "close"),
        ("43 8 * * 1-5", "close"),
        ("3 9 * * 1-5", "close"),
    ]
    scheduled_crons = re.findall(
        r"^\s*- cron: '([^']+)'$", workflow, flags=re.MULTILINE
    )
    resolve_mapping = re.findall(
        r'elif \[ "\$SCHEDULE_CRON" = "([^"]+)" \]; then\n\s+phase="([^"]+)"',
        workflow,
    )

    assert scheduled_crons == [cron for cron, _phase in expected_mapping]
    assert resolve_mapping == expected_mapping


def test_premarket_cron_maps_utc_sunday_to_shanghai_monday() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(encoding="utf-8")
    assert "37 22 * * 0-4" in workflow

    utc_trigger = datetime(2026, 8, 16, 22, 37, tzinfo=ZoneInfo("UTC"))
    shanghai_trigger = utc_trigger.astimezone(ZoneInfo("Asia/Shanghai"))

    assert utc_trigger.strftime("%A") == "Sunday"
    assert shanghai_trigger == datetime(
        2026, 8, 17, 6, 37, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert shanghai_trigger.strftime("%A") == "Monday"


def test_workflow_manual_choices_are_wired() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(encoding="utf-8")
    for phase in ("premarket", "midday", "close", "intraday"):
        assert f"- {phase}" in workflow
    assert "- all" not in workflow
    assert "force_non_trading_day" not in workflow
    assert "python scripts/portfolio_market_data.py" in workflow


def test_portfolio_dependency_smoke_uses_only_lightweight_python311_requirements() -> None:
    requirements = Path(
        ".github/requirements-portfolio-pipeline.txt"
    ).read_text(encoding="utf-8")
    workflow = Path(
        ".github/workflows/portfolio-market-data-smoke.yml"
    ).read_text(encoding="utf-8")

    assert "pydantic>=2.0.0,<3.0.0" in requirements
    assert "python-version: '3.11'" in workflow
    assert (
        "python -m pip install -r .github/requirements-portfolio-pipeline.txt"
        in workflow
    )
    assert "python -m pip check" in workflow
    assert "python scripts/portfolio_market_data.py --help" in workflow
    assert "python scripts/portfolio_close_readiness.py --help" in workflow
    assert "python scripts/validate_portfolio_snapshot.py --help" in workflow
    assert "requirements.txt" not in workflow.replace(
        ".github/requirements-portfolio-pipeline.txt", ""
    )


def test_script_entrypoint_is_locally_runnable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/portfolio_market_data.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--phase" in result.stdout


def test_script_entrypoint_rejects_all_without_override(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/portfolio_market_data.py",
            "--phase",
            "all",
            "--output-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "all is diagnostics/tests only" in result.stderr
    assert list(tmp_path.glob("*.json")) == []
