from __future__ import annotations

import json
import os
import subprocess
import textwrap
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_market_data import BENCHMARKS, build_payload
from scripts.portfolio_phase_policy import (
    PhaseTimeError,
    plan_scheduled_phase,
    validate_phase_time,
)
from scripts.portfolio_schedule_context import build_schedule_context
from scripts.portfolio_schedule_map import (
    CLOSE_CRONS,
    MIDDAY_CRONS,
    PREMARKET_CRONS,
    resolve_phase,
)
from scripts.portfolio_snapshot_readiness import inspect_snapshot
from scripts.validate_portfolio_snapshot import (
    SnapshotContractError,
    validate_snapshot_contract,
)


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


def _payload(
    phase: str,
    generated_at: datetime,
    data_date: date,
    *,
    generation_mode: str | None = None,
) -> dict:
    payload = {
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Shanghai",
        "market_phase": phase,
        "data_date": data_date.isoformat(),
        "portfolio_status": "ok",
        "holdings_codes": ["688825"],
        "watchlist_codes": [],
        "stocks": [
            {
                "code": "688825",
                "tracking_type": "holding",
                "status": "partial",
                "data_date": data_date.isoformat(),
                "source_details": {"history": "fake", "realtime": None},
                **CORE,
            }
        ],
        "benchmarks": [
            {"code": code, "status": "ok", "data_date": data_date.isoformat()}
            for code in BENCHMARKS
        ],
        "errors": [],
    }
    if generation_mode:
        payload["generation_mode"] = generation_mode
    return payload


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _workflow_step_script(step_name: str) -> str:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )
    step_start = workflow.index(f"      - name: {step_name}")
    run_start = workflow.index("        run: |\n", step_start) + len(
        "        run: |\n"
    )
    next_step = workflow.find("      - name:", run_start)
    body = workflow[run_start:] if next_step == -1 else workflow[run_start:next_step]
    return textwrap.dedent(body)


def test_premarket_third_slot_independently_targets_same_business_day() -> None:
    context = build_schedule_context(
        phase="premarket",
        schedule=PREMARKET_CRONS[2],
        current=datetime(2026, 8, 28, 7, 38, tzinfo=SHANGHAI),
    )

    assert context.target_date == "2026-08-28"
    assert context.expected_data_date == "2026-08-27"
    assert context.can_generate is True
    assert resolve_phase(PREMARKET_CRONS[2]) == "premarket"


def test_premarket_delayed_145_minutes_expires_for_data_semantics() -> None:
    current = datetime(2026, 8, 28, 10, 2, tzinfo=SHANGHAI)
    context = build_schedule_context(
        phase="premarket", schedule=PREMARKET_CRONS[2], current=current
    )

    assert context.lateness_minutes == 145
    assert context.reason == "RECOVERY_WINDOW_EXPIRED"
    assert context.can_generate is False
    assert context.generation_mode == "recovery"
    assert context.inside_recovery_window is False
    with pytest.raises(PhaseTimeError, match="RECOVERY_WINDOW_EXPIRED"):
        plan_scheduled_phase(
            "premarket",
            current,
            target_date=date(2026, 8, 28),
            generation_mode="recovery",
        )


def test_premarket_live_allows_0830_but_rejects_0921() -> None:
    validate_phase_time(
        "premarket", datetime(2026, 9, 1, 8, 30, tzinfo=SHANGHAI)
    )

    with pytest.raises(PhaseTimeError, match="between 00:00 and 08:50"):
        validate_phase_time(
            "premarket", datetime(2026, 9, 1, 9, 21, tzinfo=SHANGHAI)
        )


def test_scheduled_premarket_delay_before_0925_switches_to_recovery() -> None:
    current = datetime(2026, 9, 1, 9, 7, tzinfo=SHANGHAI)
    context = build_schedule_context(
        phase="premarket", schedule=PREMARKET_CRONS[1], current=current
    )

    assert context.lateness_minutes == 120
    assert context.can_generate is True
    assert context.inside_recovery_window is True
    assert context.generation_mode == "recovery"
    assert context.reason == "RECOVERY_REQUIRED"
    assert plan_scheduled_phase(
        "premarket",
        current,
        target_date=date(2026, 9, 1),
        generation_mode="recovery",
    ).wait_seconds == 0


def test_manual_premarket_context_after_cutoff_uses_recovery() -> None:
    context = build_schedule_context(
        phase="premarket",
        current=datetime(2026, 9, 1, 9, 21, tzinfo=SHANGHAI),
        event_name="workflow_dispatch",
    )

    assert context.target_date == "2026-09-01"
    assert context.expected_data_date == "2026-08-31"
    assert context.can_generate is True
    assert context.generation_mode == "recovery"
    assert context.reason == "RECOVERY_REQUIRED"
    assert context.inside_recovery_window is True


@pytest.mark.parametrize("cron", MIDDAY_CRONS)
def test_midday_fallbacks_map_to_midday_and_can_generate(cron: str) -> None:
    current = datetime(2026, 8, 28, 12, 15, tzinfo=SHANGHAI)
    context = build_schedule_context(phase="midday", schedule=cron, current=current)

    assert resolve_phase(cron) == "midday"
    assert context.target_date == "2026-08-28"
    assert context.expected_data_date == "2026-08-28"
    assert context.can_generate is True
    assert context.inside_recovery_window is True


def test_midday_delayed_from_1135_to_1152_still_generates_stale_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "midday.json"
    _write(
        path,
        _payload(
            "midday",
            datetime(2026, 8, 27, 11, 35, tzinfo=SHANGHAI),
            date(2026, 8, 27),
        ),
    )
    current = datetime(2026, 8, 28, 11, 52, tzinfo=SHANGHAI)
    context = build_schedule_context(
        phase="midday", schedule=MIDDAY_CRONS[0], current=current
    )
    result = inspect_snapshot(
        path,
        phase="midday",
        portfolio=PORTFOLIO,
        target_date=date(2026, 8, 28),
        expected_data_date=date(2026, 8, 28),
        generation_mode="live",
        now=current,
        target_is_trading_day=True,
    )

    assert context.expected_slot.endswith("T11:35:00+08:00")
    assert context.lateness_minutes == 17
    assert context.can_generate is True
    assert context.inside_recovery_window is True
    assert result.should_generate is True
    assert result.state_code == "RECOVERY_REQUIRED"
    assert plan_scheduled_phase(
        "midday", current, target_date=date(2026, 8, 28)
    ).wait_seconds == 0


def test_midday_delayed_to_2200_is_explicitly_missed() -> None:
    context = build_schedule_context(
        phase="midday",
        schedule=MIDDAY_CRONS[0],
        current=datetime(2026, 8, 28, 22, 36, tzinfo=SHANGHAI),
    )

    assert context.reason == "RECOVERY_WINDOW_EXPIRED"
    assert context.inside_recovery_window is False


def test_midday_stale_at_1320_expires_recovery_window(tmp_path: Path) -> None:
    path = tmp_path / "midday.json"
    _write(
        path,
        _payload(
            "midday",
            datetime(2026, 8, 27, 11, 35, tzinfo=SHANGHAI),
            date(2026, 8, 27),
        ),
    )
    current = datetime(2026, 8, 28, 13, 20, tzinfo=SHANGHAI)
    context = build_schedule_context(
        phase="midday", schedule=MIDDAY_CRONS[2], current=current
    )
    readiness = inspect_snapshot(
        path,
        phase="midday",
        portfolio=PORTFOLIO,
        target_date=date(2026, 8, 28),
        expected_data_date=date(2026, 8, 28),
        generation_mode="live",
        now=current,
        target_is_trading_day=True,
    )

    assert readiness.should_generate is True
    assert context.can_generate is False
    assert context.reason == "RECOVERY_WINDOW_EXPIRED"
    with pytest.raises(PhaseTimeError, match="RECOVERY_WINDOW_EXPIRED"):
        plan_scheduled_phase("midday", current, target_date=date(2026, 8, 28))


def test_close_delayed_from_1543_to_1730_still_generates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "close.json"
    _write(
        path,
        _payload(
            "close",
            datetime(2026, 8, 27, 15, 10, tzinfo=SHANGHAI),
            date(2026, 8, 27),
            generation_mode="live",
        ),
    )
    current = datetime(2026, 8, 28, 17, 30, tzinfo=SHANGHAI)
    context = build_schedule_context(
        phase="close", schedule=CLOSE_CRONS[1], current=current
    )
    readiness = inspect_snapshot(
        path,
        phase="close",
        portfolio=PORTFOLIO,
        target_date=date(2026, 8, 28),
        expected_data_date=date(2026, 8, 28),
        generation_mode=context.generation_mode,
        now=current,
        target_is_trading_day=True,
    )

    assert context.expected_slot.endswith("T15:43:00+08:00")
    assert context.lateness_minutes == 107
    assert context.inside_recovery_window is True
    assert context.can_generate is True
    assert readiness.should_generate is True
    assert plan_scheduled_phase(
        "close",
        current,
        target_date=date(2026, 8, 28),
        expected_data_date=date(2026, 8, 28),
        generation_mode="live",
    ).wait_seconds == 0


def test_friday_close_delayed_to_saturday_keeps_friday_target() -> None:
    context = build_schedule_context(
        phase="close",
        schedule="3 9 * * 1-5",
        current=datetime(2026, 8, 29, 2, 0, tzinfo=SHANGHAI),
    )

    assert context.target_date == "2026-08-28"
    assert context.expected_data_date == "2026-08-28"
    assert context.generation_mode == "recovery"
    assert context.can_generate is True
    plan = plan_scheduled_phase(
        "close",
        datetime(2026, 8, 29, 2, 0, tzinfo=SHANGHAI),
        target_date=date(2026, 8, 28),
        expected_data_date=date(2026, 8, 28),
        generation_mode="recovery",
    )
    assert plan.wait_seconds == 0


def test_monday_morning_close_from_thursday_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "close.json"
    _write(
        path,
        _payload(
            "close",
            datetime(2026, 8, 27, 15, 10, tzinfo=SHANGHAI),
            date(2026, 8, 27),
            generation_mode="live",
        ),
    )
    result = inspect_snapshot(
        path,
        phase="close",
        portfolio=PORTFOLIO,
        target_date=date(2026, 8, 31),
        expected_data_date=date(2026, 8, 28),
        generation_mode="recovery",
        now=datetime(2026, 8, 31, 8, 0, tzinfo=SHANGHAI),
    )

    assert result.fresh is False
    assert result.should_generate is True
    assert result.reason == "stale_snapshot"


def test_holiday_close_context_uses_last_completed_session() -> None:
    context = build_schedule_context(
        phase="close",
        current=datetime(2026, 10, 4, 16, 0, tzinfo=SHANGHAI),
    )

    assert context.expected_data_date == "2026-09-30"
    assert context.generation_mode == "recovery"


def test_midday_fallback_is_idempotent_when_snapshot_is_fresh(tmp_path: Path) -> None:
    path = tmp_path / "midday.json"
    payload = _payload(
        "midday",
        datetime(2026, 8, 28, 11, 34, tzinfo=SHANGHAI),
        date(2026, 8, 28),
    )
    _write(path, payload)
    original = path.read_bytes()
    with patch("scripts.validate_portfolio_snapshot.is_market_open", return_value=True):
        result = inspect_snapshot(
            path,
            phase="midday",
            portfolio=PORTFOLIO,
            target_date=date(2026, 8, 28),
            expected_data_date=date(2026, 8, 28),
            generation_mode="live",
            now=datetime(2026, 8, 28, 12, 5, tzinfo=SHANGHAI),
            target_is_trading_day=True,
        )

    assert result.fresh is True
    assert result.should_generate is False
    assert result.state_code == "SNAPSHOT_ALREADY_FRESH"
    assert path.read_bytes() == original


class RecoverySources:
    def __init__(self, target_date: date) -> None:
        dates = pd.bdate_range(end=target_date, periods=70)
        self.daily = pd.DataFrame(
            {
                "date": dates,
                "open": [10.0] * len(dates),
                "high": [10.5] * len(dates),
                "low": [9.8] * len(dates),
                "close": [10.2] * len(dates),
                "volume": [1000] * len(dates),
                "amount": [10200] * len(dates),
            }
        )
        self.quote_calls = 0

    def history(self, code: str, *, end_date: date, days: int):
        return self.daily.copy(), "history_fake"

    def quote(self, code: str):
        self.quote_calls += 1
        raise AssertionError("recovery must not request realtime quotes")

    def name(self, code: str) -> str:
        return "长鑫科技"

    def benchmarks(self):
        raise AssertionError("recovery must not request realtime benchmarks")

    def benchmark_history(self, code: str, *, end_date: date, days: int):
        return self.daily.tail(days).copy(), "index_history_fake"


def test_premarket_recovery_after_cutoff_uses_previous_completed_daily_bars() -> None:
    expected_data_date = date(2026, 8, 31)
    generated_at = datetime(2026, 9, 1, 9, 21, tzinfo=SHANGHAI)
    sources = RecoverySources(expected_data_date)

    payload = build_payload(
        "premarket",
        sources=sources,
        portfolio=PORTFOLIO,
        now=generated_at,
        expected_data_date=expected_data_date,
        generation_mode="recovery",
    )

    assert sources.quote_calls == 0
    assert payload["generation_mode"] == "recovery"
    assert payload["generated_at"] == generated_at.isoformat()
    assert payload["market_phase"] == "premarket"
    assert payload["data_date"] == "2026-08-31"
    assert all(
        stock["source_details"]["realtime"] is None
        for stock in payload["stocks"]
    )
    validate_snapshot_contract(
        payload,
        phase="premarket",
        portfolio=PORTFOLIO,
        now=generated_at,
        target_date=date(2026, 9, 1),
        expected_data_date=expected_data_date,
        generation_mode="recovery",
    )


def test_premarket_recovery_rejects_wrong_expected_data_date() -> None:
    with pytest.raises(
        PhaseTimeError, match="latest completed A-share trading day"
    ):
        build_payload(
            "premarket",
            sources=RecoverySources(date(2026, 8, 28)),
            portfolio=PORTFOLIO,
            now=datetime(2026, 9, 1, 9, 21, tzinfo=SHANGHAI),
            expected_data_date=date(2026, 8, 28),
            generation_mode="recovery",
        )


def test_premarket_recovery_rejects_generated_at_at_deadline() -> None:
    with pytest.raises(PhaseTimeError, match="RECOVERY_WINDOW_EXPIRED"):
        build_payload(
            "premarket",
            sources=RecoverySources(date(2026, 8, 31)),
            portfolio=PORTFOLIO,
            now=datetime(2026, 9, 1, 9, 25, tzinfo=SHANGHAI),
            expected_data_date=date(2026, 8, 31),
            generation_mode="recovery",
        )


def test_premarket_recovery_contract_rejects_wrong_payload_data_date() -> None:
    payload = _payload(
        "premarket",
        datetime(2026, 9, 1, 9, 21, tzinfo=SHANGHAI),
        date(2026, 8, 28),
        generation_mode="recovery",
    )

    with pytest.raises(SnapshotContractError, match="data_date must be 2026-08-31"):
        validate_snapshot_contract(
            payload,
            phase="premarket",
            portfolio=PORTFOLIO,
            now=datetime(2026, 9, 1, 9, 22, tzinfo=SHANGHAI),
            target_date=date(2026, 9, 1),
            expected_data_date=date(2026, 8, 31),
            generation_mode="recovery",
        )


@pytest.mark.parametrize(
    ("generated_at", "data_date", "expected_fresh", "expected_reason"),
    (
        (
            datetime(2026, 8, 31, 7, 7, tzinfo=SHANGHAI),
            date(2026, 8, 28),
            False,
            "stale_snapshot",
        ),
        (
            datetime(2026, 9, 1, 7, 7, tzinfo=SHANGHAI),
            date(2026, 8, 31),
            True,
            "already_fresh",
        ),
    ),
)
def test_manual_premarket_readiness_is_recovery_only_when_not_fresh(
    tmp_path: Path,
    generated_at: datetime,
    data_date: date,
    expected_fresh: bool,
    expected_reason: str,
) -> None:
    path = tmp_path / "premarket.json"
    _write(path, _payload("premarket", generated_at, data_date))
    context = build_schedule_context(
        phase="premarket",
        current=datetime(2026, 9, 1, 9, 21, tzinfo=SHANGHAI),
        event_name="workflow_dispatch",
    )

    result = inspect_snapshot(
        path,
        phase="premarket",
        portfolio=PORTFOLIO,
        target_date=date.fromisoformat(context.target_date),
        expected_data_date=date.fromisoformat(context.expected_data_date),
        generation_mode=context.generation_mode,
        now=datetime(2026, 9, 1, 9, 21, tzinfo=SHANGHAI),
        target_is_trading_day=True,
    )

    assert result.fresh is expected_fresh
    assert result.should_generate is (not expected_fresh)
    assert result.reason == expected_reason
    assert result.generation_mode == "recovery"


def test_close_recovery_uses_only_target_date_daily_bars() -> None:
    target = date(2026, 8, 28)
    sources = RecoverySources(target)
    generated_at = datetime(2026, 8, 29, 2, 0, tzinfo=SHANGHAI)

    payload = build_payload(
        "close",
        sources=sources,
        portfolio=PORTFOLIO,
        now=generated_at,
        expected_data_date=target,
        generation_mode="recovery",
    )

    assert sources.quote_calls == 0
    assert payload["generation_mode"] == "recovery"
    assert payload["generated_at"] == generated_at.isoformat()
    assert payload["data_date"] == target.isoformat()
    assert all(stock["data_date"] == target.isoformat() for stock in payload["stocks"])
    assert all(
        stock["source_details"]["realtime"] is None for stock in payload["stocks"]
    )
    assert all(
        benchmark["data_date"] == target.isoformat()
        for benchmark in payload["benchmarks"]
    )
    validate_snapshot_contract(
        payload,
        phase="close",
        portfolio=PORTFOLIO,
        now=generated_at,
        target_date=target,
        expected_data_date=target,
        generation_mode="recovery",
    )


def test_recovery_contract_rejects_realtime_overlay() -> None:
    target = date(2026, 8, 28)
    payload = _payload(
        "close",
        datetime(2026, 8, 29, 2, 0, tzinfo=SHANGHAI),
        target,
        generation_mode="recovery",
    )
    payload["stocks"][0]["source_details"]["realtime"] = "efinance"

    with pytest.raises(SnapshotContractError, match="must not use realtime"):
        validate_snapshot_contract(
            payload,
            phase="close",
            portfolio=PORTFOLIO,
            now=datetime(2026, 8, 29, 2, 1, tzinfo=SHANGHAI),
            target_date=target,
            expected_data_date=target,
            generation_mode="recovery",
        )


def test_workflow_has_phase_isolation_and_finite_push_retry() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )

    assert "needs: resolve_phase" in workflow
    assert (
        "group: portfolio-market-data-${{ github.ref }}-"
        "${{ needs.resolve_phase.outputs.phase }}"
    ) in workflow
    assert "for attempt in 1 2 3" in workflow
    assert "remote_is_fresh" in workflow
    assert "Verify final remote freshness" in workflow
    for diagnostic in (
        "expected schedule slot",
        "lateness_minutes",
        "expected data_date",
        "Recovery window:",
        "inside recovery window",
        "should_generate",
        "generation mode",
        "validator",
        "git diff",
        "commit",
        "final state code",
    ):
        assert diagnostic in workflow


def test_workflow_dispatch_premarket_propagates_recovery_mode() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )

    assert 'args=(--phase "$PHASE" --event-name "$EVENT_NAME")' in workflow
    assert (
        'if [ "$PHASE" != "intraday" ]; then'
        in workflow
    )
    assert 'args+=(--generation-mode "$GENERATION_MODE")' in workflow


def test_workflow_never_allows_unchanged_stale_formal_snapshot() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )

    assert "SNAPSHOT_NOT_UPDATED" in workflow
    assert 'if [ "$PHASE" != "intraday" ]; then' in workflow
    assert "exit 1" in workflow[workflow.index("SNAPSHOT_NOT_UPDATED") :]
    assert "midday" not in workflow[
        workflow.index("SNAPSHOT_NOT_UPDATED") - 300 : workflow.index("SNAPSHOT_NOT_UPDATED")
    ]


def test_midday_generator_without_file_change_is_a_red_failure(
    tmp_path: Path,
) -> None:
    github_output = tmp_path / "contract-output"
    script = (
        "git() { return 0; }\n"
        "python() { printf '%s' '{\"fresh\":false}'; }\n"
        + _workflow_step_script("Validate generated snapshot contract")
    )

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=Path.cwd(),
        env={
            **os.environ,
            "PHASE": "midday",
            "TARGET_DATE": "2026-08-28",
            "EXPECTED_DATA_DATE": "2026-08-28",
            "GENERATION_MODE": "live",
            "GITHUB_OUTPUT": str(github_output),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "SNAPSHOT_NOT_UPDATED" in result.stdout
    assert "validator=fail" in github_output.read_text(encoding="utf-8")


def test_remote_fresh_snapshot_wins_without_duplicate_commit(tmp_path: Path) -> None:
    github_output = tmp_path / "commit-output"
    call_log = tmp_path / "git-calls"
    script = (
        "git() { printf '%s\\n' \"$*\" >> \"$CALL_LOG\"; "
        "if [ \"$1\" = show ]; then printf '{}'; fi; return 0; }\n"
        "python() { printf '%s' '{\"fresh\":true}'; }\n"
        + _workflow_step_script("Commit updated snapshot safely")
    )

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=Path.cwd(),
        env={
            **os.environ,
            "PHASE": "midday",
            "TARGET_DATE": "2026-08-28",
            "EXPECTED_DATA_DATE": "2026-08-28",
            "GENERATION_MODE": "live",
            "GITHUB_REF_NAME": "main",
            "GITHUB_OUTPUT": str(github_output),
            "CALL_LOG": str(call_log),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "REMOTE_SNAPSHOT_ALREADY_FRESH" in result.stdout
    assert "result=no-op" in github_output.read_text(encoding="utf-8")
    calls = call_log.read_text(encoding="utf-8")
    assert "fetch origin main" in calls
    assert "add --" not in calls
    assert "commit -m" not in calls
    assert "push origin" not in calls
