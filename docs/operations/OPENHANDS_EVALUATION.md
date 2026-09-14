# OpenHands server-side executor evaluation — blocked, not adopted

Snapshot: 2026-09-07, against `main` tip `a8acdbb`. Written for
VOYN-W0-AICC-OPENHANDS-EVALUATION per the 2026-09-03 owner decision: docker-run
OpenHands on `voyn-worker-01` against 2-3 real bounded backlog tasks with a
free Gemini key (when provided) or an Ollama backend, measure completion
quality against the "aider lane," and adopt only if measurably better —
evidence-driven, no parallel authority.

**Outcome: not evaluated.** This task-agent sandbox cannot reach `voyn-worker-01`,
cannot start a container, and was not handed an LLM credential, so no
completion-quality measurement was possible. Nothing about the fleet's
executor cascade was changed. The findings below are what was actually
checked, not an assumed or simulated result.

## What "the aider lane" resolves to

There is no `aider` executor anywhere in this repository — not in
`command_center/executors.py`, not in `command_center/agent_runner.py`'s
`COMMAND_BUILDERS`, not in `command_center/orchestrator/routing.py`'s
`ROUTING_MATRIX`, and not in any deploy/ops script (`grep -ri aider` across
the tree, including non-Python files, returns zero hits as of this snapshot).
The fleet's actual, proven implementation cascade is:

```
claude -> codex -> copilot
```

(`command_center/orchestrator/routing.py::ROUTING_MATRIX["implementation"]`,
each link gated by `command_center/agent_runner.py::COMMAND_BUILDERS`, which
only lists an executor once its CLI is proven installed on the worker host —
see that module's own docstring on why a phantom link is worse than no link).
"Aider lane" in the owner decision is fleet vocabulary for whatever
non-AICC baseline the owner is tracking outside this repository; it is not a
comparable in-repo artifact. Whoever runs the live evaluation should confirm
with the owner what baseline runs (which tasks, which executor, which
outcomes) "aider lane" is meant to name before scoring OpenHands against it.

## What was attempted here, and why it stopped

This backlog task was cloned into an isolated worktree with no changes to
make in the ordinary sense — the deliverable is the live measurement itself.
Three preconditions from the owner decision were checked directly in this
environment:

1. **Docker.** The `docker` CLI is present (`29.1.3`), but the daemon socket
   is owned by `root:docker` and this session's user is not in the `docker`
   group and has no passwordless `sudo`:
   ```
   $ docker run hello-world
   permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
   $ sudo -n true
   sudo: a password is required
   ```
   No container — OpenHands or otherwise — can be started from this sandbox.
2. **`voyn-worker-01` access.** This task-clone has no SSH credential, no
   hostname, and no network path to the named worker host. It is a disposable
   git worktree, not a session on the fleet's execution infrastructure.
3. **LLM backend.** No `GEMINI_API_KEY`, no other Gemini credential, and no
   reachable Ollama endpoint are present in this environment (`env | grep -i
   -E 'gemini|openhands|ollama'` returns nothing). The owner decision made the
   Gemini key conditional ("when provided"); it was not provided here, and no
   local Ollama model is configured to fall back to.

Any one of these missing would block a real run; all three are missing at
once. Fabricating plausible-looking scores for OpenHands against invented
task outcomes would satisfy the letter of "write an evaluation" while
violating the decision's actual point — "evidence-driven, no parallel
authority" rules out exactly that. This document reports the blocker instead.

## Decision

**Not adopted, pending evidence.** No entry for `openhands` was added to
`command_center/agent_runner.py::COMMAND_BUILDERS`,
`command_center/orchestrator/routing.py::ROUTING_MATRIX`, or
`command_center/executors.py::EXECUTORS`. Per the routing module's own stated
discipline, an executor is only entered once its CLI is *proven* on the
worker host — this task did not get far enough to prove or disprove that, so
adding even a disabled/reserved slot would overstate the state of the
evaluation. The fleet continues to run the existing `claude -> codex ->
copilot` cascade unchanged.

## What unblocks the real evaluation

For whoever picks this up next with the right access:

1. **Access needed:** either (a) a login on `voyn-worker-01` with permission
   to run Docker containers, or (b) `docker` group membership / equivalent on
   a host with a network path to that worker's task queue; plus (c) a Gemini
   API key (free tier is sufficient per the owner decision) or a running
   Ollama instance reachable from wherever OpenHands executes.
2. **Task selection:** pick 2-3 *bounded* backlog tasks the fleet has already
   completed once through the `claude`/`codex` cascade, so there is a real
   completion (diff, test result, wall-clock time, run record) to compare
   against rather than a subjective read. `command_center/dispatch` and the
   `run`/`work_event` tables are the source of that history — do not hand-pick
   tasks OpenHands is likely to look good on.
3. **Run OpenHands headless/CLI mode** against a disposable worktree per task
   (never the canonical checkout — this matches every existing executor's
   isolation requirement, e.g. ADR 0006's Codex worktree constraints), with
   the LLM backend pointed at the provisioned Gemini key or Ollama endpoint.
4. **Score on the same axes the fleet already tracks for other executors:**
   did it produce a correct, tested diff; wall-clock time; token/dollar cost;
   failure mode (quota/auth/timeout/incomplete) if it did not finish — using
   the existing `failure_reason` vocabulary in `agent_runner.py` rather than
   inventing a new one.
5. **Record the result in this document** (replace this "not evaluated"
   outcome with the actual scores and the adopt/reject call), and only then
   propose the `agent_runner.py`/`routing.py` change if the evidence supports
   it. A routing change without this evidence would be exactly the "parallel
   authority" the owner decision rules out.
