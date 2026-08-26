"""Integration Center (AICC-INT-001) — AICC as the control center for every
project in the ecosystem.

Increment 1 (see ``docs/INTEGRATION_CENTER.md``): a project registry
(``registry.py``, the single writer of ``data/integration_registry.json``)
and strictly read-only health collectors (``collectors.py``). Rendering lives
in ``command_center/ui/integration_center.py`` — this package is
Streamlit-free, like every non-``ui`` package under ``command_center``.
"""
