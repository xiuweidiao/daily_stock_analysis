from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_market_data import _phase_data_date
from scripts.portfolio_premarket_readiness import (
    PremarketReadiness,
    inspect_premarket_snapshot,
    main as readiness_main,
)
from scripts.validate_portfolio_snapshot import (
    SnapshotContractError,
    validate_snapshot_contract,
)


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


def _premarket_payload(generated_at: datetime, data_date: date) -> dict:
    stocks = []
    for code, tracking_type in PORTFOLIO.tracked_securities():
        stocks.append(
            {
                "code": code,
                "tracking_type": tracking_type,
                "status": "partial" if code == "688825" else "ok",
                **CORE_QUOTE,
            }
        )
    return {
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Shanghai",
        "market_phase": "premarket",
        "data_date": data_date.isoformat(),
        "portfolio_status": "ok",
        "holdings_codes": list(PORTFOLIO.holdings),
        "watchlist_codes": list(PORTFOLIO.watchlist),
        "stocks": stocks,
        "benchmarks": [
            {"code": code, "status": "ok"} for code in BENCHMARK_CODES
        ],
        "errors": [],
    }


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


def _inspect(path: Path, now: datetime, expected_data_date: date):
    with (
        patch(
            "scripts.portfolio_premarket_readiness.is_market_open",
            return_value=True,
        ),
        patch(
            "scripts.validate_portfolio_snapshot.is_market_open",
            return_value=True,
        ),
        patch(
            "scripts.portfolio_market_data.get_effective_trading_date",
            return_value=expected_data_date,
        ),
    ):
        return inspect_premarket_snapshot(path, portfolio=PORTFOLIO, now=now)


@pytest.mark.parametrize("hour,minute", ((7, 7), (7, 37)))
def test_fallback_skips_today_valid_premarket_without_regeneration(
    tmp_path: Path, hour: int, minute: int
) -> None:
    path = tmp_path / "premarket.json"
    _write(
        path,
        _premarket_payload(
            datetime(2026, 8, 27, 6, 59, tzinfo=SHANGHAI),
            date(2026, 8, 26),
        ),
    )
    original = path.read_bytes()

    result = _inspect(
        path,
        datetime(2026, 8, 27, hour, minute, tzinfo=SHANGHAI),
        date(2026, 8, 26),
    )

    assert result.fresh is True
    assert result.should_generate is False
    assert result.freshness == "fresh"
    assert result.reason == "already_fresh"
    assert path.read_bytes() == original


def test_fallback_regenerates_when_only_yesterday_premarket_exists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "premarket.json"
    _write(
        path,
        _premarket_payload(
            datetime(2026, 8, 26, 7, 4, tzinfo=SHANGHAI),
            date(2026, 8, 25),
        ),
    )

    result = _inspect(
        path,
        datetime(2026, 8, 27, 7, 7, tzinfo=SHANGHAI),
        date(2026, 8, 26),
    )

    assert result.fresh is False
    assert result.should_generate is True
    assert result.freshness == "stale"
    assert result.reason == "stale_snapshot"


def test_0707_fallback_can_recover_when_primary_never_created_a_run(
    tmp_path: Path,
) -> None:
    result = _inspect(
        tmp_path / "premarket.json",
        datetime(2026, 8, 27, 7, 7, tzinfo=SHANGHAI),
        date(2026, 8, 26),
    )

    assert result.fresh is False
    assert result.should_generate is True
    assert result.freshness == "stale"
    assert result.reason == "missing_snapshot"


def test_delayed_primary_then_fallback_produces_one_ready_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "premarket.json"
    first = _inspect(
        path,
        datetime(2026, 8, 27, 7, 6, tzinfo=SHANGHAI),
        date(2026, 8, 26),
    )
    assert first.should_generate is True

    payload = _premarket_payload(
        datetime(2026, 8, 27, 7, 6, tzinfo=SHANGHAI),
        date(2026, 8, 26),
    )
    _write(path, payload)
    fallback = _inspect(
        path,
        datetime(2026, 8, 27, 7, 7, tzinfo=SHANGHAI),
        date(2026, 8, 26),
    )

    assert fallback.fresh is True
    assert fallback.should_generate is False
    assert json.loads(path.read_text(encoding="utf-8"))["generated_at"] == payload[
        "generated_at"
    ]


def test_monday_premarket_uses_previous_friday() -> None:
    generated_at = datetime(2026, 8, 24, 7, 7, tzinfo=SHANGHAI)

    assert _phase_data_date("premarket", generated_at) == date(2026, 8, 21)


def test_first_session_after_public_holiday_uses_last_completed_session() -> None:
    generated_at = datetime(2026, 10, 8, 7, 7, tzinfo=SHANGHAI)

    assert _phase_data_date("premarket", generated_at) == date(2026, 9, 30)


def test_invalid_core_quote_requires_regeneration(tmp_path: Path) -> None:
    path = tmp_path / "premarket.json"
    payload = _premarket_payload(
        datetime(2026, 8, 27, 7, 6, tzinfo=SHANGHAI),
        date(2026, 8, 26),
    )
    payload["stocks"][0]["latest_price"] = None
    _write(path, payload)

    result = _inspect(
        path,
        datetime(2026, 8, 27, 7, 7, tzinfo=SHANGHAI),
        date(2026, 8, 26),
    )

    assert result.fresh is False
    assert result.should_generate is True
    assert result.freshness == "invalid"


def test_non_trading_day_skips_without_generating(tmp_path: Path) -> None:
    path = tmp_path / "premarket.json"
    now = datetime(2026, 10, 4, 7, 7, tzinfo=SHANGHAI)
    with (
        patch(
            "scripts.portfolio_premarket_readiness.is_market_open",
            return_value=False,
        ),
        patch(
            "scripts.portfolio_market_data.get_effective_trading_date",
            return_value=date(2026, 9, 30),
        ),
    ):
        result = inspect_premarket_snapshot(path, portfolio=PORTFOLIO, now=now)

    assert result.should_generate is False
    assert result.reason == "non_trading_day"
    assert not path.exists()


def test_generated_stale_snapshot_fails_formal_validator() -> None:
    generated_at = datetime(2026, 8, 26, 7, 7, tzinfo=SHANGHAI)
    payload = _premarket_payload(generated_at, date(2026, 8, 25))

    with pytest.raises(SnapshotContractError, match="generated_at is not today"):
        validate_snapshot_contract(
            payload,
            phase="premarket",
            portfolio=PORTFOLIO,
            now=datetime(2026, 8, 27, 7, 7, tzinfo=SHANGHAI),
        )


def test_workflow_generator_failure_is_a_red_failure(tmp_path: Path) -> None:
    github_output = tmp_path / "generator-output"
    script = "python() { return 1; }\n" + _workflow_step_script(
        "Generate portfolio JSON"
    )

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=Path.cwd(),
        env={
            **os.environ,
            "PHASE": "premarket",
            "GITHUB_OUTPUT": str(github_output),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "PREMARKET_GENERATION_FAILED" in result.stdout
    assert "result=failed\n" in github_output.read_text(encoding="utf-8")


def test_workflow_unchanged_stale_output_is_a_red_failure(tmp_path: Path) -> None:
    github_output = tmp_path / "contract-output"
    script = (
        "git() { return 0; }\n"
        "python() { return 0; }\n"
        + _workflow_step_script("Validate generated snapshot contract")
    )

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=Path.cwd(),
        env={
            **os.environ,
            "PHASE": "premarket",
            "GITHUB_OUTPUT": str(github_output),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "PREMARKET_GENERATION_FAILED" in result.stdout
    output = github_output.read_text(encoding="utf-8")
    assert "generated=false\n" in output
    assert "git_diff=unchanged\n" in output
    assert "validator=fail\n" in output


def test_workflow_serializes_delayed_primary_and_fallback() -> None:
    workflow = Path(".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )

    assert "group: portfolio-market-data-${{ github.ref }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert (
        "PREMARKET_SHOULD_GENERATE: "
        "${{ steps.premarket_readiness.outputs.should_generate }}"
    ) in workflow
    assert "if: steps.decision.outputs.should_generate == 'true'" in workflow
    assert "git pull --rebase origin" in workflow
    for summary_field in (
        "Beijing time:",
        "Event:",
        "Schedule expression:",
        "Resolved phase:",
        "Current snapshot:",
        "latest_completed_trading_day:",
        "Freshness:",
        "generator:",
        "validator:",
        "git diff:",
        "commit:",
    ):
        assert summary_field in workflow


def test_readiness_cli_emits_github_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(sys, "argv", ["portfolio_premarket_readiness.py"])
    result = PremarketReadiness(
        fresh=True,
        should_generate=False,
        freshness="fresh",
        reason="already_fresh",
        generated_at="2026-08-27T07:06:00+08:00",
        data_date="2026-08-26",
        market_phase="premarket",
        portfolio_status="ok",
        today_cn="2026-08-27",
        latest_completed_trading_day="2026-08-26",
    )
    with (
        patch(
            "scripts.portfolio_premarket_readiness.load_portfolio_config",
            return_value=PORTFOLIO,
        ),
        patch(
            "scripts.portfolio_premarket_readiness.inspect_premarket_snapshot",
            return_value=result,
        ),
    ):
        assert readiness_main() == 0

    assert capsys.readouterr().out.splitlines() == [
        "PREMARKET_FRESH=true",
        "reason=already_fresh",
    ]
    output = github_output.read_text(encoding="utf-8")
    assert "fresh=true\n" in output
    assert "should_generate=false\n" in output
    assert "reason=already_fresh\n" in output
