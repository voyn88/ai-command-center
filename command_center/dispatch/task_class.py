"""Generates the `task_class` bucket key `dispatch.rating` groups ratings by
(VOYN-W0-AICC-AGENT-MARKETPLACE).

`dispatch.rating.LedgerEntry.task_class` is deliberately an opaque,
caller-supplied string — that module's docstring is explicit that it "does
not invent a taxonomy; the tree of task classes is meant to be generated from
the observed task space elsewhere, not declared here." This module is that
elsewhere.

The owning idea calls for a tree that is *generated*, not hand-drawn: nodes
should come from the task space the platform actually observes (repository ×
work type × domain × language × risk), never from a curated list that goes
stale the moment the repository shifts. Only two of those dimensions are
attributes the platform records on every task today — the project/repository
(`task["project"]`, set by `tasks_repository.create_task`) and the work type
(`task["task_type"]`, e.g. "implementation"/"review"/"final_gate"/
"architecture_review", the same field `task_pipeline` already reads to pick a
capability profile) — so `task_class_for` composes only those two. Domain,
language and risk are not yet first-class attributes anywhere in this
codebase; folding them in before they exist would mean inventing values this
module has no authority to observe, exactly the hand-drawn taxonomy the idea
rejects. Widening the key later is a matter of widening this function's
inputs as more attributes become real task-space observations.
"""

from __future__ import annotations

#: Bucket fragments for a task missing the corresponding attribute. Kept
#: distinct from any real value so an unset field is never silently
#: indistinguishable from a project or task type that happens to share the
#: same name.
_UNASSIGNED_PROJECT = "unassigned"
_UNSPECIFIED_TASK_TYPE = "unspecified"


def task_class_for(*, project: str | None, task_type: str | None) -> str:
    """The `task_class` for one task, generated from its own recorded
    attributes rather than declared by a curated list.

    Total: never raises, and a missing or blank `project`/`task_type` falls
    back to an explicit sentinel instead of colliding with a task whose field
    happens to be missing for a different reason.
    """
    proj = (
        project.strip()
        if isinstance(project, str) and project.strip()
        else _UNASSIGNED_PROJECT
    )
    kind = (
        task_type.strip()
        if isinstance(task_type, str) and task_type.strip()
        else _UNSPECIFIED_TASK_TYPE
    )
    return f"{proj}:{kind}"
