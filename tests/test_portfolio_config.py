from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.manage_portfolio import main as manage_portfolio
from scripts.portfolio_config import (
    PortfolioConfig,
    PortfolioConfigError,
    load_portfolio_config,
    validate_portfolio_config,
    write_portfolio_config,
)


def _write_config(
    path: Path,
    *,
    holdings: tuple[str, ...] = ("688012",),
    watchlist: tuple[str, ...] = (),
) -> None:
    write_portfolio_config(
        PortfolioConfig(version=1, holdings=holdings, watchlist=watchlist), path
    )


def test_duplicate_codes_within_a_category_are_deduplicated() -> None:
    config = validate_portfolio_config(
        {"version": 1, "holdings": ["688012", "688012"], "watchlist": []}
    )

    assert config.holdings == ("688012",)


def test_code_in_holdings_and_watchlist_is_rejected() -> None:
    with pytest.raises(PortfolioConfigError, match="both holdings and watchlist"):
        validate_portfolio_config(
            {"version": 1, "holdings": ["688012"], "watchlist": ["688012"]}
        )


@pytest.mark.parametrize("code", ("60051", "sh600519", 600519, "ABCDEF"))
def test_invalid_security_code_is_rejected(code: object) -> None:
    with pytest.raises(PortfolioConfigError, match="exactly six digits"):
        validate_portfolio_config(
            {"version": 1, "holdings": [code], "watchlist": []}
        )


def test_missing_config_is_explicit_error(tmp_path: Path) -> None:
    with pytest.raises(PortfolioConfigError, match="config not found"):
        load_portfolio_config(tmp_path / "missing.json")


def test_malformed_config_is_explicit_error(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.json"
    path.write_text('{"version": 1,', encoding="utf-8")

    with pytest.raises(PortfolioConfigError, match="malformed portfolio config JSON"):
        load_portfolio_config(path)


def test_private_or_unknown_fields_are_rejected() -> None:
    with pytest.raises(PortfolioConfigError, match="unsupported fields"):
        validate_portfolio_config(
            {
                "version": 1,
                "holdings": ["688012"],
                "watchlist": [],
                "holding_cost": 100,
            }
        )


def test_add_holding_moves_code_out_of_watchlist(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.json"
    _write_config(path, holdings=(), watchlist=("600519",))

    assert manage_portfolio(["--config", str(path), "add-holding", "600519"]) == 0
    config = load_portfolio_config(path)
    assert config.holdings == ("600519",)
    assert config.watchlist == ()


def test_remove_holding_does_not_add_watchlist(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.json"
    _write_config(path, holdings=("688012",))

    assert manage_portfolio(["--config", str(path), "remove-holding", "688012"]) == 0
    config = load_portfolio_config(path)
    assert config.holdings == ()
    assert config.watchlist == ()


def test_add_and_remove_watchlist(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.json"
    _write_config(path)

    assert manage_portfolio(["--config", str(path), "add-watchlist", "002594"]) == 0
    assert load_portfolio_config(path).watchlist == ("002594",)
    assert manage_portfolio(["--config", str(path), "remove-watchlist", "002594"]) == 0
    assert load_portfolio_config(path).watchlist == ()


def test_add_watchlist_rejects_existing_holding_without_rewriting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "portfolio.json"
    _write_config(path, holdings=("688012",))
    before = path.read_bytes()

    assert manage_portfolio(["--config", str(path), "add-watchlist", "688012"]) == 2
    assert "already a holding" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_repeated_add_does_not_create_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.json"
    _write_config(path, holdings=())

    assert manage_portfolio(["--config", str(path), "add-holding", "600519"]) == 0
    assert manage_portfolio(["--config", str(path), "add-holding", "600519"]) == 0
    assert load_portfolio_config(path).holdings == ("600519",)


def test_management_command_rejects_invalid_code_without_rewriting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.json"
    _write_config(path)
    before = path.read_bytes()

    assert manage_portfolio(["--config", str(path), "add-holding", "sh600519"]) == 2
    assert path.read_bytes() == before


def test_show_outputs_stable_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "portfolio.json"
    _write_config(path, holdings=("688012",), watchlist=("002594",))

    assert manage_portfolio(["--config", str(path), "show"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "version": 1,
        "holdings": ["688012"],
        "watchlist": ["002594"],
    }
