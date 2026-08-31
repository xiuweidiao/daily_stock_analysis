from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from scripts.portfolio_close_readiness import (
    CloseReadiness,
    TODAY_CLOSE_ALREADY_READY,
    TODAY_CLOSE_INVALID,
    TODAY_CLOSE_MISSING,
    inspect_close_snapshot,
    main as readiness_main,
)
from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_phase_policy import plan_scheduled_phase


SHANGHAI = ZoneInfo("Asia/Shanghai")
PORTFOLIO = PortfolioConfig(
    version=1,
    holdings=("688825", "159567"),
    watchlist=("600519",),
)
BENCHMARK_CODES = ("sh000001", "sh000300", "sz399006", "sh000688")
CORE_QUOTE = {
    "latest_price": 10.2,
    "prev_close": 10.0,
    "open": 10.1,
    "high": 10.5,
    "low": 9.9,
    "volume": 1000,
    "amount": 10200,
}


def _close_payload(generated_at: datetime, data_date: date) -> dict:
    return {
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Shanghai",
        "market_phase": "close",
        "data_date": data_date.isoformat(),
        "portfolio_status": "ok",
        "holdings_codes": list(PORTFOLIO.holdings),
        "watchlist_codes": list(PORTFOLIO.watchlist),
        "stocks": [
            {
                "code": "688825",
                "tracking_type": "holding",
                "status": "partial",
                **CORE_QUOTE,
            },
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
            {"code": code, "status": "ok"} for code in BENCHMARK_CODES
        ],
        "errors": [],
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _inspect(path: Path, now: datetime):
    with (
        patch("scripts.portfolio_schedule_context.is_market_open", return_value=True),
        patch(
            "scripts.portfolio_schedule_context.get_effective_trading_date",
            return_value=now.date(),
        ),
        patch("scripts.portfolio_snapshot_readiness.is_market_open", return_value=True),
        patch("scripts.validate_portfolio_snapshot.is_market_open", return_value=True),
    ):
        return inspect_close_snapshot(path, portfolio=PORTFOLIO, now=now)


@pytest.mark.parametrize("hour,minute", ((15, 43), (16, 43), (17, 3)))
def test_retries_skip_when_primary_close_is_already_valid(
    tmp_path: Path, hour: int, minute: int
) -> None:
    path = tmp_path / "close.json"
    _write(
        path,
        _close_payload(
            datetime(2026, 8, 19, 15, 5, tzinfo=SHANGHAI),
            date(2026, 8, 19),
        ),
    )

    result = _inspect(
        path, datetime(2026, 8, 19, hour, minute, tzinfo=SHANGHAI)
    )

    assert result.status == TODAY_CLOSE_ALREADY_READY
    assert result.should_generate is False


def test_retry_generates_when_primary_never_ran_and_close_is_from_yesterday(
    tmp_path: Path,
) -> None:
    path = tmp_path / "close.json"
    _write(
        path,
        _close_payload(
            datetime(2026, 8, 18, 15, 5, tzinfo=SHANGHAI),
            date(2026, 8, 18),
        ),
    )

    result = _inspect(
        path, datetime(2026, 8, 19, 15, 43, tzinfo=SHANGHAI)
    )

    assert result.status == TODAY_CLOSE_MISSING
    assert result.should_generate is True


def test_retry_recovers_after_source_error_snapshot_failed_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "close.json"
    generated_at = datetime(2026, 8, 19, 15, 5, tzinfo=SHANGHAI)
    payload = _close_payload(generated_at, generated_at.date())
    payload["stocks"][1]["status"] = "error"
    _write(path, payload)

    failed_result = _inspect(
        path, datetime(2026, 8, 19, 15, 10, tzinfo=SHANGHAI)
    )
    assert failed_result.status == TODAY_CLOSE_INVALID
    assert failed_result.should_generate is True

    payload["stocks"][1]["status"] = "ok"
    payload["generated_at"] = datetime(
        2026, 8, 19, 15, 43, tzinfo=SHANGHAI
    ).isoformat()
    _write(path, payload)
    recovered_result = _inspect(
        path, datetime(2026, 8, 19, 15, 48, tzinfo=SHANGHAI)
    )
    assert recovered_result.status == TODAY_CLOSE_ALREADY_READY
    assert recovered_result.should_generate is False


def test_serial_primary_then_retry_produces_only_one_ready_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "close.json"
    first = _inspect(path, datetime(2026, 8, 19, 15, 40, tzinfo=SHANGHAI))
    assert first.status == TODAY_CLOSE_MISSING
    assert first.should_generate is True

    payload = _close_payload(
        datetime(2026, 8, 19, 15, 41, tzinfo=SHANGHAI),
        date(2026, 8, 19),
    )
    _write(path, payload)
    second = _inspect(path, datetime(2026, 8, 19, 15, 43, tzinfo=SHANGHAI))

    assert second.status == TODAY_CLOSE_ALREADY_READY
    assert second.should_generate is False
    assert json.loads(path.read_text(encoding="utf-8"))["generated_at"] == payload[
        "generated_at"
    ]


def test_today_close_with_wrong_contract_is_regenerated(tmp_path: Path) -> None:
    path = tmp_path / "close.json"
    payload = _close_payload(
        datetime(2026, 8, 19, 15, 5, tzinfo=SHANGHAI),
        date(2026, 8, 19),
    )
    payload["holdings_codes"] = ["159567"]
    _write(path, payload)

    result = _inspect(
        path, datetime(2026, 8, 19, 15, 43, tzinfo=SHANGHAI)
    )

    assert result.status == TODAY_CLOSE_INVALID
    assert result.should_generate is True


def test_today_close_with_null_core_quote_is_invalid_and_regenerated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "close.json"
    payload = _close_payload(
        datetime(2026, 8, 19, 15, 5, tzinfo=SHANGHAI),
        date(2026, 8, 19),
    )
    payload["stocks"][0]["latest_price"] = None
    _write(path, payload)

    result = _inspect(
        path, datetime(2026, 8, 19, 15, 43, tzinfo=SHANGHAI)
    )

    assert result.status == TODAY_CLOSE_INVALID
    assert result.should_generate is True
    assert result.reason == "invalid_snapshot"


def test_non_trading_day_can_recover_latest_missing_close(tmp_path: Path) -> None:
    path = tmp_path / "close.json"
    with (
        patch("scripts.portfolio_schedule_context.is_market_open", return_value=False),
        patch(
            "scripts.portfolio_schedule_context.get_effective_trading_date",
            return_value=date(2026, 8, 21),
        ),
        patch("scripts.portfolio_snapshot_readiness.is_market_open", return_value=True),
    ):
        result = inspect_close_snapshot(
            path,
            portfolio=PORTFOLIO,
            now=datetime(2026, 8, 22, 15, 43, tzinfo=SHANGHAI),
        )

    assert result.status == TODAY_CLOSE_MISSING
    assert result.should_generate is True
    assert not path.exists()


def test_missing_close_after_1800_uses_recovery_mode(tmp_path: Path) -> None:
    now = datetime(2026, 8, 19, 18, 1, tzinfo=SHANGHAI)
    result = _inspect(tmp_path / "close.json", now)

    assert result.status == TODAY_CLOSE_MISSING
    assert result.should_generate is True
    plan = plan_scheduled_phase(
        "close",
        now,
        target_date=now.date(),
        expected_data_date=now.date(),
        generation_mode="recovery",
    )
    assert plan.wait_seconds == 0
    assert plan.generation_mode == "recovery"


def test_workflow_uses_phase_concurrency_and_emits_failure_states() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "group: portfolio-market-data-${{ github.ref }}-"
        "${{ needs.resolve_phase.outputs.phase }}"
    ) in workflow
    assert "cancel-in-progress: false" in workflow
    assert "steps.readiness.outputs.should_generate" in workflow
    assert "steps.decision.outputs.should_generate == 'true'" in workflow
    assert "if: steps.contract.outputs.generated == 'true'" in workflow
    for status in (
        "SNAPSHOT_GENERATED",
        "GENERATION_FAILED",
        "SNAPSHOT_CONTRACT_FAILED",
        "PUSH_FAILED",
    ):
        assert status in workflow


def test_readiness_cli_emits_machine_readable_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(sys, "argv", ["portfolio_close_readiness.py"])
    result = CloseReadiness(
        TODAY_CLOSE_ALREADY_READY,
        False,
        "contract passed",
    )
    with (
        patch(
            "scripts.portfolio_close_readiness.load_portfolio_config",
            return_value=PORTFOLIO,
        ),
        patch(
            "scripts.portfolio_close_readiness.inspect_close_snapshot",
            return_value=result,
        ),
    ):
        assert readiness_main() == 0

    assert capsys.readouterr().out.strip() == TODAY_CLOSE_ALREADY_READY
    assert github_output.read_text(encoding="utf-8") == (
        "status=TODAY_CLOSE_ALREADY_READY\nshould_generate=false\n"
    )
