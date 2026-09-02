---
name: ai-crm-production-delivery
description: Use for AI-CRM production inventory, promotion, deployment, or verification. Enforces the single repository and host, exact-SHA delivery, redacted asset migration, and separate CI/runtime/external-effect evidence.
---

# AI-CRM Production Delivery

Read `docs/development/ai_crm_next_architecture_skill.md` first. This skill adds
delivery constraints; it does not replace the architecture gate.

## Fixed scope

- Repository: `qianlan33333-png/AI-CRM-production`.
- Deployment host: `124.220.53.183`.
- V2 / AI-CRM Next owns API, data, authentication, migrations, and deployment.
- V1 is a zero-write presentation reference only.

Stop if the checked remote, default branch, `DEPLOY_HOST`, or runtime host does
not match this scope. Do not operate another repository or server.

## Required preflight

1. Fetch and prune `origin`, then record the exact `origin/main` SHA.
2. Preserve every existing checkout. If any checkout has work in progress, use
   a clean isolated worktree from `origin/main`.
3. Read `docs/architecture/new_crm_asset_migration_matrix.json` and run
   `python tools/check_new_crm_asset_migration_matrix.py`.
4. Inspect current GitHub workflow variables and the target runtime. Historical
   evidence is context, never proof of current state.
5. Re-check that `.github/workflows/deploy.yml` fails closed unless
   `DEPLOY_HOST` equals `124.220.53.183`.

## Asset migration policy

- Commit only reviewable code, migrations, configuration contracts, ADRs,
  runbooks, acceptance criteria, capability inventory, and repo-local skills.
- Never commit raw Codex memory, session logs, credentials, tokens, private
  keys, customer identifiers, message bodies, provider payloads, database
  dumps, build archives, or local evidence bundles.
- Distill useful operational knowledge into rules and acceptance criteria.
  Reference secrets only by environment or GitHub Secret name.
- Run the matrix checker, task-specific tests, full CI, and a diff-focused
  secret/PII review before delivery.

## Deployment and evidence

- Use branch, Chinese PR, required CI, merge, and exact-main promotion. Do not
  push directly to `main`.
- Before deployment, verify the candidate is the exact current `main` SHA and
  the host allowlist still passes.
- Record release SHA, migration heads, relevant service states, local health,
  and approved public health. Do not change DNS, public traffic, or an old host.
- Do not trigger payment, OAuth, callbacks, group sends, private messages, or
  other provider effects merely to prove deployment.

Report these conclusions independently:

1. capability and asset repository state;
2. PR and exact `main` state;
3. CI state;
4. deployment and manual acceptance state;
5. provider / WeCom external-effect state.

`merged`, `CI green`, `deployed`, `healthy`, `manually accepted`, and
`provider-confirmed` are different facts. Never use one as evidence for another.

## Rollback

Rollback is the repository's guarded previous-release rollback for the exact
deployed SHA. Reverting this skill or its inventory is a normal Git revert. A
rollback never means restoring a retired runtime, touching an old server, or
switching DNS without separate authorization.
