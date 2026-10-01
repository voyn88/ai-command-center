# Hotspot extraction seams — policy for AICC's highest blast-radius files

## What this is

A UX/code audit (`outputs/ux-audit-latest.json`, generated 2026-08-26T05:30:41Z,
written up in `outputs/VOYN_UX_AUDIT_LATEST.md` with 8 desktop/mobile
screenshots; audited on a clean integration worktree at main SHA `2618c2a`)
found a small set of files carrying a disproportionate share of AICC's
blast radius — the files most changes end up touching, and therefore the
files where an unrelated change is most likely to break something far away
from what it was meant to fix:

| File | Lines at audit time | Lines at freeze time (2026-09-02) |
| --- | --- | --- |
| `app.py` | 3332 | 3332 |
| `command_center/runtime/supervisor.py` | 2615 | 3263 |
| `command_center/task_pipeline.py` | 2435 | 2459 |
| `command_center/workspace_provisioning.py` | 2159 | 2285 |
| `command_center/ui/execution_center_monitor.py` | 1876 | 1876 |

(The freeze-time counts are higher for three files because ordinary work
landed on `main` in the week between the audit and this policy. That gap is
exactly the problem this policy addresses: nothing was stopping continued,
uncoordinated growth of these files. The gate below draws the line at
freeze time, not audit time, so it doesn't retroactively fail work that was
already reviewed and merged.)

**Audit verdict: a mass rewrite of these files is not wanted.** A rewrite of
a 2000+ line file with no test seams is itself the highest-blast-radius
change imaginable — it re-derives behavior no test currently pins, all at
once, in the file most likely to be depended on in ways nobody remembers.
The chosen alternative is incremental: shrink these files only as a
byproduct of doing real work in them, never as a project of its own.

## The policy

**Every change that touches one of the files above must do two things
before the behavioral change itself:**

1. **Extract a seam.** Pull the specific behavior the change needs to touch
   out into its own module with an explicit interface (a function, a class,
   a small set of both) that the hotspot file calls into. Don't edit the
   monolith in place if the edit can instead become "hotspot file calls
   `new_module.thing(...)` instead of running the old inline code." The seam
   is what makes the next change to this behavior *not* a hotspot-file edit
   at all.
2. **Write characterization tests first.** Before changing behavior, add
   tests that pin what the extracted code currently does — including its
   existing quirks and edge cases, not an idealized version of them. Get
   those tests passing against the *unchanged* behavior, then make the
   intended change and update only the assertions the change was meant to
   affect. This is what makes "I extracted this without changing behavior"
   a checked claim instead of a hopeful one.

This applies to new changes, not as a mandate to retroactively refactor
these files. A change that doesn't touch one of these files is unaffected.
A change that must touch one (a bug fix, a small feature) should come out
the other side having *added* a seam and a characterization test, and
having grown the hotspot file as little as possible — ideally not at all,
since the new logic now lives in the extracted module.

## Mechanical check: the line-count ceiling

Seam extraction and "characterization tests were written first" are
procedural — nothing short of code review can confirm a test actually
pins pre-existing behavior versus asserting the new behavior. What *is*
cheap to check mechanically is the visible symptom of skipping the policy:
the hotspot file keeps growing. So that's what's enforced:

- `tests/architecture/HOTSPOT_LINE_BASELINE.json` freezes each file's line
  count as of 2026-09-02 (the table's "freeze time" column above).
- `tests/architecture/test_hotspot_line_fitness.py` fails if any hotspot
  file's current line count exceeds its frozen ceiling.
- **Shrinking is always allowed** and is the desired direction — pulling
  code out through a seam lowers the count, and the gate never blocks that.
  The ceiling itself is not required to drop when a file shrinks; leaving
  headroom is fine, since the point is preventing unbounded growth, not
  ratcheting the ceiling down after every change.
- **Growing is blocked.** A change that would push a hotspot file's line
  count past its ceiling fails CI. If the change truly cannot avoid net
  growth (e.g. a genuinely new responsibility that has nowhere else to
  live yet), that's a deliberate, reviewed baseline edit — see below — not
  something to route around.

This is the same mechanism shape as the existing AIOS boundary fitness gate
(`docs/AIOS_BOUNDARY.md`, `tests/architecture/AIOS_BOUNDARY_BASELINE.json`):
a frozen JSON snapshot, a pytest gate collected by the required `pytest -q`
CI step, and a documented, reviewed procedure for changing the snapshot.

### Changing the baseline

Editing `tests/architecture/HOTSPOT_LINE_BASELINE.json` is a reviewed
architectural decision, not a routine fix for a failing gate. Legitimate
reasons:

1. **Lower a ceiling** after a shrink, to lock the gain in and stop the file
   from creeping back up to its old size.
2. **Raise a ceiling** only with an explanation in the PR description of why
   the new responsibility cannot live behind a seam in its own module.
3. **Add or remove a file** from the tracked set — e.g. a hotspot file was
   deleted, renamed (rename it in the baseline with its current line count
   as the new ceiling), or a new file has grown into hotspot territory and
   the team wants to start freezing it too.

Regenerate the frozen count for a single file with:

```
wc -l <path>
```

and edit the corresponding `ceilings` entry directly — there is no
generator script, deliberately: the file list is a short, hand-picked set
from the audit, not a tree-wide scan.

## CI wiring

`tests/architecture/` is collected by the required `pytest -q` step in
`.github/workflows/ci.yml`, and by the dedicated `AIOS boundary fitness`
workflow (`.github/workflows/arch-fitness.yml`, `pytest tests/architecture -q`)
as a fast, minimal-dependency, named status. Both already glob the whole
`tests/architecture/` package, so the hotspot gate runs under both without
further workflow changes.
