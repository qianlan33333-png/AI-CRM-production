# AI-CRM Agent Entry

## Frontend Development Gate

In this repository, any AI/Codex/Agent task that touches frontend development,
page development, component development, UI adjustment, or admin-console feature
work must read and follow
[`docs/skills/frontend-development-skill.md`](docs/skills/frontend-development-skill.md)
before implementation starts.

This frontend skill has priority over casual development habits and old page
patterns. If a frontend task starts without reading it, the implementation
should not proceed.

Every frontend-related final response must include the required
`Frontend Skill Checklist` from the skill. For non-frontend tasks, state that the
checklist is not applicable.

## Architecture Gate

All Codex development tasks must also follow
[`docs/development/ai_crm_next_architecture_skill.md`](docs/development/ai_crm_next_architecture_skill.md)
for AI-CRM Next architecture boundaries, production safety, legacy freeze rules,
and PR output expectations.

The architecture skill is the canonical development preflight. Other agent entry
documents must point back to it instead of introducing separate required-reading
lists.

## Production Delivery Gate

Any task that inspects, changes, or executes production delivery must also read
and follow
[`skills/ai-crm-production-delivery/SKILL.md`](skills/ai-crm-production-delivery/SKILL.md).
This gate fixes the repository, host allowlist, evidence separation, sensitive
asset policy, and external-effect boundary for production delivery work. It does
not authorize DNS changes, old-host operations, or real provider calls.

## Operation Cycle Agent Reporting

Any Agent task that creates, updates, reports, or diagnoses a CRM operation cycle
must read and follow
[`docs/operation_cycles/agent_usage_guide.md`](docs/operation_cycles/agent_usage_guide.md).
This guide is capability-specific and does not replace the architecture skill.
