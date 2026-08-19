from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_phase_policy import PhaseTimeError, plan_scheduled_phase
from scripts.validate_portfolio_snapshot import (
    SnapshotContractError,
    validate_snapshot_contract,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("phase", "started_at", "wait_seconds"),
    (
        ("premarket", datetime(2026, 8, 17, 6, 37, tzinfo=SHANGHAI), 0),
        ("midday", datetime(2026, 8, 17, 10, 50, tzinfo=SHANGHAI), 42 * 60),
        ("midday", datetime(2026, 8, 17, 11, 20, tzinfo=SHANGHAI), 12 * 60),
        ("midday", datetime(2026, 8, 17, 11, 45, tzinfo=SHANGHAI), 0),
        ("midday", datetime(2026, 8, 17, 12, 40, tzinfo=SHANGHAI), 0),
        ("close", datetime(2026, 8, 17, 14, 20, tzinfo=SHANGHAI), 45 * 60),
        ("close", datetime(2026, 8, 17, 15, 20, tzinfo=SHANGHAI), 0),
    ),
)
def test_scheduled_phase_wait_plan(
    phase: str, started_at: datetime, wait_seconds: int
) -> None:
    plan = plan_scheduled_phase(phase, started_at)

    assert plan.wait_seconds == wait_seconds
    assert plan.current == started_at


@pytest.mark.parametrize(
    ("phase", "started_at"),
    (
        ("premarket", datetime(2026, 8, 17, 8, 51, tzinfo=SHANGHAI)),
        ("midday", datetime(2026, 8, 17, 13, 1, tzinfo=SHANGHAI)),
        ("close", datetime(2026, 8, 17, 18, 0, tzinfo=SHANGHAI)),
    ),
)
def test_scheduled_phase_gate_rejects_missed_windows(
    phase: str, started_at: datetime
) -> None:
    with pytest.raises(PhaseTimeError, match="current snapshot unavailable"):
        plan_scheduled_phase(phase, started_at)


def test_scheduled_phase_wait_never_rounds_down_before_target() -> None:
    started_at = datetime(2026, 8, 17, 11, 31, 59, 900000, tzinfo=SHANGHAI)

    assert plan_scheduled_phase("midday", started_at).wait_seconds == 1


def test_workflow_crons_convert_to_intended_shanghai_queue_times() -> None:
    utc = ZoneInfo("UTC")
    conversions = (
        (
            datetime(2026, 8, 16, 22, 37, tzinfo=utc),
            datetime(2026, 8, 17, 6, 37, tzinfo=SHANGHAI),
        ),
        (
            datetime(2026, 8, 17, 2, 53, tzinfo=utc),
            datetime(2026, 8, 17, 10, 53, tzinfo=SHANGHAI),
        ),
        (
            datetime(2026, 8, 17, 6, 23, tzinfo=utc),
            datetime(2026, 8, 17, 14, 23, tzinfo=SHANGHAI),
        ),
        (
            datetime(2026, 8, 17, 7, 43, tzinfo=utc),
            datetime(2026, 8, 17, 15, 43, tzinfo=SHANGHAI),
        ),
        (
            datetime(2026, 8, 17, 8, 43, tzinfo=utc),
            datetime(2026, 8, 17, 16, 43, tzinfo=SHANGHAI),
        ),
        (
            datetime(2026, 8, 17, 9, 3, tzinfo=utc),
            datetime(2026, 8, 17, 17, 3, tzinfo=SHANGHAI),
        ),
    )

    for utc_time, shanghai_time in conversions:
        assert utc_time.astimezone(SHANGHAI) == shanghai_time
    assert conversions[0][0].strftime("%A") == "Sunday"
    assert conversions[0][1].strftime("%A") == "Monday"


def _payload(phase: str, generated_at: datetime, data_date: date) -> dict:
    return {
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Shanghai",
        "market_phase": phase,
        "data_date": data_date.isoformat(),
        "portfolio_status": "ok",
        "holdings_codes": ["159567"],
        "watchlist_codes": ["600519"],
        "stocks": [
            {"code": "159567", "tracking_type": "holding", "status": "ok"},
            {"code": "600519", "tracking_type": "watchlist", "status": "ok"},
        ],
        "benchmarks": [
            {"code": "sh000001", "status": "ok"},
            {"code": "sh000300", "status": "ok"},
            {"code": "sz399006", "status": "ok"},
            {"code": "sh000688", "status": "ok"},
        ],
        "errors": [],
    }


@pytest.mark.parametrize(
    ("phase", "generated_at", "data_date"),
    (
        (
            "premarket",
            datetime(2026, 8, 17, 6, 37, tzinfo=SHANGHAI),
            date(2026, 8, 14),
        ),
        (
            "midday",
            datetime(2026, 8, 17, 11, 32, tzinfo=SHANGHAI),
            date(2026, 8, 17),
        ),
        (
            "close",
            datetime(2026, 8, 17, 15, 5, tzinfo=SHANGHAI),
            date(2026, 8, 17),
        ),
    ),
)
def test_new_snapshot_contract_accepts_each_formal_phase(
    phase: str, generated_at: datetime, data_date: date
) -> None:
    portfolio = PortfolioConfig(
        version=1, holdings=("159567",), watchlist=("600519",)
    )
    payload = _payload(phase, generated_at, data_date)

    with (
        patch("scripts.validate_portfolio_snapshot.is_market_open", return_value=True),
        patch(
            "scripts.validate_portfolio_snapshot._phase_data_date",
            return_value=data_date,
        ),
    ):
        validate_snapshot_contract(
            payload,
            phase=phase,
            portfolio=portfolio,
            now=generated_at.replace(minute=generated_at.minute + 5),
        )


def test_old_snapshot_is_not_accepted_as_current_fallback() -> None:
    generated_at = datetime(2026, 8, 17, 11, 32, tzinfo=SHANGHAI)
    payload = _payload("midday", generated_at, date(2026, 8, 17))
    portfolio = PortfolioConfig(version=1, holdings=("159567",), watchlist=("600519",))

    with pytest.raises(SnapshotContractError, match="generated_at is not today"):
        validate_snapshot_contract(
            payload,
            phase="midday",
            portfolio=portfolio,
            now=datetime(2026, 8, 18, 11, 35, tzinfo=SHANGHAI),
        )


def test_snapshot_contract_rejects_security_pool_not_from_config() -> None:
    generated_at = datetime(2026, 8, 17, 11, 32, tzinfo=SHANGHAI)
    payload = _payload("midday", generated_at, date(2026, 8, 17))
    payload["stocks"] = [{"code": "159967", "tracking_type": "holding"}]
    portfolio = PortfolioConfig(version=1, holdings=("159567",), watchlist=("600519",))

    with (
        patch("scripts.validate_portfolio_snapshot.is_market_open", return_value=True),
        patch(
            "scripts.validate_portfolio_snapshot._phase_data_date",
            return_value=date(2026, 8, 17),
        ),
        pytest.raises(SnapshotContractError, match="code/tracking_type"),
    ):
        validate_snapshot_contract(
            payload,
            phase="midday",
            portfolio=portfolio,
            now=generated_at,
        )


def test_workflow_waits_then_validates_before_phase_scoped_commit() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.index("Check whether today's close is already ready") < workflow.index(
        "Wait for scheduled market target"
    )
    assert workflow.index("Wait for scheduled market target") < workflow.index(
        "Generate portfolio JSON"
    )
    assert workflow.index("Validate generated snapshot contract") < workflow.index(
        "Commit updated snapshots"
    )
    assert "if: steps.contract.outputs.generated == 'true'" in workflow
    assert 'git add -- "data/portfolio/${PHASE}.json"' in workflow
    assert "--allow-phase-time-override" not in workflow
    assert "git pull --rebase origin" in workflow
