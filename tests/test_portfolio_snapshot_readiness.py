from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.check_portfolio_snapshot_ready import check_snapshot_ready, main
from scripts.portfolio_config import PortfolioConfig
from scripts.portfolio_market_data import _phase_data_date


SHANGHAI = ZoneInfo("Asia/Shanghai")
PORTFOLIO = PortfolioConfig(
    version=1,
    holdings=("688825",),
    watchlist=("600519",),
)
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


@pytest.fixture(autouse=True)
def _stable_trading_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.portfolio_market_data.is_market_open", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "scripts.validate_portfolio_snapshot.is_market_open",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "scripts.portfolio_market_data.get_effective_trading_date",
        lambda *_args, **_kwargs: date(2026, 8, 19),
    )


def _payload(phase: str, generated_at: datetime, data_date: date | None = None) -> dict:
    expected_date = data_date or _phase_data_date(phase, generated_at)
    return {
        "generated_at": generated_at.isoformat(),
        "timezone": "Asia/Shanghai",
        "market_phase": phase,
        "data_date": expected_date.isoformat(),
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


def _write(path: Path, payload: dict) -> bytes:
    content = json.dumps(payload, ensure_ascii=False).encode()
    path.write_bytes(content)
    return content


@pytest.mark.parametrize(
    ("phase", "generated_at", "checked_at"),
    (
        (
            "premarket",
            datetime(2026, 8, 20, 6, 37, tzinfo=SHANGHAI),
            datetime(2026, 8, 20, 8, 0, tzinfo=SHANGHAI),
        ),
        (
            "midday",
            datetime(2026, 8, 20, 11, 34, tzinfo=SHANGHAI),
            datetime(2026, 8, 20, 12, 15, tzinfo=SHANGHAI),
        ),
        (
            "close",
            datetime(2026, 8, 20, 15, 10, tzinfo=SHANGHAI),
            datetime(2026, 8, 20, 16, 0, tzinfo=SHANGHAI),
        ),
    ),
)
def test_today_valid_snapshot_is_ready_without_modification(
    tmp_path: Path, phase: str, generated_at: datetime, checked_at: datetime
) -> None:
    path = tmp_path / f"{phase}.json"
    original = _write(path, _payload(phase, generated_at))

    result = check_snapshot_ready(
        path, phase=phase, portfolio=PORTFOLIO, now=checked_at
    )

    assert result.ready is True
    assert result.reason == "ok"
    assert result.generated_at == generated_at.isoformat()
    assert path.read_bytes() == original


def test_yesterday_snapshot_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "midday.json"
    generated_at = datetime(2026, 8, 19, 11, 34, tzinfo=SHANGHAI)
    _write(path, _payload("midday", generated_at))

    result = check_snapshot_ready(
        path,
        phase="midday",
        portfolio=PORTFOLIO,
        now=datetime(2026, 8, 20, 11, 45, tzinfo=SHANGHAI),
    )

    assert result.ready is False
    assert result.reason == "stale_snapshot"


def test_phase_mismatch_is_not_ready(tmp_path: Path) -> None:
    path = tmp_path / "midday.json"
    generated_at = datetime(2026, 8, 20, 15, 10, tzinfo=SHANGHAI)
    _write(path, _payload("close", generated_at))

    result = check_snapshot_ready(
        path,
        phase="midday",
        portfolio=PORTFOLIO,
        now=datetime(2026, 8, 20, 15, 20, tzinfo=SHANGHAI),
    )

    assert result.ready is False
    assert result.reason == "invalid_snapshot"


def test_data_date_mismatch_is_not_ready(tmp_path: Path) -> None:
    path = tmp_path / "midday.json"
    generated_at = datetime(2026, 8, 20, 11, 34, tzinfo=SHANGHAI)
    _write(path, _payload("midday", generated_at, date(2026, 8, 19)))

    result = check_snapshot_ready(
        path,
        phase="midday",
        portfolio=PORTFOLIO,
        now=datetime(2026, 8, 20, 11, 45, tzinfo=SHANGHAI),
    )

    assert result.ready is False
    assert result.reason == "invalid_snapshot"


def test_generated_at_outside_phase_window_is_not_ready(tmp_path: Path) -> None:
    path = tmp_path / "midday.json"
    generated_at = datetime(2026, 8, 20, 13, 1, tzinfo=SHANGHAI)
    _write(path, _payload("midday", generated_at, date(2026, 8, 20)))

    result = check_snapshot_ready(
        path,
        phase="midday",
        portfolio=PORTFOLIO,
        now=datetime(2026, 8, 20, 13, 5, tzinfo=SHANGHAI),
    )

    assert result.ready is False
    assert result.reason == "invalid_snapshot"


def test_missing_file_is_not_ready(tmp_path: Path) -> None:
    result = check_snapshot_ready(
        tmp_path / "midday.json",
        phase="midday",
        portfolio=PORTFOLIO,
        now=datetime(2026, 8, 20, 11, 45, tzinfo=SHANGHAI),
    )

    assert result.ready is False
    assert result.reason == "missing_snapshot"


def test_contract_failure_is_not_ready(tmp_path: Path) -> None:
    path = tmp_path / "midday.json"
    generated_at = datetime(2026, 8, 20, 11, 34, tzinfo=SHANGHAI)
    payload = _payload("midday", generated_at)
    payload["stocks"][0]["latest_price"] = None
    _write(path, payload)

    result = check_snapshot_ready(
        path,
        phase="midday",
        portfolio=PORTFOLIO,
        now=datetime(2026, 8, 20, 11, 45, tzinfo=SHANGHAI),
    )

    assert result.ready is False
    assert result.reason == "invalid_snapshot"


def test_portfolio_config_pool_mismatch_is_not_ready(tmp_path: Path) -> None:
    path = tmp_path / "midday.json"
    generated_at = datetime(2026, 8, 20, 11, 34, tzinfo=SHANGHAI)
    payload = _payload("midday", generated_at)
    payload["holdings_codes"] = ["159567"]
    _write(path, payload)

    result = check_snapshot_ready(
        path,
        phase="midday",
        portfolio=PORTFOLIO,
        now=datetime(2026, 8, 20, 11, 45, tzinfo=SHANGHAI),
    )

    assert result.ready is False
    assert result.reason == "invalid_snapshot"


def test_cli_outputs_structured_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    generated_at = datetime(2026, 8, 20, 11, 34, tzinfo=SHANGHAI)
    snapshot_path = tmp_path / "midday.json"
    config_path = tmp_path / "portfolio.json"
    _write(snapshot_path, _payload("midday", generated_at))
    config_path.write_text(
        json.dumps(PORTFOLIO.as_dict(), ensure_ascii=False), encoding="utf-8"
    )

    assert (
        main(
            [
                "--phase",
                "midday",
                "--path",
                str(snapshot_path),
                "--config",
                str(config_path),
            ],
            now=datetime(2026, 8, 20, 11, 45, tzinfo=SHANGHAI),
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "phase": "midday",
        "ready": True,
        "reason": "ok",
        "generated_at": generated_at.isoformat(),
        "data_date": "2026-08-20",
    }
