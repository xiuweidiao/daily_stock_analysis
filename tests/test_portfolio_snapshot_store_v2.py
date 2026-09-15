from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_intraday_history import (
    IntradayHistoryError,
    IntradayHistoryResult,
    exact_morning_session,
    sina_symbol,
)
from scripts.portfolio_midday_reconstruction import reconstruct_midday_snapshot
from scripts.portfolio_snapshot_repair import SnapshotRepair
from scripts.portfolio_schedule_context import build_schedule_context
from scripts.portfolio_snapshot_store import (
    SnapshotStoreError,
    build_universe,
    persist_snapshot,
    record_phase_issue,
    resolve_historical_universe,
)
from scripts.validate_portfolio_snapshot import (
    SnapshotContractError,
    validate_snapshot_contract,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
PORTFOLIO = PortfolioConfig(version=1, holdings=("688825", "159567"), watchlist=())
BENCHMARKS = ("sh000001", "sh000300", "sz399006", "sh000688")


def _history(target: date, rows: int = 81) -> pd.DataFrame:
    dates = pd.bdate_range(end=target, periods=rows)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [10.0 + index / 100 for index in range(rows)],
            "high": [10.4 + index / 100 for index in range(rows)],
            "low": [9.8 + index / 100 for index in range(rows)],
            "close": [10.2 + index / 100 for index in range(rows)],
            "volume": [10000 + index for index in range(rows)],
            "amount": [(10000 + index) * (10.2 + index / 100) for index in range(rows)],
        }
    )


class FakeSources:
    def history(self, code: str, *, end_date: date, days: int):
        return _history(end_date), "fake_daily"

    def benchmark_history(self, code: str, *, end_date: date, days: int):
        return _history(end_date, max(days, 5)), "fake_index_daily"

    def name(self, code: str) -> str:
        return f"name-{code}"

    def quote(self, code: str):
        return None, None, []


class FakeMinuteProvider:
    def __init__(self, *, include_amount: bool = True, fail: bool = False) -> None:
        self.include_amount = include_amount
        self.fail = fail

    def get_intraday_history(self, symbol, *, security_type, trading_date, interval="1m"):
        if self.fail:
            raise IntradayHistoryError("Sina unavailable")
        frame = pd.DataFrame(
            {
                "timestamp": [
                    datetime.combine(trading_date, datetime.min.time(), SHANGHAI).replace(hour=9, minute=31),
                    datetime.combine(trading_date, datetime.min.time(), SHANGHAI).replace(hour=11, minute=30),
                ],
                "open": [11.0, 11.3],
                "high": [11.4, 11.8],
                "low": [10.9, 11.2],
                "close": [11.2, 11.6],
                "volume": [1000, 2000],
            }
        )
        if self.include_amount:
            frame["amount"] = [11200.0, 23200.0]
        return IntradayHistoryResult(
            frame=frame,
            provider="akshare",
            route="fake_sina",
            requested_date=trading_date.isoformat(),
            actual_start=frame.iloc[0]["timestamp"].isoformat(),
            actual_end=frame.iloc[-1]["timestamp"].isoformat(),
            interval="1m",
            timestamp_semantics="bar_end",
        )


def _snapshot(phase: str, trading_date: date, as_of: datetime) -> dict:
    core = {
        "latest_price": 10.2,
        "change_pct": 1.0,
        "open": 10.0,
        "high": 10.5,
        "low": 9.9,
        "prev_close": 10.1,
        "volume": 1000,
        "amount": 10200,
        "status": "ok",
    }
    stocks = [
        {"code": code, "tracking_type": "holding", **core}
        for code in PORTFOLIO.holdings
    ]
    return {
        "schema_version": "2.0",
        "trading_date": trading_date.isoformat(),
        "market_phase": phase,
        "snapshot_as_of": as_of.isoformat(),
        "generated_at": as_of.isoformat(),
        "timezone": "Asia/Shanghai",
        "generation_mode": "recovery",
        "data_date": trading_date.isoformat(),
        "status": "ok",
        "completeness": "full",
        "portfolio_status": "ok",
        "holdings_codes": list(PORTFOLIO.holdings),
        "watchlist_codes": [],
        "stocks": stocks,
        "benchmarks": [{"code": code, "status": "ok"} for code in BENCHMARKS],
        "errors": [],
        "provenance": {},
    }


def test_universe_authoritative_and_inferred_confidence(tmp_path: Path) -> None:
    now = datetime(2026, 9, 14, 18, 37, tzinfo=SHANGHAI)
    authoritative = build_universe(PORTFOLIO, date(2026, 9, 14), captured_at=now)
    inferred = build_universe(
        PORTFOLIO,
        date(2026, 9, 12),
        captured_at=now,
        source_type="inferred_snapshot",
        confidence="inferred",
    )
    assert (authoritative["source_type"], authoritative["confidence"]) == (
        "frozen",
        "authoritative",
    )
    assert (inferred["source_type"], inferred["confidence"]) == (
        "inferred_snapshot",
        "inferred",
    )


def test_historical_universe_is_inferred_from_snapshot_not_current_config(tmp_path: Path) -> None:
    target = date(2026, 9, 12)
    day = tmp_path / "snapshots" / target.isoformat()
    day.mkdir(parents=True)
    payload = _snapshot(
        "close", target, datetime(2026, 9, 12, 15, 0, tzinfo=SHANGHAI)
    )
    (day / "close.json").write_text(json.dumps(payload), encoding="utf-8")
    universe = resolve_historical_universe(
        tmp_path,
        target,
        repo_root=tmp_path,
        captured_at=datetime(2026, 9, 15, 6, 7, tzinfo=SHANGHAI),
    )
    assert universe["holdings_codes"] == list(PORTFOLIO.holdings)
    assert universe["source_type"] == "inferred_snapshot"
    assert universe["confidence"] == "inferred"


def test_historical_universe_can_be_inferred_from_git_with_explicit_confidence(
    tmp_path: Path,
) -> None:
    calls = []

    def fake_git(command, **kwargs):
        calls.append(command)
        stdout = (
            "abc123\n"
            if command[1] == "rev-list"
            else json.dumps({"version": 1, "holdings": ["688825"], "watchlist": ["159567"]})
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    universe = resolve_historical_universe(
        tmp_path,
        date(2026, 9, 12),
        repo_root=tmp_path,
        captured_at=datetime(2026, 9, 15, 6, 7, tzinfo=SHANGHAI),
        git_runner=fake_git,
    )

    assert len(calls) == 2
    assert universe["source_type"] == "inferred_git"
    assert universe["confidence"] == "inferred"
    assert universe["source_commit"] == "abc123"


def test_repair_older_date_does_not_regress_latest(tmp_path: Path) -> None:
    newer_date = date(2026, 9, 20)
    older_date = date(2026, 9, 12)
    newer = _snapshot(
        "midday", newer_date, datetime(2026, 9, 20, 11, 30, tzinfo=SHANGHAI)
    )
    older = _snapshot(
        "midday", older_date, datetime(2026, 9, 12, 11, 30, tzinfo=SHANGHAI)
    )
    now = datetime(2026, 9, 20, 18, 37, tzinfo=SHANGHAI)
    persist_snapshot(tmp_path, newer, portfolio=PORTFOLIO, now=now)
    result = persist_snapshot(tmp_path, older, portfolio=PORTFOLIO, now=now)
    latest = json.loads((tmp_path / "latest" / "midday.json").read_text())
    compatibility = json.loads((tmp_path / "midday.json").read_text())
    assert result.compatibility_updated is False
    assert latest["trading_date"] == compatibility["trading_date"] == "2026-09-20"


def test_normal_generation_cannot_overwrite_canonical_history(tmp_path: Path) -> None:
    target = date(2026, 9, 14)
    original = _snapshot(
        "close", target, datetime(2026, 9, 14, 15, 0, tzinfo=SHANGHAI)
    )
    changed = json.loads(json.dumps(original))
    changed["generated_at"] = "2026-09-14T18:37:00+08:00"
    now = datetime(2026, 9, 14, 18, 37, tzinfo=SHANGHAI)
    persist_snapshot(tmp_path, original, portfolio=PORTFOLIO, now=now)
    with pytest.raises(SnapshotStoreError, match="canonical conflict"):
        persist_snapshot(tmp_path, changed, portfolio=PORTFOLIO, now=now)


def test_manifest_records_missed_without_fake_snapshot(tmp_path: Path) -> None:
    target = date(2026, 9, 14)
    record_phase_issue(
        tmp_path,
        target,
        "midday",
        lifecycle_status="missed",
        reason="RECONSTRUCTION_UNAVAILABLE",
        now=datetime(2026, 9, 14, 17, 47, tzinfo=SHANGHAI),
    )
    manifest = json.loads(
        (tmp_path / "snapshots" / target.isoformat() / "manifest.json").read_text()
    )
    health = json.loads((tmp_path / "pipeline_health.json").read_text())
    assert manifest["phases"]["midday"]["lifecycle_status"] == "missed"
    assert not (tmp_path / "snapshots" / target.isoformat() / "midday.json").exists()
    assert health["status"] == "degraded"


def test_sina_symbol_supports_star_etf_and_index() -> None:
    assert sina_symbol("688825", "stock") == "sh688825"
    assert sina_symbol("159567", "etf") == "sz159567"
    assert sina_symbol("sh000688", "index") == "sh000688"


def test_exact_morning_session_rejects_1129_as_1130() -> None:
    target = date(2026, 9, 14)
    result = FakeMinuteProvider().get_intraday_history(
        "688825", security_type="stock", trading_date=target
    )
    result.frame.loc[result.frame.index[-1], "timestamp"] = datetime(
        2026, 9, 14, 11, 29, tzinfo=SHANGHAI
    )
    with pytest.raises(IntradayHistoryError, match="exact 11:30"):
        exact_morning_session(result, target)


def test_production_delay_reconstructs_true_1130_snapshot() -> None:
    target = date(2026, 9, 14)
    actual = datetime(2026, 9, 14, 17, 47, 43, tzinfo=SHANGHAI)
    payload = reconstruct_midday_snapshot(
        target_date=target,
        portfolio=PORTFOLIO,
        sources=FakeSources(),
        provider=FakeMinuteProvider(),
        now=actual,
    )
    assert payload["snapshot_as_of"] == "2026-09-14T11:30:00+08:00"
    assert payload["generated_at"] == "2026-09-14T17:47:43+08:00"
    assert payload["generation_mode"] == "reconstructed"
    assert payload["status"] == "ok"
    assert payload["completeness"] == "partial"
    assert payload["stocks"][0]["volume_ratio"] is None
    assert payload["stocks"][0]["turnover_rate"] is None
    assert payload["stocks"][0]["amount"] == 34400.0
    assert {item["code"] for item in payload["stocks"]} == {"688825", "159567"}
    assert len(payload["benchmarks"]) == 4
    assert all(
        item["provider_timestamp"] == "2026-09-14T11:30:00+08:00"
        for item in [*payload["stocks"], *payload["benchmarks"]]
    )


def test_real_337_minute_schedule_delay_routes_to_reconstruction() -> None:
    context = build_schedule_context(
        phase="midday",
        schedule="10 4 * * 1-5",
        current=datetime(2026, 9, 14, 17, 47, 43, tzinfo=SHANGHAI),
    )
    assert context.expected_slot == "2026-09-14T12:10:00+08:00"
    assert context.lateness_minutes == 337
    assert context.can_generate is True
    assert context.reason == "RECONSTRUCTION_REQUIRED"
    assert context.generation_mode == "reconstructed"


def test_midday_1152_wakeup_uses_exact_history_not_realtime_quote() -> None:
    context = build_schedule_context(
        phase="midday",
        schedule="35 3 * * 1-5",
        current=datetime(2026, 9, 14, 11, 52, tzinfo=SHANGHAI),
    )
    assert context.can_generate is True
    assert context.reason == "RECONSTRUCTION_REQUIRED"
    assert context.generation_mode == "reconstructed"


def test_missing_core_amount_makes_reconstructed_snapshot_partial() -> None:
    payload = reconstruct_midday_snapshot(
        target_date=date(2026, 9, 14),
        portfolio=PORTFOLIO,
        sources=FakeSources(),
        provider=FakeMinuteProvider(include_amount=False),
        now=datetime(2026, 9, 14, 17, 47, tzinfo=SHANGHAI),
    )
    assert payload["status"] == "partial"
    assert payload["completeness"] == "partial"


def test_reconstructed_contract_requires_real_1130_as_of() -> None:
    target = date(2026, 9, 14)
    now = datetime(2026, 9, 14, 17, 47, 43, tzinfo=SHANGHAI)
    payload = reconstruct_midday_snapshot(
        target_date=target,
        portfolio=PORTFOLIO,
        sources=FakeSources(),
        provider=FakeMinuteProvider(),
        now=now,
    )
    validate_snapshot_contract(
        payload,
        phase="midday",
        portfolio=PORTFOLIO,
        now=now,
        target_date=target,
        expected_data_date=target,
        generation_mode="reconstructed",
    )
    payload["snapshot_as_of"] = "2026-09-14T17:47:43+08:00"
    with pytest.raises(SnapshotContractError, match="exactly 11:30"):
        validate_snapshot_contract(
            payload,
            phase="midday",
            portfolio=PORTFOLIO,
            now=now,
            target_date=target,
            expected_data_date=target,
            generation_mode="reconstructed",
        )


def test_repair_unavailable_minute_history_is_degraded_success_state(tmp_path: Path) -> None:
    target = date(2026, 9, 14)
    universe = build_universe(
        PORTFOLIO,
        target,
        captured_at=datetime(2026, 9, 14, 18, 37, tzinfo=SHANGHAI),
    )
    (tmp_path / "universe").mkdir()
    (tmp_path / "universe" / f"{target}.json").write_text(json.dumps(universe))
    repair = SnapshotRepair(
        root=tmp_path,
        repo_root=tmp_path,
        sources=FakeSources(),
        intraday_provider=FakeMinuteProvider(fail=True),
    )
    result = repair.repair_phase(
        target,
        "midday",
        universe=universe,
        now=datetime(2026, 9, 14, 18, 37, tzinfo=SHANGHAI),
    )
    assert result.state == "SNAPSHOT_MISSED"
    health = json.loads((tmp_path / "pipeline_health.json").read_text())
    assert health["status"] == "degraded"


def test_repair_is_idempotent_after_canonical_snapshot_exists(tmp_path: Path) -> None:
    target = date(2026, 9, 14)
    now = datetime(2026, 9, 14, 18, 37, tzinfo=SHANGHAI)
    universe = build_universe(PORTFOLIO, target, captured_at=now)
    (tmp_path / "universe").mkdir()
    (tmp_path / "universe" / f"{target}.json").write_text(json.dumps(universe))
    repair = SnapshotRepair(
        root=tmp_path,
        repo_root=tmp_path,
        sources=FakeSources(),
        intraday_provider=FakeMinuteProvider(),
    )
    first = repair.repair_phase(target, "midday", universe=universe, now=now)
    manifest = json.loads(
        (tmp_path / "snapshots" / str(target) / "manifest.json").read_text()
    )
    health = json.loads((tmp_path / "pipeline_health.json").read_text())
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    }
    second = repair.repair_phase(target, "midday", universe=universe, now=now + timedelta(minutes=1))
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    }
    assert first.state == "SNAPSHOT_RECONSTRUCTED"
    assert manifest["phases"]["midday"]["snapshot_status"] == "ok"
    assert manifest["phases"]["midday"]["completeness"] == "partial"
    assert health["status"] == "degraded"
    assert second.state == "SNAPSHOT_ALREADY_FRESH"
    assert after == before


def test_repair_rebuilds_missing_manifest_without_changing_canonical(tmp_path: Path) -> None:
    target = date(2026, 9, 14)
    now = datetime(2026, 9, 14, 18, 37, tzinfo=SHANGHAI)
    universe = build_universe(PORTFOLIO, target, captured_at=now)
    (tmp_path / "universe").mkdir()
    (tmp_path / "universe" / f"{target}.json").write_text(json.dumps(universe))
    repair = SnapshotRepair(
        root=tmp_path,
        repo_root=tmp_path,
        sources=FakeSources(),
        intraday_provider=FakeMinuteProvider(),
    )
    first = repair.repair_phase(target, "midday", universe=universe, now=now)
    canonical = Path(first.path).read_bytes()
    (Path(first.path).parent / "manifest.json").unlink()

    second = repair.repair_phase(
        target, "midday", universe=universe, now=now + timedelta(minutes=1)
    )

    assert second.state == "SNAPSHOT_ALREADY_FRESH"
    assert Path(first.path).read_bytes() == canonical
    manifest = json.loads((Path(first.path).parent / "manifest.json").read_text())
    assert manifest["phases"]["midday"]["lifecycle_status"] == "available"


@pytest.mark.parametrize(
    "repair_time",
    (
        datetime(2026, 9, 14, 18, 37, tzinfo=SHANGHAI),
        datetime(2026, 9, 15, 6, 7, tzinfo=SHANGHAI),
        datetime(2026, 9, 16, 18, 37, tzinfo=SHANGHAI),
    ),
)
def test_midday_repair_succeeds_same_next_or_two_days_later(
    tmp_path: Path, repair_time: datetime
) -> None:
    target = date(2026, 9, 14)
    universe = build_universe(
        PORTFOLIO,
        target,
        captured_at=repair_time,
        source_type="inferred_git" if repair_time.date() > target else "frozen",
        confidence="inferred" if repair_time.date() > target else "authoritative",
    )
    (tmp_path / "universe").mkdir()
    (tmp_path / "universe" / f"{target}.json").write_text(json.dumps(universe))
    repair = SnapshotRepair(
        root=tmp_path,
        repo_root=tmp_path,
        sources=FakeSources(),
        intraday_provider=FakeMinuteProvider(),
    )
    result = repair.repair_phase(
        target, "midday", universe=universe, now=repair_time
    )
    assert result.state == "SNAPSHOT_RECONSTRUCTED"
    snapshot = json.loads(Path(result.path).read_text())
    assert snapshot["snapshot_as_of"] == "2026-09-14T11:30:00+08:00"
    assert snapshot["generated_at"] == repair_time.isoformat()


def test_close_error_state_can_later_recover_from_daily_history(tmp_path: Path) -> None:
    target = date(2026, 9, 14)
    now = datetime(2026, 9, 15, 6, 7, tzinfo=SHANGHAI)
    universe = build_universe(
        PORTFOLIO,
        target,
        captured_at=now,
        source_type="inferred_git",
        confidence="inferred",
    )
    (tmp_path / "universe").mkdir()
    (tmp_path / "universe" / f"{target}.json").write_text(json.dumps(universe))
    record_phase_issue(
        tmp_path,
        target,
        "close",
        lifecycle_status="error",
        reason="all realtime providers failed",
        now=datetime(2026, 9, 14, 18, 0, tzinfo=SHANGHAI),
    )
    repair = SnapshotRepair(
        root=tmp_path,
        repo_root=tmp_path,
        sources=FakeSources(),
        intraday_provider=FakeMinuteProvider(),
    )
    result = repair.repair_phase(target, "close", universe=universe, now=now)
    assert result.state == "SNAPSHOT_GENERATED"
    assert result.generation_mode == "recovery"
    snapshot = json.loads(Path(result.path).read_text())
    assert snapshot["data_date"] == "2026-09-14"
    assert snapshot["generated_at"] == now.isoformat()


def test_all_snapshot_writers_share_one_concurrency_group() -> None:
    expected = "portfolio-snapshot-writer-${{ github.ref }}"
    for path in (
        Path(".github/workflows/portfolio-market-data.yml"),
        Path(".github/workflows/portfolio-close-watchdog.yml"),
        Path(".github/workflows/portfolio-snapshot-repair.yml"),
    ):
        assert expected in path.read_text(encoding="utf-8")
