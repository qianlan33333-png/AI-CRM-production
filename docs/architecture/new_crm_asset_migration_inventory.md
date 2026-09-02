# New CRM Asset Inventory and Migration Notes

The machine-readable source of truth is
`docs/architecture/new_crm_asset_migration_matrix.json`. It records every
discovered logical memory, skill, capability, and local asset group with a
destination or a blocking/exclusion reason.

## Inventory result

- The local `AI-CRM-v2` checkout and `AI-CRM-production` now share the same
  GitHub remote and `origin/main`. `AI-CRM-v2` is therefore a stale checkout,
  not a second delivery source. No code is copied from its working tree.
- Four existing repo-local skill packages are already canonical. The new
  production-delivery skill distills only repository/host/evidence/safety rules.
- The production profile contains all 14 registry capabilities. Their source,
  dependencies, checks, and deployment evidence remain code-backed.
- Local Huangxiaocan copywriting skills contain operator voice, campaign URLs,
  and external-message behavior. They remain local-only; their safe product and
  automation boundaries are represented by existing HXC/automation code and
  architecture contracts.

## Excluded local assets

The following are inventoried but never copied verbatim:

- local HXC plans: potentially stale product decisions and business context;
- recovery SQL/manifests: unreviewed writes and business-object payloads;
- acceptance workbooks: binary evidence that may contain customer/business data;
- Codex artifacts: generated archives, logs, certificates, database evidence,
  and possible credentials/PII;
- archive public-key files: cryptographic deployment material that belongs in
  host configuration or a Secret reference, not source control;
- raw memories and session records: private tool history rather than a
  reviewable product contract.

Their reusable lessons are reduced to the architecture gate, the production
delivery skill/runbook, and existing capability/side-effect contracts. Exclusion
is a deliberate migration disposition, not an assertion that the source was
fully safe to inspect or commit.

## Capability status vocabulary

- `real`: code-backed and contract-tested; deployment/external-effect state is
  still reported independently.
- `backend_blocked`: a named dependency or approval is missing; no UI or local
  artifact may claim completion.
- `presentation_only`: a V1/static/acceptance surface with no authority to write
  data or claim backend behavior.

V1 remains zero-write and presentation-only. AI-CRM Next/V2 remains the API,
data, authentication, migration, and deployment owner. Existing OpenAPI routes
must be used directly; mock or fixture data is never production evidence.
