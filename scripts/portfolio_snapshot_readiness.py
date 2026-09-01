#!/usr/bin/env python3
"""Unified read-only freshness decision for formal portfolio snapshots."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_config import (
    DEFAULT_CONFIG_PATH,
    PortfolioConfig,
    load_portfolio_config,
)
from scripts.portfolio_market_data import OFFICIAL_OUTPUT_DIR, REPORT_PHASES
from scripts.portfolio_phase_policy import SHANGHAI_TZ, as_shanghai_time
from scripts.portfolio_schedule_context import build_schedule_context
from scripts.validate_portfolio_snapshot import (
    SnapshotContractError,
    _parse_generated_at,
    validate_snapshot_contract,
)
from src.core.trading_calendar import is_market_open


@dataclass(frozen=True)
class SnapshotReadiness:
    phase: str
    fresh: bool
    should_generate: bool
    reason: str
    state_code: str
    freshness: str
    generated_at: str | None
    data_date: str | None
    market_phase: str | None
    portfolio_status: str | None
    expected_data_date: str
    target_date: str
    current_beijing_time: str
    generation_mode: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _metadata(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _result(
    *,
    phase: str,
    fresh: bool,
    should_generate: bool,
    reason: str,
    payload: Mapping[str, Any],
    target_date: date,
    expected_data_date: date,
    current: datetime,
    generation_mode: str,
) -> SnapshotReadiness:
    if fresh:
        state_code = "SNAPSHOT_ALREADY_FRESH"
    elif reason == "non_trading_day":
        state_code = "NON_TRADING_DAY"
    else:
        state_code = "RECOVERY_REQUIRED"
    return SnapshotReadiness(
        phase=phase,
        fresh=fresh,
        should_generate=should_generate,
        reason=reason,
        state_code=state_code,
        freshness="fresh" if fresh else (
            "stale" if reason in {"missing_snapshot", "stale_snapshot"} else "invalid"
        ),
        generated_at=(
            payload.get("generated_at")
            if isinstance(payload.get("generated_at"), str)
            else None
        ),
        data_date=(
            payload.get("data_date")
            if isinstance(payload.get("data_date"), str)
            else None
        ),
        market_phase=(
            payload.get("market_phase")
            if isinstance(payload.get("market_phase"), str)
            else None
        ),
        portfolio_status=(
            payload.get("portfolio_status")
            if isinstance(payload.get("portfolio_status"), str)
            else None
        ),
        expected_data_date=expected_data_date.isoformat(),
        target_date=target_date.isoformat(),
        current_beijing_time=current.isoformat(),
        generation_mode=generation_mode,
    )


def inspect_snapshot(
    path: Path,
    *,
    phase: str,
    portfolio: PortfolioConfig,
    target_date: date,
    expected_data_date: date,
    generation_mode: str,
    now: datetime | None = None,
    target_is_trading_day: bool | None = None,
) -> SnapshotReadiness:
    """Inspect one snapshot without fetching data or modifying the repository."""
    if phase not in REPORT_PHASES:
        raise ValueError(f"unsupported formal phase: {phase}")
    current = as_shanghai_time(now or datetime.now(SHANGHAI_TZ))
    target_trading = (
        is_market_open("cn", target_date)
        if target_is_trading_day is None
        else target_is_trading_day
    )
    payload = _metadata(path)
    common = {
        "phase": phase,
        "payload": payload,
        "target_date": target_date,
        "expected_data_date": expected_data_date,
        "current": current,
        "generation_mode": generation_mode,
    }
    if phase in {"premarket", "midday"} and not target_trading:
        return _result(
            fresh=False,
            should_generate=False,
            reason="non_trading_day",
            **common,
        )
    if not path.exists():
        return _result(
            fresh=False,
            should_generate=True,
            reason="missing_snapshot",
            **common,
        )
    if not payload:
        return _result(
            fresh=False,
            should_generate=True,
            reason="invalid_snapshot",
            **common,
        )

    try:
        generated_at = _parse_generated_at(payload.get("generated_at"))
    except SnapshotContractError:
        generated_at = None
    if phase in {"premarket", "midday"} and generated_at is not None:
        if generated_at.date() < target_date:
            return _result(
                fresh=False,
                should_generate=True,
                reason="stale_snapshot",
                **common,
            )

    payload_data_date = payload.get("data_date")
    if isinstance(payload_data_date, str):
        try:
            parsed_data_date = date.fromisoformat(payload_data_date)
        except ValueError:
            parsed_data_date = None
        if (
            phase == "close"
            and parsed_data_date is not None
            and parsed_data_date < expected_data_date
        ):
            return _result(
                fresh=False,
                should_generate=True,
                reason="stale_snapshot",
                **common,
            )

    payload_mode = str(payload.get("generation_mode") or "live")
    validation_target = (
        expected_data_date if phase == "close" and payload_mode == "live" else target_date
    )
    try:
        validate_snapshot_contract(
            payload,
            phase=phase,
            portfolio=portfolio,
            now=current,
            max_generation_age=None,
            target_date=validation_target,
            expected_data_date=expected_data_date,
            generation_mode=payload_mode,
        )
    except SnapshotContractError:
        return _result(
            fresh=False,
            should_generate=True,
            reason="invalid_snapshot",
            **common,
        )
    return _result(
        fresh=True,
        should_generate=False,
        reason="already_fresh",
        **common,
    )


def _write_github_output(result: SnapshotReadiness) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        for key, value in result.as_dict().items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = "" if value is None else str(value)
            output.write(f"{key}={rendered}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=REPORT_PHASES, required=True)
    parser.add_argument("--path", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--target-date", type=date.fromisoformat)
    parser.add_argument("--expected-data-date", type=date.fromisoformat)
    parser.add_argument("--generation-mode", choices=("live", "recovery"))
    parser.add_argument("--schedule")
    parser.add_argument("--now", help="ISO-8601 test/diagnostic clock")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    current = datetime.fromisoformat(args.now) if args.now else datetime.now(SHANGHAI_TZ)
    context = build_schedule_context(
        phase=args.phase,
        current=current,
        schedule=args.schedule or None,
    )
    target_date = args.target_date or date.fromisoformat(context.target_date)
    expected_data_date = args.expected_data_date or date.fromisoformat(
        context.expected_data_date
    )
    generation_mode = args.generation_mode or context.generation_mode
    portfolio = load_portfolio_config(args.config)
    result = inspect_snapshot(
        args.path or OFFICIAL_OUTPUT_DIR / f"{args.phase}.json",
        phase=args.phase,
        portfolio=portfolio,
        target_date=target_date,
        expected_data_date=expected_data_date,
        generation_mode=generation_mode,
        now=current,
        target_is_trading_day=(
            is_market_open("cn", target_date)
            if args.target_date is not None
            else context.target_is_trading_day
        ),
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, separators=(",", ":")))
    _write_github_output(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
