# Portfolio Snapshot Store

Portfolio Market Data v2 treats GitHub cron as a wake-up signal, not as the
market-data clock. `Asia/Shanghai` timestamps and trading-calendar dates remain
the data authority.

## Source of truth

```text
data/portfolio/
  snapshots/YYYY-MM-DD/{premarket,midday,close}.json
  snapshots/YYYY-MM-DD/manifest.json
  universe/YYYY-MM-DD.json
  latest/{premarket,midday,close}.json
  pipeline_health.json
  {premarket,midday,close}.json
```

- Per-day snapshots and universes are canonical. Normal generation does not
  overwrite valid history; repair may replace corrupt state or improve it.
- `manifest.json` owns lifecycle (`pending`, `available`, `missed`, `error`).
  Missing market data is recorded there; no fake snapshot is created.
- `latest/*.json` and the three root JSON files are derived compatibility
  copies. They advance only when `snapshot_as_of` is newer, so an older repair
  cannot move consumers backwards.
- `pipeline_health.json` is the first consumer entry point. A provider outage
  yields `degraded` health while a clean repair run still completes normally.

## Snapshot contract

```json
{
  "schema_version": "2.0",
  "trading_date": "2026-09-14",
  "market_phase": "midday",
  "snapshot_kind": "morning_close",
  "snapshot_as_of": "2026-09-14T11:30:00+08:00",
  "information_cutoff": "2026-09-14T11:30:00+08:00",
  "generated_at": "2026-09-14T17:47:43+08:00",
  "timezone": "Asia/Shanghai",
  "generation_mode": "reconstructed",
  "data_date": "2026-09-14",
  "status": "ok",
  "completeness": "partial",
  "portfolio_status": "partial",
  "blocking": false,
  "warnings": [
    {"scope": "stock", "code": "688825", "field": "MA60", "reason": "insufficient_history"}
  ],
  "data_quality": {
    "blocking": false,
    "warnings_count": 1,
    "errors_count": 0,
    "errors": []
  },
  "holdings_codes": ["688825"],
  "watchlist_codes": [],
  "holdings": [],
  "watchlist": [],
  "stocks": [],
  "benchmarks": [],
  "errors": [],
  "provenance": {}
}
```

`snapshot_as_of` is the real market instant represented by the data;
`information_cutoff` is the latest external-information time allowed for that
report phase; `generated_at` is only the real execution time. They are never
substituted. A late `generated_at` does not make a correctly reconstructed
business snapshot stale.

`status` describes analytical usability. `completeness` describes optional
field coverage. A reconstructed midday snapshot with complete price, morning
OHLCV/amount, previous close, MA and return fields is therefore
`status=ok, completeness=partial` when native `volume_ratio` or
`turnover_rate` is unavailable.

`blocking=false` means consumers may use the snapshot and surface its
structured warnings. Missing long-history indicators are non-blocking;
universe mismatches, wrong business dates/times, missing core quotes, required
benchmark failures and corrupt structure are blocking and cannot be committed
as a formal snapshot.

Premarket remains `market_phase=premarket` for compatibility and sets
`snapshot_kind=previous_close_context`. It represents the latest completed
official close, not auction or pre-open quotes.

## Historical universe

The first normal snapshot freezes that trading day's config as
`source_type=frozen, confidence=authoritative`. Historical repair resolves a
universe from the frozen file, a same-day canonical snapshot, or the Git version
of `config/portfolio.json` as of that date. Inferred values use
`inferred_snapshot` or `inferred_git` and `confidence=inferred`; current config
is never silently substituted for unknown history.

## Midday reconstruction

Midday means the 11:30 morning close. The first provider is AkShare's Sina
`stock_zh_a_minute` route for stocks, ETFs and the four required indexes. The
returned range must contain the requested date and an exact 11:30 end-labelled
bar. Bars from 09:31 through 11:30 build morning OHLCV/amount; the reconstructed
close combines with prior completed daily bars for MA and return metrics.

Provider retention is never assumed. Reconstructability is proven from the
actual response. A 11:29 bar, current quote, interpolation or rewritten time is
not accepted as 11:30.

## Repair and consumers

`Portfolio Snapshot Repair` wakes at 18:37, 22:17 and 06:07 Beijing time and
audits the last five A-share sessions. Close and premarket context use completed
daily bars; midday uses historical minute bars. Provider unavailability records
`missed`/`error` and degrades health. Schema corruption, canonical conflict,
Git failure, or final remote verification failure fails the workflow.

Consumers should read:

```text
pipeline_health.json -> daily manifest.json -> canonical phase snapshot
```

Weekly consumers resolve each trading day in the target week and use canonical
close snapshots for core return, drawdown, trend and relative-index metrics.
Midday is optional; an explicit `missed` state does not block a close-based
weekly report.
