#!/usr/bin/env python3
"""Canonical per-trading-day portfolio snapshot store and derived indexes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.portfolio_config import PortfolioConfig, validate_portfolio_config
from scripts.portfolio_phase_policy import as_shanghai_time


SCHEMA_VERSION = "2.0"
PHASES = ("premarket", "midday", "close")


class SnapshotStoreError(ValueError):
    """Raised when canonical state is corrupt or conflicts with frozen history."""


@dataclass(frozen=True)
class StoreWriteResult:
    canonical_path: Path
    manifest_path: Path
    compatibility_updated: bool
    canonical_changed: bool


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotStoreError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SnapshotStoreError(f"JSON root must be an object: {path}")
    return value


def universe_hash(holdings: list[str], watchlist: list[str]) -> str:
    body = json.dumps(
        {"holdings_codes": holdings, "watchlist_codes": watchlist},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_universe(
    portfolio: PortfolioConfig,
    trading_date: date,
    *,
    captured_at: datetime,
    source_type: str = "frozen",
    confidence: str = "authoritative",
    source_commit: str | None = None,
) -> dict[str, Any]:
    if source_type not in {"frozen", "inferred_snapshot", "inferred_git"}:
        raise SnapshotStoreError(f"unsupported universe source_type: {source_type}")
    if confidence not in {"authoritative", "inferred"}:
        raise SnapshotStoreError(f"unsupported universe confidence: {confidence}")
    holdings = list(portfolio.holdings)
    watchlist = list(portfolio.watchlist)
    return {
        "schema_version": SCHEMA_VERSION,
        "trading_date": trading_date.isoformat(),
        "captured_at": as_shanghai_time(captured_at).isoformat(),
        "source_type": source_type,
        "confidence": confidence,
        "source_commit": source_commit,
        "holdings_codes": holdings,
        "watchlist_codes": watchlist,
        "universe_hash": universe_hash(holdings, watchlist),
        "frozen": True,
    }


def portfolio_from_universe(payload: Mapping[str, Any]) -> PortfolioConfig:
    return validate_portfolio_config(
        {
            "version": 1,
            "holdings": payload.get("holdings_codes"),
            "watchlist": payload.get("watchlist_codes"),
        }
    )


def _validate_universe(payload: Mapping[str, Any], trading_date: date) -> None:
    if payload.get("trading_date") != trading_date.isoformat():
        raise SnapshotStoreError("historical universe trading_date mismatch")
    portfolio = portfolio_from_universe(payload)
    expected_hash = universe_hash(list(portfolio.holdings), list(portfolio.watchlist))
    if payload.get("universe_hash") != expected_hash:
        raise SnapshotStoreError("historical universe hash mismatch")


def freeze_universe(
    root: Path,
    trading_date: date,
    portfolio: PortfolioConfig,
    *,
    captured_at: datetime,
) -> dict[str, Any]:
    path = root / "universe" / f"{trading_date.isoformat()}.json"
    existing = _load_json(path)
    if existing is not None:
        _validate_universe(existing, trading_date)
        existing_portfolio = portfolio_from_universe(existing)
        if existing_portfolio != portfolio:
            raise SnapshotStoreError(
                "canonical conflict: current config differs from the frozen daily universe"
            )
        return existing
    payload = build_universe(portfolio, trading_date, captured_at=captured_at)
    _atomic_json(path, payload)
    return payload


def resolve_historical_universe(
    root: Path,
    trading_date: date,
    *,
    repo_root: Path,
    captured_at: datetime,
    git_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Resolve history without ever silently substituting today's config."""
    universe_path = root / "universe" / f"{trading_date.isoformat()}.json"
    existing = _load_json(universe_path)
    if existing is not None:
        _validate_universe(existing, trading_date)
        return existing

    day_dir = root / "snapshots" / trading_date.isoformat()
    for phase in PHASES:
        snapshot = _load_json(day_dir / f"{phase}.json")
        if snapshot is None:
            continue
        try:
            portfolio = validate_portfolio_config(
                {
                    "version": 1,
                    "holdings": snapshot.get("holdings_codes"),
                    "watchlist": snapshot.get("watchlist_codes"),
                }
            )
        except ValueError:
            continue
        inferred = build_universe(
            portfolio,
            trading_date,
            captured_at=captured_at,
            source_type="inferred_snapshot",
            confidence="inferred",
        )
        _atomic_json(universe_path, inferred)
        return inferred

    before = f"{trading_date.isoformat()} 23:59:59 +0800"
    revision = git_runner(
        ["git", "rev-list", "-1", f"--before={before}", "HEAD", "--", "config/portfolio.json"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    commit = revision.stdout.strip()
    if commit:
        shown = git_runner(
            ["git", "show", f"{commit}:config/portfolio.json"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if shown.returncode == 0:
            try:
                portfolio = validate_portfolio_config(json.loads(shown.stdout))
            except (json.JSONDecodeError, ValueError) as exc:
                raise SnapshotStoreError(f"invalid historical portfolio config: {exc}") from exc
            inferred = build_universe(
                portfolio,
                trading_date,
                captured_at=captured_at,
                source_type="inferred_git",
                confidence="inferred",
                source_commit=commit,
            )
            _atomic_json(universe_path, inferred)
            return inferred
    raise SnapshotStoreError(
        f"HISTORICAL_UNIVERSE_UNAVAILABLE for {trading_date.isoformat()}"
    )


def _manifest(root: Path, trading_date: date) -> dict[str, Any]:
    path = root / "snapshots" / trading_date.isoformat() / "manifest.json"
    payload = _load_json(path)
    if payload is None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "trading_date": trading_date.isoformat(),
            "expected_phases": list(PHASES),
            "phases": {
                phase: {
                    "lifecycle_status": "pending",
                    "snapshot_status": None,
                    "completeness": None,
                    "generation_mode": None,
                    "snapshot_as_of": None,
                    "information_cutoff": None,
                    "blocking": None,
                    "warnings_count": 0,
                    "path": None,
                    "reason": None,
                }
                for phase in PHASES
            },
        }
    if payload.get("trading_date") != trading_date.isoformat() or not isinstance(
        payload.get("phases"), dict
    ):
        raise SnapshotStoreError(f"manifest corruption for {trading_date}")
    return payload


def _as_of(payload: Mapping[str, Any]) -> datetime | None:
    value = payload.get("snapshot_as_of")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return as_shanghai_time(parsed)


def _compatibility_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(payload))
    if "stocks" not in result:
        result["stocks"] = [
            *deepcopy(result.get("holdings", [])),
            *deepcopy(result.get("watchlist", [])),
        ]
    return result


def _advance_latest(root: Path, phase: str, payload: Mapping[str, Any]) -> bool:
    latest_path = root / "latest" / f"{phase}.json"
    existing = _load_json(latest_path)
    compatibility = _compatibility_payload(payload)
    candidate_time = _as_of(payload)
    existing_time = _as_of(existing or {})
    if candidate_time is None:
        return False
    if existing is not None and existing_time is not None and candidate_time < existing_time:
        return False
    root_path = root / f"{phase}.json"
    root_payload = _load_json(root_path)
    changed = existing != compatibility or root_payload != compatibility
    if changed:
        _atomic_json(latest_path, compatibility)
        _atomic_json(root_path, compatibility)
    return changed


def materialize_compatibility(root: Path, payload: Mapping[str, Any]) -> bool:
    """Repair derived latest/root copies without mutating canonical lifecycle."""
    phase = str(payload.get("market_phase"))
    if phase not in PHASES:
        raise SnapshotStoreError(f"unsupported compatibility phase: {phase}")
    return _advance_latest(root, phase, payload)


def _update_health(root: Path, trading_date: date, manifest: Mapping[str, Any], now: datetime) -> None:
    health_path = root / "pipeline_health.json"
    current = _load_json(health_path) or {}
    previous_date = current.get("latest_trading_date")
    if isinstance(previous_date, str) and previous_date > trading_date.isoformat():
        return
    phases = {}
    issues = []
    for phase, state in manifest["phases"].items():
        phases[phase] = deepcopy(state)
        if state.get("lifecycle_status") in {"missed", "error"}:
            issues.append(
                {
                    "phase": phase,
                    "reason": state.get("reason") or "PHASE_DATA_UNAVAILABLE",
                }
            )
        elif state.get("snapshot_status") == "partial":
            issues.append({"phase": phase, "reason": "CORE_FIELDS_UNAVAILABLE"})
        elif state.get("completeness") == "partial":
            issues.append({"phase": phase, "reason": "OPTIONAL_FIELDS_UNAVAILABLE"})
    _atomic_json(
        health_path,
        {
            "schema_version": SCHEMA_VERSION,
            "checked_at": as_shanghai_time(now).isoformat(),
            "latest_trading_date": trading_date.isoformat(),
            "manifest_path": str(
                Path("data/portfolio/snapshots") / trading_date.isoformat() / "manifest.json"
            ),
            "status": "degraded" if issues else "ok",
            "phases": phases,
            "issues": issues,
            "last_successful_commit": None,
        },
    )


def persist_snapshot(
    root: Path,
    payload: Mapping[str, Any],
    *,
    portfolio: PortfolioConfig,
    now: datetime,
    universe: Mapping[str, Any] | None = None,
    allow_repair: bool = False,
) -> StoreWriteResult:
    phase = str(payload.get("market_phase"))
    if phase not in PHASES:
        raise SnapshotStoreError(f"unsupported canonical phase: {phase}")
    trading_date = date.fromisoformat(str(payload.get("trading_date") or payload.get("data_date")))
    if universe is None:
        universe = freeze_universe(root, trading_date, portfolio, captured_at=now)
    _validate_universe(universe, trading_date)
    frozen_portfolio = portfolio_from_universe(universe)
    if frozen_portfolio != portfolio:
        raise SnapshotStoreError("snapshot portfolio does not match historical universe")

    canonical = deepcopy(dict(payload))
    canonical["schema_version"] = SCHEMA_VERSION
    canonical["trading_date"] = trading_date.isoformat()
    canonical["universe"] = {
        "path": str(Path("data/portfolio/universe") / f"{trading_date}.json"),
        "hash": universe["universe_hash"],
        "source_type": universe["source_type"],
        "confidence": universe["confidence"],
    }
    stocks = canonical.get("stocks", [])
    canonical["holdings"] = [
        deepcopy(item) for item in stocks if item.get("tracking_type") == "holding"
    ]
    canonical["watchlist"] = [
        deepcopy(item) for item in stocks if item.get("tracking_type") == "watchlist"
    ]
    canonical_path = root / "snapshots" / trading_date.isoformat() / f"{phase}.json"
    existing = _load_json(canonical_path)
    changed = existing != canonical
    if existing is not None and changed and not allow_repair:
        raise SnapshotStoreError(
            f"canonical conflict: {canonical_path} already contains a different snapshot"
        )
    if changed:
        _atomic_json(canonical_path, canonical)

    manifest = _manifest(root, trading_date)
    relative = Path("data/portfolio/snapshots") / trading_date.isoformat() / f"{phase}.json"
    manifest["phases"][phase] = {
        "lifecycle_status": "available",
        "snapshot_status": canonical.get("status", "ok"),
        "completeness": canonical.get("completeness", "full"),
        "generation_mode": canonical.get("generation_mode", "live"),
        "snapshot_as_of": canonical.get("snapshot_as_of"),
        "information_cutoff": canonical.get("information_cutoff"),
        "blocking": canonical.get("blocking"),
        "warnings_count": (
            canonical.get("data_quality", {}).get("warnings_count", 0)
            if isinstance(canonical.get("data_quality"), Mapping)
            else 0
        ),
        "path": str(relative),
        "reason": None,
    }
    manifest["updated_at"] = as_shanghai_time(now).isoformat()
    manifest_path = canonical_path.parent / "manifest.json"
    _atomic_json(manifest_path, manifest)
    compatibility_updated = _advance_latest(root, phase, canonical)
    _update_health(root, trading_date, manifest, now)
    return StoreWriteResult(canonical_path, manifest_path, compatibility_updated, changed)


def record_phase_issue(
    root: Path,
    trading_date: date,
    phase: str,
    *,
    lifecycle_status: str,
    reason: str,
    now: datetime,
) -> Path:
    if phase not in PHASES or lifecycle_status not in {"missed", "error"}:
        raise SnapshotStoreError("invalid phase issue")
    manifest = _manifest(root, trading_date)
    manifest["phases"][phase] = {
        "lifecycle_status": lifecycle_status,
        "snapshot_status": None,
        "completeness": None,
        "generation_mode": None,
        "snapshot_as_of": None,
        "information_cutoff": None,
        "blocking": None,
        "warnings_count": 0,
        "path": None,
        "reason": reason,
    }
    manifest["updated_at"] = as_shanghai_time(now).isoformat()
    path = root / "snapshots" / trading_date.isoformat() / "manifest.json"
    _atomic_json(path, manifest)
    _update_health(root, trading_date, manifest, now)
    return path
