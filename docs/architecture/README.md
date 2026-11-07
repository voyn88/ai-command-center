<!-- GENERATED FILE — do not hand-edit.
     Source: the `.mmd` files in this directory.
     Regenerate with `python scripts/generate_architecture_docs.py`.
     Checked for staleness by tests/architecture/test_architecture_docs_freshness.py. -->

# AI Command Center — architecture as code

C4-style Mermaid diagrams for AI Command Center, checked in as text so they render
natively on GitHub and stay reviewable in a normal diff. These are diagrams, not
authority: [`ARCHITECTURE.md`](../../ARCHITECTURE.md) and the accepted
[ADRs](../adr/) are authoritative if a diagram and the prose ever disagree — update
the `.mmd` source and regenerate rather than editing this file.

## 1. System context (C4 level 1)

Who and what AI Command Center talks to, from the outside. The system boundary is the repository this document lives in; everything else is an external person or system it integrates with.

```mermaid
%% C4 context (level 1) — AI Command Center and its external actors/systems.
%% Source of truth for docs/architecture/README.md's "System context" diagram.
%% Edit this file, then run `python scripts/generate_architecture_docs.py` to
%% regenerate the README; `tests/architecture/test_architecture_docs_freshness.py`
%% fails CI if the two drift apart.
flowchart TD
    Engineer["Person<br/><b>Engineer</b><br/>local operator"]

    subgraph Boundary["AI Command Center (this repository)"]
        AICC["Software System<br/><b>AI Command Center</b><br/>local engineering control plane"]
    end

    ProviderCLI["External<br/><b>Local provider CLIs</b><br/>Claude Code (default),<br/>Codex / Ollama / Copilot"]
    GitRepos["External<br/><b>Git repositories</b><br/>local checkouts and worktrees"]
    GitHub["External<br/><b>GitHub</b><br/>pull requests, merge queue,<br/>via the local gh CLI"]
    AiosCore["External<br/><b>AIOS Core</b><br/>closed platform engine,<br/>reached only through aios_sdk / aios_db"]
    Portfolio["External<br/><b>Portfolio checkout</b><br/>separate repository read<br/>for cross-project intelligence"]

    Engineer -->|"uses, via browser (Streamlit)<br/>or the native desktop shell"| AICC
    AICC -->|"starts and supervises<br/>as local subprocesses"| ProviderCLI
    AICC -->|"reads, writes, creates worktrees"| GitRepos
    AICC -->|"opens/updates pull requests"| GitHub
    AICC -->|"adapter-only: aios_sdk (tasks),<br/>aios_db (Postgres primitives)"| AiosCore
    AICC -->|"reads only"| Portfolio
```

Source: [`context.mmd`](context.mmd)

## 2. Containers (C4 level 2)

The deployable/runnable units inside the AI Command Center boundary, and how they reach the external systems from the context diagram. See [`ARCHITECTURE.md`](../../ARCHITECTURE.md) for the prose description these containers implement.

```mermaid
%% C4 container (level 2) — containers inside the AI Command Center system
%% boundary from context.mmd. Source of truth for docs/architecture/README.md's
%% "Containers" diagram. Edit this file, then run
%% `python scripts/generate_architecture_docs.py` to regenerate the README;
%% tests/architecture/test_architecture_docs_freshness.py fails CI on drift.
flowchart TD
    Engineer["Person<br/><b>Engineer</b>"]

    subgraph AICC["AI Command Center"]
        Streamlit["Container: Python / Streamlit<br/><b>Streamlit web app</b><br/>app.py, localhost-only by default"]
        UI["Container: Python<br/><b>UI panels</b><br/>command_center/ui"]
        Domain["Container: Python<br/><b>Domain &amp; application services</b><br/>planning, launch, execution queue,<br/>portfolio, project_config"]
        Runtime["Container: Python<br/><b>Runtime subsystem</b><br/>command_center/runtime:<br/>API facade, scheduler,<br/>autonomy, completion"]
        Supervisor["Container: Python process group<br/><b>Runtime Supervisor</b><br/>owns provider-CLI subprocess lifecycle"]
        Worker["Container: systemd daemon<br/><b>Headless worker service</b><br/>command_center/worker,<br/>claim → execute → report, unattended"]
        Desktop["Container: PySide6/Qt<br/><b>Native desktop shell</b><br/>command_center.desktop (D1 shipped)"]
        AiosAdapter["Container: Python module<br/><b>AIOS/DB adapters</b><br/>application/aios_tasks.py,<br/>db/adapter.py — sole boundary crossings"]
        Stores[("Container: SQLite + JSON/JSONL<br/><b>Local persistence</b><br/>data/runtime.db, data/tasks.json,<br/>data/execution_queue.json, ...")]
    end

    ProviderCLI["External<br/><b>Local provider CLIs</b>"]
    GitRepos["External<br/><b>Git repositories</b>"]
    GitHub["External<br/><b>GitHub</b>"]
    AiosCore["External<br/><b>AIOS Core</b>"]

    Engineer -->|"HTTP/WebSocket"| Streamlit
    Engineer -->|"native app"| Desktop

    Streamlit --> UI
    Streamlit --> Domain
    Desktop -->|"D2/D3 target: command_center.application adapters<br/>(not yet built)"| Domain

    UI --> Domain
    Domain --> Runtime
    Domain --> Stores
    Runtime --> Stores
    Runtime --> Supervisor
    Supervisor -->|"subprocess"| ProviderCLI

    Worker -->|"claims work_item rows"| Stores
    Worker -->|"dispatches"| ProviderCLI
    Worker -->|"push, PR ops"| GitHub

    Domain -->|"git/gh operations"| GitRepos
    Domain -->|"pull-request operations"| GitHub

    Domain --> AiosAdapter
    Runtime --> AiosAdapter
    AiosAdapter -->|"aios_sdk / aios_db only"| AiosCore
```

Source: [`container.mmd`](container.mmd)

## Keeping this in sync

Each diagram above is generated from the `.mmd` file of the same name in this
directory — edit that file, not this one. After editing, run:

```sh
python scripts/generate_architecture_docs.py
```

`tests/architecture/test_architecture_docs_freshness.py` (part of the repository's
existing architecture-fitness suite) fails if this file is stale relative to the
`.mmd` sources, so a diagram change without regeneration does not merge silently.

An ADR that changes AI Command Center's external integrations or its container
boundaries should update these diagrams — see
[`docs/adr/TEMPLATE.md`](../adr/TEMPLATE.md).
