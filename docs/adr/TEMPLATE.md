# ADR NNNN — Title

Status: **Proposed.** (Update to **Accepted** on merge, or **Superseded by ADR-XXXX** / **Rejected** later.)

<!--
Copy this file to `docs/adr/00NN-short-slug.md` (next sequential number; see the
other files in this directory for the current high-water mark) and fill in
every section below. Delete this comment block before opening the PR.
-->

## Context

What forced this decision now? State the constraint, incident, or conflicting
fact that made the status quo untenable — not just the feature being built.
Link the ticket, prior ADR, or code that motivated it.

## Decision

What was decided, and why this option over the alternatives. Be specific about
what changes: modules, contracts, data ownership, process boundaries.

If this decision changes AI Command Center's **external integrations** (a new
system it talks to, or a changed relationship with an existing one — Git,
GitHub, a provider CLI, AIOS Core, the Portfolio checkout) or its **container
boundaries** (a new deployable/runnable unit, or a changed responsibility
between existing ones — the Streamlit app, the runtime subsystem, the headless
worker, the desktop shell, the AIOS/DB adapters), update the corresponding
diagram source in [`docs/architecture/`](../architecture/) (`context.mmd` and/or
`container.mmd`) and regenerate the rendered doc:

```sh
python scripts/generate_architecture_docs.py
```

`tests/architecture/test_architecture_docs_freshness.py` fails CI if the
diagrams and the generated `docs/architecture/README.md` drift apart, so this
step is checked, not just requested.

## Consequences

What gets easier, what gets harder, and what follow-up work (if any) this
creates. Include rejected alternatives here if they are worth recording so the
question does not get re-litigated later.
