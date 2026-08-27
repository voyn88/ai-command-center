"""Host deployment operations (VOYN-W0-AICC-DEPLOY-AUTOMATION).

Deliberately NOT under ``command_center/orchestrator``: the AIOS boundary
gate (ADR-0008 / tests/architecture) classifies that path token as frozen
orchestration-engine territory, and self-deploy is host operations -- a
git + systemctl wrapper -- not task-orchestration capability.
"""
