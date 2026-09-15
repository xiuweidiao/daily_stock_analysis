#!/usr/bin/env python3
"""Audit and repair canonical portfolio snapshots for recent A-share sessions."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_config import DEFAULT_CONFIG_PATH, PortfolioConfig, load_portfolio_config
from scripts.portfolio_intraday_history import AkShareSinaIntradayProvider, HistoricalIntradayProvider
from scripts.portfolio_market_data import FreeProjectSources, OFFICIAL_OUTPUT_DIR, build_payload
from scripts.portfolio_midday_reconstruction import reconstruct_midday_snapshot
from scripts.portfolio_phase_policy import SHANGHAI_TZ, as_shanghai_time
from scripts.portfolio_snapshot_store import (
    SnapshotStoreError,
    freeze_universe,
    materialize_compatibility,
    persist_snapshot,
    portfolio_from_universe,
    record_phase_issue,
    resolve_historical_universe,
)
from scripts.validate_portfolio_snapshot import SnapshotContractError, validate_snapshot_contract
from src.core.trading_calendar import is_market_open


LOGGER = logging.getLogger("portfolio_snapshot_repair")


@dataclass(frozen=True)
class PhaseRepairResult:
    trading_date: str
    phase: str
    state: str
    generation_mode: str | None
    path: str | None
    reason: str | None = None
    provider: str | None = None


def previous_trading_day(value: date) -> date:
    candidate = value - timedelta(days=1)
    for _ in range(370):
        if is_market_open("cn", candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise SnapshotStoreError(f"could not resolve prior A-share session for {value}")


def recent_trading_days(now: datetime, count: int) -> list[date]:
    current = as_shanghai_time(now)
    candidate = current.date()
    if current.timetz().replace(tzinfo=None) < time(15, 0):
        candidate -= timedelta(days=1)
    result: list[date] = []
    for _ in range(370):
        if is_market_open("cn", candidate):
            result.append(candidate)
            if len(result) == count:
                return list(reversed(result))
        candidate -= timedelta(days=1)
    raise SnapshotStoreError(f"could not resolve {count} recent A-share sessions")


class SnapshotRepair:
    def __init__(
        self,
        *,
        root: Path = OFFICIAL_OUTPUT_DIR,
        repo_root: Path = REPOSITORY_ROOT,
        sources: Any | None = None,
        intraday_provider: HistoricalIntradayProvider | None = None,
    ) -> None:
        self.root = root
        self.repo_root = repo_root
        self.sources = sources or FreeProjectSources()
        self.intraday_provider = intraday_provider or AkShareSinaIntradayProvider()

    def _valid_existing(
        self, trading_date: date, phase: str, portfolio: PortfolioConfig, now: datetime
    ) -> bool:
        path = self.root / "snapshots" / trading_date.isoformat() / f"{phase}.json"
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            mode = str(payload.get("generation_mode") or "live")
            expected = previous_trading_day(trading_date) if phase == "premarket" else trading_date
            validate_snapshot_contract(
                payload,
                phase=phase,
                portfolio=portfolio,
                now=now,
                max_generation_age=None,
                target_date=trading_date,
                expected_data_date=expected,
                generation_mode=mode,
            )
            return True
        except (OSError, json.JSONDecodeError, SnapshotContractError, ValueError):
            return False

    def repair_phase(
        self,
        trading_date: date,
        phase: str,
        *,
        universe: dict[str, Any],
        now: datetime,
    ) -> PhaseRepairResult:
        portfolio = portfolio_from_universe(universe)
        if self._valid_existing(trading_date, phase, portfolio, now):
            path = self.root / "snapshots" / trading_date.isoformat() / f"{phase}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            manifest_path = path.parent / "manifest.json"
            manifest = (
                json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists()
                else None
            )
            phase_state = (manifest or {}).get("phases", {}).get(phase, {})
            health_path = self.root / "pipeline_health.json"
            health = (
                json.loads(health_path.read_text(encoding="utf-8"))
                if health_path.exists()
                else None
            )
            expected_path = str(
                Path("data/portfolio/snapshots")
                / trading_date.isoformat()
                / f"{phase}.json"
            )
            manifest_matches = (
                phase_state.get("lifecycle_status") == "available"
                and phase_state.get("path") == expected_path
                and phase_state.get("snapshot_as_of") == payload.get("snapshot_as_of")
                and phase_state.get("generation_mode")
                == payload.get("generation_mode")
            )
            health_covers_day = bool(
                health
                and str(health.get("latest_trading_date") or "")
                >= trading_date.isoformat()
            )
            if not manifest_matches or not health_covers_day:
                persist_snapshot(
                    self.root,
                    payload,
                    portfolio=portfolio,
                    now=now,
                    universe=universe,
                    allow_repair=True,
                )
            else:
                materialize_compatibility(self.root, payload)
            return PhaseRepairResult(
                trading_date.isoformat(),
                phase,
                "SNAPSHOT_ALREADY_FRESH",
                None,
                str(path),
                provider=str(payload.get("provenance", {}).get("provider") or "existing"),
            )
        mode = "reconstructed" if phase == "midday" else "recovery"
        expected = previous_trading_day(trading_date) if phase == "premarket" else trading_date
        if phase == "midday":
            payload = reconstruct_midday_snapshot(
                target_date=trading_date,
                portfolio=portfolio,
                sources=self.sources,
                now=now,
                provider=self.intraday_provider,
            )
        else:
            payload = build_payload(
                phase,
                sources=self.sources,
                portfolio=portfolio,
                now=now,
                target_date=trading_date,
                expected_data_date=expected,
                generation_mode="recovery",
            )
        required_quote_fields = (
            "latest_price", "prev_close", "open", "high", "low", "volume", "amount"
        )
        missing_required = any(
            item.get(field) is None
            for item in payload.get("stocks", [])
            for field in required_quote_fields
        )
        if payload.get("errors") or missing_required:
            reason = "; ".join(
                str(item.get("message") or item)
                for item in payload.get("errors", [])
            ) or "required reconstructed fields are unavailable"
            lifecycle = "missed" if phase == "midday" else "error"
            record_phase_issue(
                self.root,
                trading_date,
                phase,
                lifecycle_status=lifecycle,
                reason=reason,
                now=now,
            )
            return PhaseRepairResult(
                trading_date.isoformat(),
                phase,
                "SNAPSHOT_MISSED" if phase == "midday" else "GENERATION_FAILED",
                mode,
                None,
                reason,
                "akshare/sina_stock_zh_a_minute"
                if phase == "midday"
                else "project_daily_fallback_chain",
            )
        try:
            validate_snapshot_contract(
                payload,
                phase=phase,
                portfolio=portfolio,
                now=now,
                max_generation_age=None,
                target_date=trading_date,
                expected_data_date=expected,
                generation_mode=mode,
            )
            result = persist_snapshot(
                self.root,
                payload,
                portfolio=portfolio,
                now=now,
                universe=universe,
                allow_repair=True,
            )
            return PhaseRepairResult(
                trading_date.isoformat(),
                phase,
                "SNAPSHOT_RECONSTRUCTED" if phase == "midday" else "SNAPSHOT_GENERATED",
                mode,
                str(result.canonical_path),
                provider=(
                    "akshare/sina_stock_zh_a_minute"
                    if phase == "midday"
                    else "project_daily_fallback_chain"
                ),
            )
        except (SnapshotStoreError, SnapshotContractError):
            raise

    def repair_day(
        self,
        trading_date: date,
        *,
        phases: Sequence[str],
        current_portfolio: PortfolioConfig,
        now: datetime,
    ) -> list[PhaseRepairResult]:
        universe_path = self.root / "universe" / f"{trading_date.isoformat()}.json"
        day_path = self.root / "snapshots" / trading_date.isoformat()
        if universe_path.exists() or day_path.exists():
            universe = resolve_historical_universe(
                self.root,
                trading_date,
                repo_root=self.repo_root,
                captured_at=now,
            )
        elif trading_date == as_shanghai_time(now).date():
            universe = freeze_universe(
                self.root, trading_date, current_portfolio, captured_at=now
            )
        else:
            universe = resolve_historical_universe(
                self.root,
                trading_date,
                repo_root=self.repo_root,
                captured_at=now,
            )
        return [
            self.repair_phase(
                trading_date, phase, universe=universe, now=as_shanghai_time(now)
            )
            for phase in phases
        ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("premarket", "midday", "close", "all"), default="all")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--target-date", type=date.fromisoformat)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--root", type=Path, default=OFFICIAL_OUTPUT_DIR)
    parser.add_argument("--result-path", type=Path)
    parser.add_argument("--now", help="ISO-8601 test/diagnostic clock")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    now = as_shanghai_time(
        datetime.fromisoformat(args.now) if args.now else datetime.now(SHANGHAI_TZ)
    )
    if args.days < 1:
        LOGGER.error("--days must be positive")
        return 2
    try:
        portfolio = load_portfolio_config(args.config)
        days = [args.target_date] if args.target_date else recent_trading_days(now, args.days)
        phases = ("premarket", "midday", "close") if args.phase == "all" else (args.phase,)
        repair = SnapshotRepair(root=args.root)
        results = []
        for trading_date in days:
            if trading_date is None or not is_market_open("cn", trading_date):
                continue
            results.extend(
                repair.repair_day(
                    trading_date,
                    phases=phases,
                    current_portfolio=portfolio,
                    now=now,
                )
            )
        rendered = json.dumps([asdict(item) for item in results], ensure_ascii=False)
        if args.result_path:
            args.result_path.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
    except SnapshotStoreError as exc:
        LOGGER.error("repair state failure: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
