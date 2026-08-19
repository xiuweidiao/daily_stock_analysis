#!/usr/bin/env python3
"""Wait for a scheduled portfolio phase's legal data-collection target."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.portfolio_phase_policy import (
    PhaseTimeError,
    SHANGHAI_TZ,
    plan_scheduled_phase,
)


LOGGER = logging.getLogger("portfolio_phase_gate")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("premarket", "midday", "close"), required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        plan = plan_scheduled_phase(args.phase, datetime.now(SHANGHAI_TZ))
    except PhaseTimeError as exc:
        LOGGER.error("%s", exc)
        return 2

    if plan.wait_seconds:
        LOGGER.info(
            "%s scheduled run entered the queue at %s; waiting %d seconds until %s",
            plan.phase,
            plan.current.isoformat(),
            plan.wait_seconds,
            plan.target.isoformat() if plan.target else "n/a",
        )
        time.sleep(plan.wait_seconds)
    else:
        LOGGER.info("%s scheduled run is ready at %s", plan.phase, plan.current.isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
