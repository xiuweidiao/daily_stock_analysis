#!/usr/bin/env python3
"""Check whether a committed portfolio snapshot is ready for consumption."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_config import (
    DEFAULT_CONFIG_PATH,
    PortfolioConfig,
    PortfolioConfigError,
    load_portfolio_config,
)
from scripts.portfolio_market_data import (
    OFFICIAL_OUTPUT_DIR,
    REPORT_PHASES,
    TIMEZONE_NAME,
    _phase_data_date,
)
from scripts.portfolio_phase_policy import (
    SHANGHAI_TZ,
    PhaseTimeError,
    as_shanghai_time,
    validate_phase_time,
)
from scripts.validate_portfolio_snapshot import (
    SnapshotContractError,
    _parse_generated_at,
    validate_snapshot_contract,
)


@dataclass(frozen=True)
class SnapshotReadiness:
    phase: str
    ready: bool
    reason: str
    generated_at: str | None
    data_date: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _not_ready(
    phase: str,
    reason: str,
    *,
    payload: Mapping[str, Any] | None = None,
) -> SnapshotReadiness:
    return SnapshotReadiness(
        phase=phase,
        ready=False,
        reason=reason,
        generated_at=(
            payload.get("generated_at")
            if payload and isinstance(payload.get("generated_at"), str)
            else None
        ),
        data_date=(
            payload.get("data_date")
            if payload and isinstance(payload.get("data_date"), str)
            else None
        ),
    )


def check_snapshot_ready(
    path: Path,
    *,
    phase: str,
    portfolio: PortfolioConfig,
    now: datetime | None = None,
) -> SnapshotReadiness:
    """Return a read-only readiness decision for one committed snapshot file."""
    if phase not in REPORT_PHASES:
        raise ValueError(f"unsupported report phase: {phase}")
    current = as_shanghai_time(now or datetime.now(SHANGHAI_TZ))
    if not path.exists():
        return _not_ready(phase, "missing_snapshot")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _not_ready(phase, "invalid_snapshot")
    if not isinstance(payload, Mapping):
        return _not_ready(phase, "invalid_snapshot")

    if payload.get("market_phase") != phase:
        return _not_ready(phase, "invalid_snapshot", payload=payload)
    if payload.get("timezone") != TIMEZONE_NAME:
        return _not_ready(phase, "invalid_snapshot", payload=payload)

    try:
        generated_at = _parse_generated_at(payload.get("generated_at"))
    except SnapshotContractError:
        return _not_ready(phase, "invalid_snapshot", payload=payload)
    if generated_at.date() != current.date():
        return _not_ready(phase, "stale_snapshot", payload=payload)

    try:
        validate_phase_time(phase, generated_at)
    except PhaseTimeError:
        return _not_ready(phase, "invalid_snapshot", payload=payload)

    expected_data_date = _phase_data_date(phase, generated_at).isoformat()
    if payload.get("data_date") != expected_data_date:
        return _not_ready(
            phase,
            "invalid_snapshot",
            payload=payload,
        )

    try:
        validate_snapshot_contract(
            payload,
            phase=phase,
            portfolio=portfolio,
            now=current,
            max_generation_age=None,
        )
    except SnapshotContractError:
        return _not_ready(phase, "invalid_snapshot", payload=payload)

    return SnapshotReadiness(
        phase=phase,
        ready=True,
        reason="ok",
        generated_at=payload["generated_at"],
        data_date=payload["data_date"],
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=REPORT_PHASES, required=True)
    parser.add_argument("--path", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None, *, now: datetime | None = None
) -> int:
    args = parse_args(argv)
    path = args.path or OFFICIAL_OUTPUT_DIR / f"{args.phase}.json"
    try:
        portfolio = load_portfolio_config(args.config)
    except PortfolioConfigError:
        result = _not_ready(args.phase, "invalid_snapshot")
    else:
        result = check_snapshot_ready(
            path, phase=args.phase, portfolio=portfolio, now=now
        )
    print(json.dumps(result.as_dict(), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
