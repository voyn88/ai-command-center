"""Request bodies and response wrappers for the Wave-3 model-registry surface.

The *entities* returned here are the shared contract models in
:mod:`command_center.api.models` (``ModelEntry``, ``ModelEvent``) — these classes
describe the **inputs** a client POSTs and the small composite responses (a list
page, a download result, an assignment result, a history) that wrap those
entities.

Kept separate from ``models.py`` on purpose: the entity skeletons are the
read/response contract both shells code against; request shapes are an
implementation detail of this backend and evolve independently.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from command_center.api.models import ModelEntry, ModelEvent, ModelKind


class RegisterModelRequest(BaseModel):
    """POST body for ``/models``. ``name`` and ``kind`` are required — a catalog
    entry always has a display name and is either external or local. The rest is
    optional metadata the auto-select helper weighs and the provenance record."""

    name: str
    kind: ModelKind
    provider: str | None = None
    cost: float | None = None
    quality: float | None = None
    latency_ms: int | None = None
    provenance: str | None = None
    actor: str | None = None


class DownloadModelRequest(BaseModel):
    """POST body for ``/models/{id}/download``. Optional ``provenance`` overrides
    the entry's recorded origin for this transfer; ``actor`` labels who requested
    it in the governance log."""

    provenance: str | None = None
    actor: str | None = None


class AssignModelRequest(BaseModel):
    """POST body for ``/models/{id}/assign``. ``target_ref`` is the opaque
    task/agent the model is bound to. ``sensitive`` marks the context as carrying
    sensitive data — an external model is refused for it (the guard)."""

    target_ref: str
    sensitive: bool = False
    actor: str | None = None
    provenance: str | None = None


class ModelList(BaseModel):
    """A page of catalog entries plus the paging echo the client sent."""

    models: list[ModelEntry] = Field(default_factory=list)
    limit: int
    offset: int

    model_config = {"protected_namespaces": ()}


class DownloadModelResult(BaseModel):
    """The outcome of a download request: the model after the lifecycle ran
    (``installed`` on success, ``error`` on failure) and the ordered progress
    ticks recorded along the way."""

    model: ModelEntry
    progress: list[int] = Field(default_factory=list)

    model_config = {"protected_namespaces": ()}


class AssignModelResponse(BaseModel):
    """Result of assigning a model: the entry and the target it was bound to."""

    model: ModelEntry
    target_ref: str

    model_config = {"protected_namespaces": ()}


class ModelHistory(BaseModel):
    """A model's full governance log, oldest → newest — the traceable history."""

    model_id: str
    events: list[ModelEvent] = Field(default_factory=list)

    model_config = {"protected_namespaces": ()}
