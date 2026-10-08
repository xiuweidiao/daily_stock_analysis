from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = ROOT / "cloudflare" / "portfolio-scheduler"
SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


def test_cloudflare_crons_match_documented_shanghai_slots() -> None:
    config = tomllib.loads((WORKER_ROOT / "wrangler.toml").read_text(encoding="utf-8"))
    assert config["triggers"]["crons"] == [
        "40 23 * * 0-4",
        "40 0 * * 1-5",
        "40 3 * * 1-5",
        "0 4 * * 1-5",
        "10 7 * * 1-5",
        "30 7 * * 1-5",
        "30 10 * * 1-5",
    ]
    conversions = (
        (datetime(2026, 9, 20, 23, 40, tzinfo=UTC), (7, 40)),
        (datetime(2026, 9, 21, 0, 40, tzinfo=UTC), (8, 40)),
        (datetime(2026, 9, 21, 3, 40, tzinfo=UTC), (11, 40)),
        (datetime(2026, 9, 21, 4, 0, tzinfo=UTC), (12, 0)),
        (datetime(2026, 9, 21, 7, 10, tzinfo=UTC), (15, 10)),
        (datetime(2026, 9, 21, 7, 30, tzinfo=UTC), (15, 30)),
        (datetime(2026, 9, 21, 10, 30, tzinfo=UTC), (18, 30)),
    )
    for utc_time, expected_clock in conversions:
        shanghai = utc_time.astimezone(SHANGHAI)
        assert (shanghai.hour, shanghai.minute) == expected_clock
        assert shanghai.weekday() < 5


def test_cloudflare_dispatch_metadata_is_accepted_by_existing_workflows() -> None:
    market = (ROOT / ".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )
    repair = (ROOT / ".github/workflows/portfolio-snapshot-repair.yml").read_text(
        encoding="utf-8"
    )
    for workflow in (market, repair):
        assert "trigger_source:" in workflow
        assert "expected_slot:" in workflow
        assert "Trigger source: ${TRIGGER_SOURCE}" in workflow
        assert "inputs.trigger_source" in workflow
    assert "inputs.expected_slot" in market
    assert "DISPATCH_EXPECTED_SLOT" in repair


def test_cloudflare_reuses_the_canonical_workflows_and_main_ref() -> None:
    source = (WORKER_ROOT / "src/index.js").read_text(encoding="utf-8")
    config = (WORKER_ROOT / "wrangler.toml").read_text(encoding="utf-8")
    assert 'const MARKET_WORKFLOW = "portfolio-market-data.yml"' in source
    assert 'const REPAIR_WORKFLOW = "portfolio-snapshot-repair.yml"' in source
    assert 'GITHUB_REF = "main"' in config
    assert "portfolio_market_data.py" not in source
    assert "portfolio_snapshot_repair.py" not in source


def test_dual_schedulers_share_existing_idempotency_and_writer_lock() -> None:
    market = (ROOT / ".github/workflows/portfolio-market-data.yml").read_text(
        encoding="utf-8"
    )
    repair = (ROOT / ".github/workflows/portfolio-snapshot-repair.yml").read_text(
        encoding="utf-8"
    )
    assert "Check snapshot readiness" in market
    assert "Recheck readiness before generator" in market
    assert "Verify final remote freshness" in market
    assert "portfolio-snapshot-writer-${{ github.ref }}" in market
    assert "portfolio-snapshot-writer-${{ github.ref }}" in repair


def test_cloudflare_secret_files_are_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/cloudflare/portfolio-scheduler/.dev.vars" in gitignore
    assert "/cloudflare/portfolio-scheduler/.wrangler/" in gitignore
    worker_source = (WORKER_ROOT / "src/index.js").read_text(encoding="utf-8")
    assert "test-token-never-log" not in worker_source
