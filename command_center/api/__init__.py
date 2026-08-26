"""HTTP/JSON API service for the AI Command Center (Wave 0, F-1).

A FastAPI application (``uvicorn command_center.api.app:app``) that turns the
desktop and future mobile shells into thin clients over one backend.

It began as a read-only surface built ON the existing, already audited read
paths (the Integration Center registry + health collectors, the tasks
repository, the runtime run read model, git readers). It is **no longer
read-only**: the Wave-1 through Wave-3 surfaces added 27 mutating routes to it.
Saying otherwise here is not a cosmetic error — the stale wording is the
plausible reason the mutating surface was long counted as two endpoints rather
than 29 (``VOYN-W0-AICC-AUTH-HTTP-01a``). Every one of those routes is
authenticated and authorized through :mod:`command_center.http_auth`, whose
routing table is checked against the live router tree at application build.
Streamlit code is still untouched and supervisor/db semantics are unchanged.

Layering (per the project's Controller -> Service -> Repository rule):

    app.py      -- FastAPI routes (controllers); no business logic
    service.py  -- read-only aggregation over the existing modules
    schemas.py  -- Pydantic response models (the wire contract both shells use)
    models.py   -- typed skeletons for the not-yet-built "new engine" entities

``models.py`` deliberately carries no persistence: it exists so the contract
for Proposals (Советник), Board motions/votes/decisions, audit runs, incidents,
the model catalog, and the owner day/digest surfaces is visible now, before any
of it is wired to storage.
"""
