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
CORE_QUOTE = {
    "latest_price": 10.2,
    "prev_close": 10.0,
    "open": 10.1,
    "high": 10.5,
    "low": 9.9,
    "volume": 1000,
    "amount": 10200,
}


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
            datetime(2026, 8, 16, 23, 7, tzinfo=utc),
            datetime(2026, 8, 17, 7, 7, tzinfo=SHANGHAI),
        ),
        (
            datetime(2026, 8, 16, 23, 37, tzinfo=utc),
            datetime(2026, 8, 17, 7, 37, tzinfo=SHANGHAI),
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
            {
                "code": "159567",
                "tracking_type": "holding",
                "status": "ok",
                **CORE_QUOTE,
            },
            {
                "code": "600519",
                "tracking_type": "watchlist",
                "status": "ok",
                **CORE_QUOTE,
            },
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

    with pytest.raises(
        SnapshotContractError, match="generated_at is not the target date"
    ):
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


@pytest.mark.parametrize(
    "field",
    ("latest_price", "prev_close", "open", "high", "low", "volume", "amount"),
)
def test_snapshot_contract_rejects_null_core_quote_fields(field: str) -> None:
    generated_at = datetime(2026, 8, 17, 15, 5, tzinfo=SHANGHAI)
    payload = _payload("close", generated_at, generated_at.date())
    payload["stocks"][0][field] = None
    portfolio = PortfolioConfig(
        version=1, holdings=("159567",), watchlist=("600519",)
    )

    with (
        patch("scripts.validate_portfolio_snapshot.is_market_open", return_value=True),
        patch(
            "scripts.validate_portfolio_snapshot._phase_data_date",
            return_value=generated_at.date(),
        ),
        pytest.raises(
            SnapshotContractError,
            match=rf"field {field} must be a finite number",
        ),
    ):
        validate_snapshot_contract(
            payload,
            phase="close",
            portfolio=portfolio,
            now=generated_at,
        )


@pytest.mark.parametrize(
    "value", (float("nan"), float("inf"), "10.2", "not-a-number", True)
)
def test_snapshot_contract_rejects_non_finite_latest_price(value: object) -> None:
    generated_at = datetime(2026, 8, 17, 15, 5, tzinfo=SHANGHAI)
    payload = _payload("close", generated_at, generated_at.date())
    payload["stocks"][0]["latest_price"] = value
    portfolio = PortfolioConfig(
        version=1, holdings=("159567",), watchlist=("600519",)
    )

    with (
        patch("scripts.validate_portfolio_snapshot.is_market_open", return_value=True),
        patch(
            "scripts.validate_portfolio_snapshot._phase_data_date",
            return_value=generated_at.date(),
        ),
        pytest.raises(
            SnapshotContractError,
            match="field latest_price must be a finite number",
        ),
    ):
        validate_snapshot_contract(
            payload,
            phase="close",
            portfolio=portfolio,
            now=generated_at,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("latest_price", 0, "latest_price must be greater than zero"),
        ("prev_close", -1, "prev_close must be greater than zero"),
        ("high", 9.8, "high must be greater than or equal to low"),
        ("volume", -1, "volume must be non-negative"),
        ("amount", -1, "amount must be non-negative"),
    ),
)
def test_snapshot_contract_rejects_invalid_core_quote_values(
    field: str, value: float, message: str
) -> None:
    generated_at = datetime(2026, 8, 17, 15, 5, tzinfo=SHANGHAI)
    payload = _payload("close", generated_at, generated_at.date())
    payload["stocks"][0][field] = value
    portfolio = PortfolioConfig(
        version=1, holdings=("159567",), watchlist=("600519",)
    )

    with (
        patch("scripts.validate_portfolio_snapshot.is_market_open", return_value=True),
        patch(
            "scripts.validate_portfolio_snapshot._phase_data_date",
            return_value=generated_at.date(),
        ),
        pytest.raises(SnapshotContractError, match=message),
    ):
        validate_snapshot_contract(
            payload,
            phase="close",
            portfolio=portfolio,
            now=generated_at,
        )


def test_snapshot_contract_allows_partial_short_history_indicators() -> None:
    generated_at = datetime(2026, 8, 17, 15, 5, tzinfo=SHANGHAI)
    payload = _payload("close", generated_at, generated_at.date())
    stock = payload["stocks"][0]
    stock.update(
        {
            "status": "partial",
            "status_detail": "insufficient bars for MA60/return_60d",
            "MA20": None,
            "MA60": None,
            "return_20d": None,
            "return_60d": None,
        }
    )
    portfolio = PortfolioConfig(
        version=1, holdings=("159567",), watchlist=("600519",)
    )

    with (
        patch("scripts.validate_portfolio_snapshot.is_market_open", return_value=True),
        patch(
            "scripts.validate_portfolio_snapshot._phase_data_date",
            return_value=generated_at.date(),
        ),
    ):
        validate_snapshot_contract(
            payload,
            phase="close",
            portfolio=portfolio,
            now=generated_at,
        )


def test_workflow_waits_then_validates_before_phase_scoped_commit() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.index("Check snapshot readiness") < workflow.index(
        "Wait for scheduled market target"
    )
    assert workflow.index("Wait for scheduled market target") < workflow.index(
        "Generate portfolio JSON"
    )
    assert workflow.index("Validate generated snapshot contract") < workflow.index(
        "Commit updated snapshot safely"
    )
    assert "if: steps.contract.outputs.generated == 'true'" in workflow
    assert 'git add -- "$snapshot"' in workflow
    assert "--allow-phase-time-override" not in workflow
    assert "git pull --rebase origin" in workflow
    assert (
        "group: portfolio-market-data-${{ github.ref }}-"
        "${{ needs.resolve_phase.outputs.phase }}"
    ) in workflow
