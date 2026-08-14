#!/usr/bin/env python3
"""Manage holding and watchlist codes without editing Python source files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_config import (
    DEFAULT_CONFIG_PATH,
    PortfolioConfig,
    PortfolioConfigError,
    load_portfolio_config,
    validate_security_code,
    write_portfolio_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "add-holding",
        "remove-holding",
        "add-watchlist",
        "remove-watchlist",
    ):
        subparsers.add_parser(command).add_argument("code")
    subparsers.add_parser("show")
    return parser


def _updated_config(config: PortfolioConfig, command: str, code: str) -> PortfolioConfig:
    holdings = list(config.holdings)
    watchlist = list(config.watchlist)
    if command == "add-holding":
        if code in watchlist:
            watchlist.remove(code)
        if code not in holdings:
            holdings.append(code)
    elif command == "remove-holding":
        if code in holdings:
            holdings.remove(code)
    elif command == "add-watchlist":
        if code in holdings:
            raise PortfolioConfigError(f"{code} is already a holding")
        if code not in watchlist:
            watchlist.append(code)
    elif command == "remove-watchlist":
        if code in watchlist:
            watchlist.remove(code)
    else:  # guarded by argparse
        raise PortfolioConfigError(f"unsupported command: {command}")
    return PortfolioConfig(
        version=config.version,
        holdings=tuple(holdings),
        watchlist=tuple(watchlist),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_portfolio_config(args.config)
        if args.command == "show":
            print(json.dumps(config.as_dict(), ensure_ascii=False, indent=2))
            return 0
        code = validate_security_code(args.code)
        updated = _updated_config(config, args.command, code)
        write_portfolio_config(updated, args.config)
        print(json.dumps(updated.as_dict(), ensure_ascii=False, indent=2))
        return 0
    except PortfolioConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
