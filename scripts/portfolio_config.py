"""Load, validate and atomically update the public portfolio code list."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config" / "portfolio.json"
ALLOWED_CONFIG_KEYS = {"version", "holdings", "watchlist"}
SECURITY_CODE_PATTERN = re.compile(r"^[0-9]{6}$")


class PortfolioConfigError(ValueError):
    """Raised when the portfolio configuration is missing or ambiguous."""


@dataclass(frozen=True)
class PortfolioConfig:
    version: int
    holdings: Tuple[str, ...]
    watchlist: Tuple[str, ...]

    def tracked_securities(self) -> Tuple[Tuple[str, str], ...]:
        return tuple((code, "holding") for code in self.holdings) + tuple(
            (code, "watchlist") for code in self.watchlist
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "holdings": list(self.holdings),
            "watchlist": list(self.watchlist),
        }


def validate_security_code(raw_code: Any) -> str:
    """Require a plain six-digit code so names and account data never enter config."""
    if not isinstance(raw_code, str) or SECURITY_CODE_PATTERN.fullmatch(raw_code) is None:
        raise PortfolioConfigError(
            f"invalid security code {raw_code!r}; expected exactly six digits"
        )
    return raw_code


def _unique_codes(values: Any, field: str) -> Tuple[str, ...]:
    if not isinstance(values, list):
        raise PortfolioConfigError(f"portfolio config field {field!r} must be an array")
    result = []
    seen = set()
    for raw_code in values:
        code = validate_security_code(raw_code)
        if code not in seen:
            result.append(code)
            seen.add(code)
    return tuple(result)


def validate_portfolio_config(data: Any) -> PortfolioConfig:
    if not isinstance(data, Mapping):
        raise PortfolioConfigError("portfolio config root must be a JSON object")
    unknown_keys = set(data).difference(ALLOWED_CONFIG_KEYS)
    if unknown_keys:
        raise PortfolioConfigError(
            f"portfolio config contains unsupported fields: {sorted(unknown_keys)}"
        )
    missing_keys = ALLOWED_CONFIG_KEYS.difference(data)
    if missing_keys:
        raise PortfolioConfigError(
            f"portfolio config missing required fields: {sorted(missing_keys)}"
        )
    version = data["version"]
    if type(version) is not int or version != 1:
        raise PortfolioConfigError("portfolio config version must be 1")
    holdings = _unique_codes(data["holdings"], "holdings")
    watchlist = _unique_codes(data["watchlist"], "watchlist")
    overlap = sorted(set(holdings).intersection(watchlist))
    if overlap:
        raise PortfolioConfigError(
            f"codes cannot be both holdings and watchlist: {', '.join(overlap)}"
        )
    return PortfolioConfig(version=version, holdings=holdings, watchlist=watchlist)


def load_portfolio_config(path: Path = DEFAULT_CONFIG_PATH) -> PortfolioConfig:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PortfolioConfigError(f"portfolio config not found: {path}") from exc
    except OSError as exc:
        raise PortfolioConfigError(f"cannot read portfolio config {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PortfolioConfigError(
            f"malformed portfolio config JSON at {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    return validate_portfolio_config(data)


def write_portfolio_config(config: PortfolioConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Write validated configuration using an atomic replace in the same directory."""
    validated = validate_portfolio_config(config.as_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(validated.as_dict(), temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
