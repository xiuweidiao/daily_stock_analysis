from __future__ import annotations

import json
import os
import subprocess
import textwrap
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from scripts.portfolio_close_watchdog import (
    CLOSE_ALREADY_FRESH,
    CLOSE_RECOVERY_REQUIRED,
    NON_TRADING_DAY,
    inspect_close_watchdog,
)
from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_schedule_context import build_schedule_context
from scripts.portfolio_schedule_map import WATCHDOG_CLOSE_CRONS, resolve_phase


SHANGHAI = ZoneInfo("Asia/Shanghai")
PORTFOLIO = PortfolioConfig(version=1, holdings=("688825",), watchlist=())
CORE_QUOTE = {
    "latest_price": 10.2,
    "prev_close": 10.0,
    "open": 10.1,
    "high": 10.5,
    "low": 9.9,
    "volume": 1000,
    "amount": 10200,
}
BENCHMARK_CODES = ("sh000001", "sh000300", "sz399006", "sh000688")


def _payload(
    generated_at: datetime,
    data_date: date,
    *,
    portfolio_status: str = "ok",
) -> dict:
    return {
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Shanghai",
        "market_phase": "close",
        "data_date": data_date.isoformat(),
        "generation_mode": "live",
        "portfolio_status": portfolio_status,
        "holdings_codes": ["688825"],
        "watchlist_codes": [],
        "stocks": [
            {
                "code": "688825",
                "tracking_type": "holding",
                "status": "partial",
                **CORE_QUOTE,
            }
        ],
        "benchmarks": [
            {"code": code, "status": "ok"} for code in BENCHMARK_CODES
        ],
        "errors": [],
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _inspect(
    path: Path,
    now: datetime,
    *,
    schedule: str | None = None,
    target_is_trading_day: bool = True,
):
    with (
        patch(
            "scripts.portfolio_schedule_context.is_market_open",
            return_value=target_is_trading_day,
        ),
        patch("scripts.portfolio_snapshot_readiness.is_market_open", return_value=True),
        patch("scripts.validate_portfolio_snapshot.is_market_open", return_value=True),
    ):
        return inspect_close_watchdog(
            path,
            portfolio=PORTFOLIO,
            now=now,
            schedule=schedule,
        )


def _workflow_step_script(step_name: str) -> str:
    workflow = Path(
        ".github/workflows/portfolio-close-watchdog.yml"
    ).read_text(encoding="utf-8")
    step_start = workflow.index(f"      - name: {step_name}")
    run_start = workflow.index("        run: |\n", step_start) + len(
        "        run: |\n"
    )
    next_step = workflow.find("      - name:", run_start)
    body = workflow[run_start:] if next_step == -1 else workflow[run_start:next_step]
    return textwrap.dedent(body)


def test_fresh_today_close_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "close.json"
    today = date(2026, 8, 31)
    payload = _payload(datetime(2026, 8, 31, 15, 10, tzinfo=SHANGHAI), today)
    _write(path, payload)
    original = path.read_bytes()

    result = _inspect(
        path, datetime(2026, 8, 31, 16, 17, tzinfo=SHANGHAI)
    )

    assert result.fresh is True
    assert result.should_generate is False
    assert result.result == CLOSE_ALREADY_FRESH
    assert path.read_bytes() == original


@pytest.mark.parametrize("existing", ("stale", "missing", "invalid"))
def test_stale_missing_or_invalid_close_requires_recovery(
    tmp_path: Path, existing: str
) -> None:
    path = tmp_path / "close.json"
    if existing != "missing":
        data_date = date(2026, 8, 29) if existing == "stale" else date(2026, 8, 31)
        payload = _payload(
            datetime.combine(data_date, datetime.min.time(), SHANGHAI).replace(
                hour=15, minute=10
            ),
            data_date,
            portfolio_status="error" if existing == "invalid" else "ok",
        )
        _write(path, payload)

    result = _inspect(
        path, datetime(2026, 8, 31, 16, 17, tzinfo=SHANGHAI)
    )

    assert result.fresh is False
    assert result.should_generate is True
    assert result.result == CLOSE_RECOVERY_REQUIRED
    expected_readiness = {
        "stale": "stale",
        "missing": "missing",
        "invalid": "invalid",
    }
    assert result.readiness == expected_readiness[existing]


def test_non_trading_nominal_date_skips_recovery(tmp_path: Path) -> None:
    result = _inspect(
        tmp_path / "close.json",
        datetime(2026, 10, 1, 16, 17, tzinfo=SHANGHAI),
        schedule=WATCHDOG_CLOSE_CRONS[0],
        target_is_trading_day=False,
    )

    assert result.target_is_trading_day is False
    assert result.should_generate is False
    assert result.result == NON_TRADING_DAY


def test_watchdog_after_1800_still_requires_recovery(tmp_path: Path) -> None:
    result = _inspect(
        tmp_path / "close.json",
        datetime(2026, 8, 31, 18, 20, tzinfo=SHANGHAI),
    )

    assert result.should_generate is True
    assert result.generation_mode == "recovery"
    assert "SCHEDULE_MISSED_PHASE_WINDOW" not in result.reason


def test_delayed_watchdog_keeps_nominal_trading_date(tmp_path: Path) -> None:
    result = _inspect(
        tmp_path / "close.json",
        datetime(2026, 9, 1, 2, 0, tzinfo=SHANGHAI),
        schedule=WATCHDOG_CLOSE_CRONS[-1],
    )

    assert result.target_date == "2026-08-31"
    assert result.should_generate is True


def test_all_watchdog_crons_map_to_close_and_expected_beijing_hours() -> None:
    expected_hours = (16, 17, 18, 19, 20)
    for cron, expected_hour in zip(WATCHDOG_CLOSE_CRONS, expected_hours):
        assert resolve_phase(cron) == "close"
        context = build_schedule_context(
            phase="close",
            schedule=cron,
            current=datetime(
                2026, 8, 31, expected_hour, 18, tzinfo=SHANGHAI
            ),
        )
        assert datetime.fromisoformat(context.expected_slot).hour == expected_hour
        assert datetime.fromisoformat(context.expected_slot).minute == 17


def test_normal_and_watchdog_close_share_concurrency_group() -> None:
    normal = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )
    watchdog = Path(
        ".github/workflows/portfolio-close-watchdog.yml"
    ).read_text(encoding="utf-8")

    expected = "portfolio-market-data-${{ github.ref }}-close"
    assert expected in watchdog
    assert "portfolio-market-data-${{ github.ref }}-" in normal
    assert "${{ needs.resolve_phase.outputs.phase }}" in normal
    assert "remote_is_fresh" in watchdog
    assert "Verify final remote close" in watchdog


def test_watchdog_generator_failure_is_red(tmp_path: Path) -> None:
    github_output = tmp_path / "generator-output"
    script = (
        "python() { return 1; }\n"
        + _workflow_step_script("Recover close snapshot")
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "TARGET_DATE": "2026-08-31",
            "GITHUB_OUTPUT": str(github_output),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "CLOSE_RECOVERY_FAILED" in result.stdout
    assert "result=failed" in github_output.read_text(encoding="utf-8")


def test_manual_close_after_1800_uses_open_ended_recovery_gate() -> None:
    context = build_schedule_context(
        phase="close",
        current=datetime(2026, 8, 31, 18, 20, tzinfo=SHANGHAI),
    )
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )

    assert context.expected_data_date == "2026-08-31"
    assert context.generation_mode == "recovery"
    assert "Wait for phase recovery window" in workflow
    assert "env.PHASE != 'intraday'" in workflow
    assert "workflow_dispatch:" in workflow
    assert "- close" in workflow
