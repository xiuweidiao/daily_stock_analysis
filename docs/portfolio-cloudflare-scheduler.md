# Portfolio Cloudflare Scheduler

This integration adds an independent clock for the existing portfolio snapshot pipeline. The Cloudflare Worker does not fetch market data, validate snapshots, write repository files, or implement a second recovery path. It only calls GitHub's `workflow_dispatch` API with `ref=main`.

```text
GitHub schedule ------\
                       +--> existing GitHub Actions workflows
Cloudflare Cron ------/             |
                                    +--> readiness / manifest
                                    +--> generator or reconstruction
                                    +--> validator / commit / remote verification
```

The existing shared `portfolio-snapshot-writer-${ref}` concurrency group and readiness rechecks make duplicate wakeups safe. The first run that creates a valid canonical snapshot wins; later Cloudflare or delayed GitHub runs see `SNAPSHOT_ALREADY_FRESH` or `REMOTE_SNAPSHOT_ALREADY_FRESH` and do not create another snapshot commit.

## Schedule

Cloudflare Cron uses UTC. The Worker configuration in `cloudflare/portfolio-scheduler/wrangler.toml` maps to Asia/Shanghai as follows:

| Purpose | Cloudflare UTC cron | Asia/Shanghai |
| --- | --- | --- |
| Premarket primary | `40 23 * * 0-4` | Monday-Friday 07:40 |
| Premarket recovery wakeup | `40 0 * * 1-5` | Monday-Friday 08:40 |
| Midday primary | `40 3 * * 1-5` | Monday-Friday 11:40 |
| Midday recovery wakeup | `0 4 * * 1-5` | Monday-Friday 12:00 |
| Close primary | `10 7 * * 1-5` | Monday-Friday 15:10 |
| Close recovery wakeup | `30 7 * * 1-5` | Monday-Friday 15:30 |
| Daily repair | `30 10 * * 1-5` | Monday-Friday 18:30 |

The Sunday UTC premarket expression intentionally becomes Monday morning in Shanghai. The 12:00 trigger is not the only midday wakeup; 11:40 is the primary. GitHub's existing schedules remain enabled as a separate fallback clock.

The phase wakeups dispatch `.github/workflows/portfolio-market-data.yml` with `phase=premarket|midday|close`. The repair wakeup dispatches `.github/workflows/portfolio-snapshot-repair.yml` with `days=5`. Both include `trigger_source=cloudflare_cron` and the expected Shanghai business slot for the Actions summary.

## GitHub credential

For this personal repository, the smallest practical credential is a **fine-grained personal access token**, not a classic PAT:

1. Limit repository access to `xiuweidiao/daily_stock_analysis` only.
2. Grant repository permission **Actions: Read and write**.
3. Leave **Contents** and all other writable permissions disabled. Metadata read access is automatic.
4. Give the token a short expiry and rotate it before expiry.

`Actions: write` is required by GitHub's workflow-dispatch endpoint. The token itself cannot write snapshot files; the dispatched workflow uses its existing repository-scoped `GITHUB_TOKEN` and current contract gates.

A GitHub App installation token is also suitable, but it requires the Worker to sign a JWT and exchange it for short-lived installation tokens. That is more operational complexity than a single-repository fine-grained token. Use an App when centrally managed key rotation is more important than minimal maintenance.

Never put the token in `wrangler.toml`, `.dev.vars`, source, logs, commits, or workflow inputs.

## First deployment

Prerequisites: a Cloudflare account, Node.js, and Wrangler authentication.

```bash
cd cloudflare/portfolio-scheduler
npm test
npx wrangler@4 login
npx wrangler@4 secret put GITHUB_TOKEN
npx wrangler@4 deploy
```

Enter the fine-grained token only at the `secret put` prompt. `wrangler.toml` contains only non-secret repository coordinates and the cron definitions. Cloudflare's Worker Logs/Observability records accepted or failed dispatches without logging request headers or response bodies.

## Manual test

Use Wrangler's local scheduled-event endpoint. Put a test credential in the ignored local file `cloudflare/portfolio-scheduler/.dev.vars`:

```text
GITHUB_TOKEN=github_pat_...
```

Then run:

```bash
cd cloudflare/portfolio-scheduler
npm run dev
curl --get --data-urlencode 'cron=40 3 * * 1-5' \
  http://localhost:8787/__scheduled
```

That example dispatches midday on `main`. Use a repository test token only for this check and remove `.dev.vars` afterward. A successful GitHub API request returns HTTP 204 to the Worker; the Worker log records `result=accepted`. HTTP failures and network failures cause the Cron invocation to fail visibly and never log the token.

Verify the created GitHub run:

```bash
gh run list --workflow portfolio-market-data.yml --event workflow_dispatch --limit 5
gh run view RUN_ID
```

The Actions Step Summary must show:

```text
Trigger source: cloudflare_cron
Expected schedule slot: 11:40
Resolved phase: midday
```

The run must continue through the existing readiness, generator/reconstruction, validator, commit and final remote freshness steps. If the snapshot is already valid, generator and commit are skipped and the final state is `SNAPSHOT_ALREADY_FRESH` or `REMOTE_SNAPSHOT_ALREADY_FRESH`.

## Failure and monitoring semantics

- Missing `GITHUB_TOKEN`, network failure, or a GitHub response other than 204 makes the Worker invocation fail. The log records only the HTTP status and GitHub request ID.
- The Worker does not claim the snapshot succeeded merely because dispatch was accepted. Snapshot truth remains in GitHub Actions, the manifest and `data/portfolio/pipeline_health.json`.
- If GitHub Actions is unavailable, later Cloudflare cron triggers try again. When Actions recovers, the existing readiness/reconstruction chain repairs recoverable snapshots.
- Midday reconstruction still requires exact 11:30 historical minute bars. If unavailable, repair records `missed`/degraded rather than inventing data.
- Close recovery continues to use completed daily bars. Premarket continues to represent previous-close context.

Operational checkpoints are:

| Beijing checkpoint | Expected evidence |
| --- | --- |
| after 08:50 | premarket ready, or manifest/health states the failure |
| after 12:10 | midday ready/reconstructed, or explicitly missed/degraded |
| after 15:45 | close ready, or recovery still required |
| after 18:30 repair | all three phases audited; remaining blocking/missed states visible in manifest and pipeline health |

Cloudflare being an independent clock removes GitHub scheduled-event creation as the only wakeup path. It does not make GitHub Actions or free market-data providers infallible.

## Rollback

Disable or delete the Cloudflare cron triggers, or remove the Worker:

```bash
cd cloudflare/portfolio-scheduler
npx wrangler@4 delete
```

Then delete/rotate the fine-grained GitHub token. GitHub's original schedules remain active throughout, so disabling Cloudflare reverts scheduling behavior without changing the market-data pipeline or snapshot contract. The optional workflow-dispatch metadata inputs are backward compatible and may remain, or the PR can be reverted as a unit.
