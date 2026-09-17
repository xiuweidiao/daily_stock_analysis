from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_market_data import build_payload
from scripts.portfolio_midday_reconstruction import reconstruct_midday_snapshot
from scripts.portfolio_snapshot_contract import assess_data_quality
from scripts.portfolio_snapshot_readiness import inspect_snapshot
from scripts.validate_portfolio_snapshot import validate_snapshot_contract
from tests.portfolio_snapshot_test_utils import finalize_snapshot_fixture
from tests.test_portfolio_full_reliability import RecoverySources
from tests.test_portfolio_market_data import _FakeSources
from tests.test_portfolio_snapshot_store_v2 import FakeMinuteProvider, FakeSources


SHANGHAI = ZoneInfo("Asia/Shanghai")
PORTFOLIO = PortfolioConfig(version=1, holdings=("688825",), watchlist=())
CORE = {
    "latest_price": 10.2,
    "prev_close": 10.0,
    "open": 10.1,
    "high": 10.5,
    "low": 9.9,
    "volume": 1000,
    "amount": 10200,
}
BENCHMARKS = ("sh000001", "sh000300", "sz399006", "sh000688")


def _quality_payload(
    phase: str,
    *,
    generated_at: datetime,
    trading_date: date,
    data_date: date,
) -> dict:
    payload = {
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Shanghai",
        "market_phase": phase,
        "data_date": data_date.isoformat(),
        "holdings_codes": ["688825"],
        "watchlist_codes": [],
        "stocks": [
            {
                "code": "688825",
                "tracking_type": "holding",
                "status": "ok",
                **CORE,
            }
        ],
        "benchmarks": [
            {"code": code, "status": "ok"} for code in BENCHMARKS
        ],
        "errors": [],
    }
    return finalize_snapshot_fixture(
        payload,
        phase=phase,
        portfolio=PORTFOLIO,
        trading_date=trading_date,
        data_date=data_date,
        generated_at=generated_at,
    )


def test_premarket_0848_uses_previous_close_business_time() -> None:
    now = datetime(2026, 9, 17, 8, 48, tzinfo=SHANGHAI)
    with patch(
        "scripts.portfolio_market_data._phase_data_date", return_value=date(2026, 9, 16)
    ):
        payload = build_payload(
            "premarket",
            sources=_FakeSources(),
            portfolio=PORTFOLIO,
            now=now,
            target_date=now.date(),
        )
    assert payload["generated_at"] == now.isoformat()
    assert payload["snapshot_as_of"] == "2026-09-16T15:00:00+08:00"
    assert payload["information_cutoff"] == "2026-09-17T08:50:00+08:00"


def test_premarket_0852_recovery_is_not_stale() -> None:
    now = datetime(2026, 9, 17, 8, 52, tzinfo=SHANGHAI)
    payload = build_payload(
        "premarket",
        sources=RecoverySources(date(2026, 9, 16)),
        portfolio=PORTFOLIO,
        now=now,
        target_date=now.date(),
        expected_data_date=date(2026, 9, 16),
        generation_mode="recovery",
    )
    assert payload["generated_at"] == now.isoformat()
    assert payload["snapshot_as_of"] == "2026-09-16T15:00:00+08:00"
    assert payload["information_cutoff"] == "2026-09-17T08:50:00+08:00"
    validate_snapshot_contract(
        payload,
        phase="premarket",
        portfolio=PORTFOLIO,
        now=now,
        target_date=now.date(),
        expected_data_date=date(2026, 9, 16),
        generation_mode="recovery",
    )


def test_premarket_1000_recovery_keeps_previous_close_data() -> None:
    now = datetime(2026, 9, 17, 10, 0, tzinfo=SHANGHAI)
    payload = build_payload(
        "premarket",
        sources=RecoverySources(date(2026, 9, 16)),
        portfolio=PORTFOLIO,
        now=now,
        target_date=now.date(),
        expected_data_date=date(2026, 9, 16),
        generation_mode="recovery",
    )
    assert payload["generated_at"] == now.isoformat()
    assert payload["snapshot_as_of"] == "2026-09-16T15:00:00+08:00"
    assert all(item["data_date"] == "2026-09-16" for item in payload["stocks"])


def test_midday_1140_reconstructs_exact_morning_close() -> None:
    now = datetime(2026, 9, 14, 11, 40, tzinfo=SHANGHAI)
    payload = reconstruct_midday_snapshot(
        target_date=now.date(),
        portfolio=PORTFOLIO,
        sources=FakeSources(),
        provider=FakeMinuteProvider(),
        now=now,
    )
    assert payload["generated_at"] == now.isoformat()
    assert payload["snapshot_as_of"] == "2026-09-14T11:30:00+08:00"
    assert payload["information_cutoff"] == "2026-09-14T11:30:00+08:00"
    assert payload["stocks"][0]["provider_timestamp"] == "2026-09-14T11:30:00+08:00"


def test_midday_1300_late_reconstruction_never_uses_afternoon_quote() -> None:
    now = datetime(2026, 9, 14, 13, 0, tzinfo=SHANGHAI)
    payload = reconstruct_midday_snapshot(
        target_date=now.date(),
        portfolio=PORTFOLIO,
        sources=FakeSources(),
        provider=FakeMinuteProvider(),
        now=now,
    )
    assert payload["generated_at"] == now.isoformat()
    assert payload["snapshot_as_of"] == "2026-09-14T11:30:00+08:00"
    assert all(
        item["provider_timestamp"] == "2026-09-14T11:30:00+08:00"
        for item in [*payload["stocks"], *payload["benchmarks"]]
    )


def test_midday_reconstruction_rejects_afternoon_data_with_faked_as_of() -> None:
    now = datetime(2026, 9, 14, 18, 0, tzinfo=SHANGHAI)
    payload = reconstruct_midday_snapshot(
        target_date=now.date(),
        portfolio=PORTFOLIO,
        sources=FakeSources(),
        provider=FakeMinuteProvider(),
        now=now,
    )
    payload["stocks"][0]["provider_timestamp"] = "2026-09-14T15:00:00+08:00"
    assessment = assess_data_quality(
        payload,
        phase="midday",
        portfolio=PORTFOLIO,
        trading_date=now.date(),
        expected_data_date=now.date(),
    )
    assert assessment["blocking"] is True
    assert any(
        item.get("field") == "provider_timestamp"
        and item.get("reason") == "wrong_business_time"
        for item in assessment["errors"]
    )


def test_close_1520_has_official_close_business_time() -> None:
    now = datetime(2026, 8, 14, 15, 20, tzinfo=SHANGHAI)
    payload = build_payload(
        "close",
        sources=_FakeSources(),
        portfolio=PORTFOLIO,
        now=now,
        target_date=now.date(),
        expected_data_date=now.date(),
    )
    assert payload["snapshot_as_of"] == "2026-08-14T15:00:00+08:00"
    assert payload["information_cutoff"] == "2026-08-14T15:00:00+08:00"


def test_close_1800_recovery_keeps_official_close_business_time() -> None:
    now = datetime(2026, 9, 17, 18, 0, tzinfo=SHANGHAI)
    payload = build_payload(
        "close",
        sources=RecoverySources(now.date()),
        portfolio=PORTFOLIO,
        now=now,
        target_date=now.date(),
        expected_data_date=now.date(),
        generation_mode="recovery",
    )
    assert payload["generated_at"] == now.isoformat()
    assert payload["snapshot_as_of"] == "2026-09-17T15:00:00+08:00"
    assert payload["information_cutoff"] == "2026-09-17T15:00:00+08:00"


def test_insufficient_ma60_is_partial_but_non_blocking() -> None:
    payload = _quality_payload(
        "close",
        generated_at=datetime(2026, 9, 17, 18, 0, tzinfo=SHANGHAI),
        trading_date=date(2026, 9, 17),
        data_date=date(2026, 9, 17),
    )
    payload["stocks"][0]["MA60"] = None
    payload["stocks"][0]["return_60d"] = None
    payload = finalize_snapshot_fixture(
        payload,
        phase="close",
        portfolio=PORTFOLIO,
        trading_date=date(2026, 9, 17),
        data_date=date(2026, 9, 17),
        generated_at=datetime(2026, 9, 17, 18, 0, tzinfo=SHANGHAI),
    )
    assert payload["portfolio_status"] == "partial"
    assert payload["blocking"] is False
    assert {item["field"] for item in payload["warnings"]} == {"MA60", "return_60d"}


def test_missing_latest_price_is_blocking() -> None:
    payload = _quality_payload(
        "close",
        generated_at=datetime(2026, 9, 17, 18, 0, tzinfo=SHANGHAI),
        trading_date=date(2026, 9, 17),
        data_date=date(2026, 9, 17),
    )
    payload["stocks"][0]["latest_price"] = None
    assessment = assess_data_quality(
        payload,
        phase="close",
        portfolio=PORTFOLIO,
        trading_date=date(2026, 9, 17),
        expected_data_date=date(2026, 9, 17),
    )
    assert assessment["blocking"] is True
    assert assessment["errors"][0]["reason"] == "missing_core_field"


def test_portfolio_universe_mismatch_is_blocking() -> None:
    payload = _quality_payload(
        "close",
        generated_at=datetime(2026, 9, 17, 18, 0, tzinfo=SHANGHAI),
        trading_date=date(2026, 9, 17),
        data_date=date(2026, 9, 17),
    )
    other = PortfolioConfig(version=1, holdings=("600519",), watchlist=())
    assessment = assess_data_quality(
        payload,
        phase="close",
        portfolio=other,
        trading_date=date(2026, 9, 17),
        expected_data_date=date(2026, 9, 17),
    )
    assert assessment["blocking"] is True
    assert any(item["reason"] == "portfolio_mismatch" for item in assessment["errors"])


def test_snapshot_as_of_phase_mismatch_is_blocking() -> None:
    payload = _quality_payload(
        "midday",
        generated_at=datetime(2026, 9, 17, 13, 0, tzinfo=SHANGHAI),
        trading_date=date(2026, 9, 17),
        data_date=date(2026, 9, 17),
    )
    payload["snapshot_as_of"] = "2026-09-17T15:00:00+08:00"
    assessment = assess_data_quality(
        payload,
        phase="midday",
        portfolio=PORTFOLIO,
        trading_date=date(2026, 9, 17),
        expected_data_date=date(2026, 9, 17),
    )
    assert assessment["blocking"] is True
    assert any(item["field"] == "snapshot_as_of" for item in assessment["errors"])


def test_old_trading_day_cannot_be_fresh_for_today(tmp_path) -> None:
    payload = _quality_payload(
        "close",
        generated_at=datetime(2026, 9, 16, 18, 0, tzinfo=SHANGHAI),
        trading_date=date(2026, 9, 16),
        data_date=date(2026, 9, 16),
    )
    path = tmp_path / "close.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = inspect_snapshot(
        path,
        phase="close",
        portfolio=PORTFOLIO,
        target_date=date(2026, 9, 17),
        expected_data_date=date(2026, 9, 17),
        generation_mode="recovery",
        now=datetime(2026, 9, 17, 18, 0, tzinfo=SHANGHAI),
        target_is_trading_day=True,
    )
    assert result.fresh is False
    assert result.reason == "stale_snapshot"
