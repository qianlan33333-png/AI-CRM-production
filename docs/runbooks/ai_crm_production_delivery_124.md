# AI-CRM Production Delivery Runbook

## Purpose and boundary

This runbook is the reviewable replacement for local deployment memories and
session evidence. The only delivery repository is
`qianlan33333-png/AI-CRM-production`; the only approved host is
`124.220.53.183`.

The runbook does not authorize DNS or public-traffic switching, old-server
inspection or cleanup, data migration from another host, or provider-side test
writes. Those actions require separate, explicit scope.

## Preflight

1. Confirm `origin` and the default branch, then `git fetch --prune origin`.
2. Record `git rev-parse origin/main`; work only in a clean isolated worktree.
3. Run `python tools/check_new_crm_asset_migration_matrix.py`.
4. Confirm repository-level `DEPLOY_HOST` exists without printing its value.
5. Confirm the deploy workflow contains `EXPECTED_DEPLOY_HOST: 124.220.53.183`
   and fails before transfer when the Secret differs.
6. Read the current health release header and verify the current server
   hostname. Do not infer either from an earlier run.

## Promotion

1. Commit a focused branch and open a Chinese PR using the architecture output
   sections: Summary, Architecture boundary, Safety / non-goals, Verification,
   Risk / rollback, and Next action.
2. Require the PR gate to pass. Merge through GitHub; never update `main`
   directly.
3. Fetch again and record the exact merged `origin/main` SHA.
4. Verify the deployment run selected that exact SHA and that full regression
   passed before the transfer job began.

## Deployment acceptance

Collect non-sensitive evidence for each line independently:

| Gate | Required evidence |
| --- | --- |
| exact release | `origin/main`, workflow `headSha`, health release header, and server release SHA agree |
| schema | current Alembic heads equal repository migration heads |
| runtime | required systemd units are enabled/active as defined by the release manifest |
| health | local web health and the approved health URL return success for the exact SHA |
| data mode | PostgreSQL, production repository policy, and fixture mode disabled |
| timers | required timers are active; warning counts are reported, not hidden |
| authentication | route and configuration checks pass; no live OAuth flow is triggered for smoke testing |
| external effects | configuration/readiness is reported separately; no real send/payment/provider call is initiated |

The health endpoint may report queue warnings while remaining HTTP-ready.
Record those warnings as operational state; do not rewrite them as green external
effects or as a failed deployment without checking their owning contract.

## Asset handling

The authoritative inventory is
`docs/architecture/new_crm_asset_migration_matrix.json`. Raw local plans,
recovery SQL, spreadsheets, build archives, logs, certificates, Codex memories,
and session records remain outside Git. The matrix records why each source is
canonical, distilled, blocked, or excluded.

Secrets are referenced only by names such as `DEPLOY_HOST`, `DEPLOY_USER`, and
`DEPLOY_SSH_KEY`. Never paste their values into a PR, log excerpt, runbook, or
acceptance report.

## Rollback

Use the guarded previous-release rollback for the deployed exact SHA. Verify the
restored health release header and schema compatibility. This runbook does not
authorize old-host operations, DNS changes, or legacy-runtime restoration.
