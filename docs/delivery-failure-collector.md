# Delivery failure collection

`command_center.orchestrator.delivery_failure_collector` adds two bounded calls for the existing control-plane and release owner. It creates no daemon, queue, task, retry or deployment. The feature must be enabled; STOP may hold assignments while observations continue. Callers must inspect `CollectionResult`: `admitted` means SQL admission, not acceptance of delivery and not proof of an outer transaction's commit. `ignored` is not PASS; `refused` is missing/unusable evidence, not an invented failure.

## CI integration

Invoke `collect_ci_failure(connection, task_id=..., revision=..., expected_sha=..., run_id=..., attempt=...)` for a known workflow attempt from the existing CI observation path. Call inside the established `gh_access.tick` with fleet identity and ambient fallback disabled. The collector refuses absent, degraded, ambient or fallback-enabled quota contexts. It does not rerun checks or enumerate the backlog.

The canonical task must be enrolled at a current revision and the SHA must already be linked in canonical evidence. Historical linked SHAs remain eligible for incident observation; a later successful build does not erase the original failure. Repository routing comes from `planner.repo_route` and the trusted checkout origin. The collector reads exactly one GitHub Actions REST attempt and checks its run ID, attempt, repository, SHA, completed status and timestamp. Only `failure` and `timed_out` create incidents. Cancellation, skipped/neutral/action-required runs and incomplete runs do not become product defects. Lookup/auth errors are unavailable observations.

Run/attempt identity is stable under replay and distinguishes a later attempt. Provider titles, arbitrary HTML URLs and logs are never copied into the ledger. The retained reference is the attempt-specific GitHub URL. The existing runner owns API quota accounting, task mapping and when to invoke collection.

## Deployment integration

Invoke `collect_deploy_failure(connection, task_id=..., revision=..., expected_sha=..., source=DeploymentSource(...), offset=..., sha256=...)` after the existing release owner retains an actual self-deploy provenance row. `DeploymentSource` is trusted operator configuration binding canonical repo, host ID, logical source ID and an absolute protected JSONL path. Do not accept it, the byte offset or checksum from a role/model payload.

The existing self-deployer writes `at`, `outcome`, `detail`, `previous_sha` and `target_sha`. The row does not authenticate a host or repository; that binding comes from the configured release source. Hash the exact complete line bytes, including the final newline. The collector reads at most 64 KiB, requires a line boundary, rejects symlinks/nonregular files, verifies the exact hash/schema/timestamp/SHA, and admits only `failed` or `rolled_back`. Partial rows, unavailable/rotated logs and wrong hashes are refusals. `noop`, `deployed` and policy `refused` outcomes are not deployment failures.

The artifact URI identifies the configured host/source/byte offset and line digest. The release owner must retain that source mapping and exact row, including through rotation; the collector does not publish raw stderr or provide a new artifact service. Use the existing authorized evidence retrieval path for protected source records. Do not advertise the URI as remotely retrievable until that mapping is configured and verified. Only constant safe descriptions, source identity and digest enter the ledger. Raw `detail` can contain credentials or paths and stays in the protected source journal.

A successful rollback is recorded as a failed deployment attempt, not a statement that production remains unhealthy. Failures before a known target/previous SHA cannot be attached to an arbitrary task; keep them in the existing infrastructure incident path.

## Rollout ownership

This module is a producer for retained facts, not a second writer or release controller. The existing integrator must wire these calls into its CI/release observation transaction and configure the trusted source mapping. Current services are not changed by adding this module. PostgreSQL independently rechecks task revision/repository on admission; callers remain responsible for their surrounding transaction and must not treat a refused result as persistence. A real pilot must verify one naturally observed event and replay without manufacturing a failure or receipt.
