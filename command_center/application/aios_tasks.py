"""The sole adapter from AICC's task contract to the public AIOS SDK."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from command_center import storage, tasks_repository
from command_center.application import aios_status, tasks_gateway

logger = logging.getLogger(__name__)

_AICC_PRIORITY_TO_INT = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}
_INT_TO_AICC_PRIORITY = {value: key for key, value in _AICC_PRIORITY_TO_INT.items()}

# These are the only product fields allowed to leave AICC.  Prompts, notes,
# local paths, provider identities and timelines deliberately stay local.
_SAFE_PAYLOAD_FIELDS = (
    "project",
    "workflow_stage",
    "owner",
    "estimate_hours",
    "depends_on",
    "goal",
    "parent_task_id",
    "untrusted_import",
    "current_stage",
    "progress",
)

_REMOTE_STATE_TO_LANE = {
    "open": "Backlog",
    "assigned": "Next",
    "in_progress": "In Progress",
    "completed": "Done",
}

_LANE_TO_TARGET_STATE = {
    "Backlog": "open",
    "Next": "assigned",
    "In Progress": "in_progress",
    "Done": "completed",
}


_EXPECTED_SDK_VERSION = "0.2.0"
_EXPECTED_API_MAJOR = 1
_EXPECTED_STATUS_SOURCES = {
    "liveness": "getHealth",
    "readiness": "getReadiness",
    "identity_and_capabilities": "whoAmI",
    "workspace_activity": "listWorkspaceTimeline",
}


def validate_aios_sdk_contract(sdk: ModuleType | Any) -> None:
    """Reject a mutable or incompatible SDK before any network request."""
    contract = getattr(sdk, "SDK_CONTRACT", None)
    if (
        getattr(sdk, "__version__", None) != _EXPECTED_SDK_VERSION
        or getattr(contract, "distribution", None) != "aios-sdk"
        or getattr(contract, "sdk_version", None) != _EXPECTED_SDK_VERSION
        or getattr(contract, "api_major", None) != _EXPECTED_API_MAJOR
        or dict(getattr(contract, "core_status_sources", {}))
        != _EXPECTED_STATUS_SOURCES
        or tuple(getattr(contract, "unknown_evidence", ()))
        != ("accepted_sha", "deployed_sha")
        or not set(_EXPECTED_STATUS_SOURCES.values()).issubset(
            set(getattr(contract, "callable_operations", ()))
        )
    ):
        raise aios_status.AIOSStatusContractError("incompatible_aios_sdk")


def _load_aios_sdk() -> ModuleType:
    # This local, top-level-only import is the one mechanically allowed SDK
    # dependency in AICC production code.
    import aios_sdk

    validate_aios_sdk_contract(aios_sdk)
    return aios_sdk


class AIOSSDKStatusClient:
    """The SDK-backed implementation of AICC's read-only status port."""

    def __init__(
        self,
        client: Any,
        *,
        tenant_id: str,
        workspace_id: str,
        sdk: ModuleType | Any | None = None,
    ) -> None:
        self._client = client
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._sdk = sdk or _load_aios_sdk()
        validate_aios_sdk_contract(self._sdk)

    def _translate(self, error: Exception) -> aios_status.AIOSStatusError:
        request_id = getattr(error, "request_id", None)
        code = str(getattr(error, "code", None) or type(error).__name__)
        if isinstance(error, self._sdk.AuthenticationError):
            return aios_status.AIOSStatusAuthenticationError(code, request_id=request_id)
        if isinstance(error, self._sdk.AuthorizationError):
            return aios_status.AIOSStatusAuthorizationError(code, request_id=request_id)
        if isinstance(error, self._sdk.TimeoutError):
            return aios_status.AIOSStatusTimeoutError(code, request_id=request_id)
        if isinstance(error, self._sdk.ContractError):
            return aios_status.AIOSStatusContractError(code, request_id=request_id)
        return aios_status.AIOSStatusRemoteError(code, request_id=request_id)

    @staticmethod
    def _evidence(event: str, response: Any) -> aios_status.AIOSStatusEvidence:
        request_id = getattr(response, "request_id", None)
        if request_id is not None and not isinstance(request_id, str):
            raise aios_status.AIOSStatusContractError("invalid_request_id")
        return aios_status.AIOSStatusEvidence(event, request_id)

    def get_core_status(self) -> aios_status.AIOSCoreStatus:
        try:
            return self._get_core_status()
        except aios_status.AIOSStatusError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise aios_status.AIOSStatusContractError(
                "invalid_status_contract"
            ) from None

    def _get_core_status(self) -> aios_status.AIOSCoreStatus:
        try:
            health = self._client.health.get()
            readiness = self._client.readiness.get()
            identity = self._client.identity.who_am_i()
            timeline = self._client.workspaces.list_timeline(
                self._workspace_id, limit=50
            )
        except self._sdk.AIOSSDKError as error:
            raise self._translate(error) from None

        health_data = health.data
        readiness_data = readiness.data
        identity_data = identity.data
        if getattr(identity_data, "tenant_id", None) != self._tenant_id:
            raise aios_status.AIOSStatusTenantError("tenant_mismatch")
        health_value = getattr(health_data, "status", None)
        readiness_value = getattr(readiness_data, "status", None)
        health_version = getattr(health_data, "build_version", None)
        readiness_version = getattr(readiness_data, "build_version", health_version)
        capabilities = getattr(identity_data, "capabilities", None)
        if (
            not isinstance(health_value, str)
            or readiness_value not in {"ready", "not_ready"}
            or not isinstance(health_version, str)
            or not health_version
            or readiness_version != health_version
            or not isinstance(capabilities, (tuple, list))
            or any(not isinstance(value, str) or not value for value in capabilities)
            or len(getattr(timeline, "items", ())) > 50
        ):
            raise aios_status.AIOSStatusContractError("invalid_status_contract")

        events: list[aios_status.AIOSStatusTimelineEvent] = []
        for item in timeline.items:
            event_id = getattr(item, "id", None)
            event_type = getattr(item, "event_type", None)
            occurred_at = getattr(item, "occurred_at", None)
            if (
                not isinstance(event_id, str)
                or not event_id
                or not isinstance(event_type, str)
                or not event_type
                or not isinstance(occurred_at, datetime)
                or occurred_at.tzinfo is None
            ):
                raise aios_status.AIOSStatusContractError("invalid_timeline_event")
            # Deliberately copy only the allowlisted fields. Actor, subject and
            # payload can contain principals, prompts and local paths.
            events.append(
                aios_status.AIOSStatusTimelineEvent(
                    event_id=event_id,
                    event_type=event_type,
                    occurred_at=occurred_at,
                )
            )

        return aios_status.AIOSCoreStatus(
            readiness=(
                aios_status.AIOSCoreReadiness.READY
                if readiness_value == "ready" and health_value == "ok"
                else aios_status.AIOSCoreReadiness.NOT_READY
            ),
            source="AIOS SDK",
            version=health_version,
            health=health_value,
            tenant_id=self._tenant_id,
            capabilities=tuple(capabilities),
            evidence=(
                self._evidence("health.get", health),
                self._evidence("readiness.get", readiness),
                self._evidence("identity.who_am_i", identity),
                self._evidence("workspaces.list_timeline", timeline),
            ),
            timeline=tuple(events),
            accepted_sha=None,
            deployed_sha=None,
        )

    def close(self) -> None:
        self._client.close()


def build_aios_status_client(
    *,
    url: str,
    token: str,
    tenant_id: str,
    workspace_id: str,
    sdk: ModuleType | Any | None = None,
    transport: Any | None = None,
) -> AIOSSDKStatusClient:
    sdk_module = sdk or _load_aios_sdk()
    validate_aios_sdk_contract(sdk_module)
    kwargs = {"transport": transport} if transport is not None else {}
    client = sdk_module.AIOSClient(url, token=token, **kwargs)
    return AIOSSDKStatusClient(
        client,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        sdk=sdk_module,
    )


def task_idempotency_key(task: dict[str, Any]) -> str:
    """Stable, non-secret identity for create replay across crash windows."""
    aicc_id = str(task.get("id") or "")
    if not aicc_id:
        raise tasks_gateway.TasksGatewayContractError("task id is required for AIOS create")
    digest = hashlib.sha256(f"aicc-task-v1\0{aicc_id}".encode()).hexdigest()
    return f"aicc-task-v1-{digest}"


def _target_state(lane: str) -> str:
    try:
        return _LANE_TO_TARGET_STATE[lane]
    except KeyError as error:
        raise tasks_gateway.UnsupportedTaskTransitionError(
            f"AICC lane {lane!r} has no lossless AIOS task transition"
        ) from error


def aicc_dict_to_create_request(
    task: dict[str, Any],
) -> tuple[tasks_gateway.CreateTaskDTO, str]:
    lane = str(task.get("status") or "Backlog")
    target_state = _target_state(lane)
    project = str(task.get("project") or "")
    task_id = str(task.get("id") or "")
    subject_ref = f"{project}/{task_id}"[:255] if project and task_id else task_id[:255]
    if not subject_ref:
        raise tasks_gateway.TasksGatewayContractError("task id is required for AIOS create")

    payload: dict[str, Any] = {"aicc_id": task_id}
    for field in _SAFE_PAYLOAD_FIELDS:
        value = task.get(field)
        if value is not None:
            payload[field] = value

    return (
        tasks_gateway.CreateTaskDTO(
            subject_ref=subject_ref,
            type=str(task.get("task_type") or "task")[:128],
            title=str(task.get("title") or "Untitled")[:512],
            priority=_AICC_PRIORITY_TO_INT.get(str(task.get("priority") or "Medium"), 2),
            payload=payload,
        ),
        target_state,
    )


def _lane_for_remote_state(state: str) -> str:
    try:
        return _REMOTE_STATE_TO_LANE[state]
    except KeyError as error:
        raise tasks_gateway.UnsupportedTaskStateError(
            f"AIOS task state {state!r} cannot be projected into an AICC lane"
        ) from error


def _evidence_dicts(
    evidence: tuple[tasks_gateway.GatewayEvidence, ...],
) -> list[dict[str, str | None]]:
    return [item.as_dict() for item in evidence]


def aios_task_to_aicc_dict(
    task: tasks_gateway.TaskDTO,
    *,
    evidence: tuple[tasks_gateway.GatewayEvidence, ...] = (),
) -> dict[str, Any]:
    payload = task.payload or {}
    aicc_id = str(payload.get("aicc_id") or task.id)
    now = datetime.now(timezone.utc)
    created_at = task.created_at if isinstance(task.created_at, datetime) else now
    updated_at = task.updated_at if isinstance(task.updated_at, datetime) else now
    result: dict[str, Any] = {
        "id": aicc_id,
        "aios_id": task.id,
        "title": task.title,
        # The remote lifecycle is authoritative.  A stale payload lane cannot
        # promote or roll back a task.
        "status": _lane_for_remote_state(task.state),
        "priority": _INT_TO_AICC_PRIORITY.get(task.priority, "Medium"),
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }
    for field in _SAFE_PAYLOAD_FIELDS:
        if field in payload:
            result[field] = payload[field]
    result.setdefault("project", "")
    result.setdefault("task_type", task.type if task.type != "task" else "")
    result.setdefault("depends_on", [])
    result.setdefault("owner", "")
    result.setdefault("estimate_hours", 0.0)
    result.setdefault("goal", result["title"])
    result.setdefault("workflow_stage", "Draft")
    result.setdefault("timeline", [])
    if evidence:
        result["aios_evidence"] = _evidence_dicts(evidence)
    return result


class AIOSIdMap:
    """Validated, atomically persisted AICC-id to AIOS-id correlation."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_path = path.with_name(f".{path.name}.lock")
        self._lock = threading.Lock()
        with storage.file_lock(self._lock_path):
            self._data = self._load()

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise tasks_gateway.CorruptTaskMapError(
                f"AIOS task map cannot be read: {self._path}"
            ) from error
        if not isinstance(value, dict) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(remote_id, str)
            or not remote_id
            for key, remote_id in value.items()
        ):
            raise tasks_gateway.CorruptTaskMapError(
                f"AIOS task map must contain non-empty string pairs: {self._path}"
            )
        if len(set(value.values())) != len(value):
            raise tasks_gateway.CorruptTaskMapError(
                f"AIOS task map contains duplicate remote identities: {self._path}"
            )
        return value

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(self._data, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            if os.name != "nt":
                directory_fd = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as error:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise tasks_gateway.TaskMapWriteError(
                f"AIOS task map cannot be persisted: {self._path}"
            ) from error

    def get(self, aicc_id: str) -> str | None:
        with self._lock:
            with storage.file_lock(self._lock_path):
                self._data = self._load()
                return self._data.get(aicc_id)

    def put(self, aicc_id: str, aios_id: str) -> None:
        if not aicc_id or not aios_id:
            raise tasks_gateway.TaskMapWriteError("task map identities must be non-empty")
        with self._lock:
            with storage.file_lock(self._lock_path):
                self._data = self._load()
                existing_owner = next(
                    (
                        key
                        for key, value in self._data.items()
                        if value == aios_id and key != aicc_id
                    ),
                    None,
                )
                if existing_owner is not None:
                    raise tasks_gateway.CorruptTaskMapError(
                        "one AIOS task cannot map to multiple AICC task identities"
                    )
                previous = self._data.get(aicc_id)
                self._data[aicc_id] = aios_id
                try:
                    self._save()
                except Exception:
                    if previous is None:
                        self._data.pop(aicc_id, None)
                    else:
                        self._data[aicc_id] = previous
                    raise

    def remove(self, aicc_id: str) -> None:
        with self._lock:
            with storage.file_lock(self._lock_path):
                self._data = self._load()
                if aicc_id not in self._data:
                    return
                previous = self._data.pop(aicc_id)
                try:
                    self._save()
                except Exception:
                    self._data[aicc_id] = previous
                    raise


def _task_dto(task: Any) -> tasks_gateway.TaskDTO:
    return tasks_gateway.TaskDTO(
        id=str(task.id),
        subject_ref=str(task.subject_ref),
        type=str(task.type),
        title=str(task.title),
        state=str(task.state),
        priority=int(task.priority),
        payload=dict(task.payload or {}),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


class AIOSSDKTasksGateway:
    """Thin translation layer; no AIOS model escapes this class."""

    def __init__(self, client: Any, sdk: ModuleType | Any | None = None) -> None:
        self._client = client
        self._sdk = sdk or _load_aios_sdk()

    def _call(self, event: str, call: Any) -> tasks_gateway.TaskResult:
        try:
            response = call()
        except self._sdk.AIOSSDKError as error:
            raise tasks_gateway.TasksGatewayRemoteError(
                str(getattr(error, "code", None) or type(error).__name__),
                request_id=getattr(error, "request_id", None),
                retryable=bool(getattr(error, "retryable", False)),
            ) from None
        return tasks_gateway.TaskResult(
            _task_dto(response.data),
            (tasks_gateway.GatewayEvidence(event, getattr(response, "request_id", None)),),
        )

    def list_tasks(self) -> tasks_gateway.TaskListResult:
        tasks: list[tasks_gateway.TaskDTO] = []
        evidence: list[tasks_gateway.GatewayEvidence] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            try:
                page = self._client.tasks.list(limit=50, cursor=cursor)
            except self._sdk.AIOSSDKError as error:
                raise tasks_gateway.TasksGatewayRemoteError(
                    str(getattr(error, "code", None) or type(error).__name__),
                    request_id=getattr(error, "request_id", None),
                    retryable=bool(getattr(error, "retryable", False)),
                ) from None
            tasks.extend(_task_dto(item) for item in page.items)
            evidence.append(tasks_gateway.GatewayEvidence("task.list", page.request_id))
            if not page.has_more:
                return tasks_gateway.TaskListResult(tuple(tasks), tuple(evidence))
            if page.next_cursor is None or page.next_cursor in seen:
                raise tasks_gateway.TasksGatewayContractError(
                    "AIOS task list returned a missing or repeated cursor"
                )
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    def create_task(
        self, request: tasks_gateway.CreateTaskDTO, *, idempotency_key: str
    ) -> tasks_gateway.TaskResult:
        model = self._sdk.CreateTaskRequest(
            subject_ref=request.subject_ref,
            type=request.type,
            title=request.title,
            priority=request.priority,
            payload=request.payload,
        )
        return self._call(
            "task.create",
            lambda: self._client.tasks.create(model, idempotency_key=idempotency_key),
        )

    def get_task(self, task_id: str) -> tasks_gateway.TaskResult:
        return self._call("task.get", lambda: self._client.tasks.get(task_id))

    def assign_task(self, task_id: str, assignee: str) -> tasks_gateway.TaskResult:
        return self._call("task.assign", lambda: self._client.tasks.assign(task_id, assignee))

    def start_task(self, task_id: str) -> tasks_gateway.TaskResult:
        return self._call("task.start", lambda: self._client.tasks.start(task_id))

    def complete_task(self, task_id: str) -> tasks_gateway.TaskResult:
        return self._call("task.complete", lambda: self._client.tasks.complete(task_id))

    def cancel_task(self, task_id: str) -> tasks_gateway.TaskResult:
        return self._call("task.cancel", lambda: self._client.tasks.cancel(task_id))

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class AIOSTasksRepository:
    """AICC repository facade over the product-owned ``TasksGateway``."""

    def __init__(self, gateway_or_client: Any, id_map: AIOSIdMap) -> None:
        self._gateway: tasks_gateway.TasksGateway = (
            gateway_or_client
            if isinstance(gateway_or_client, tasks_gateway.TasksGateway)
            else AIOSSDKTasksGateway(gateway_or_client)
        )
        self._id_map = id_map

    def load_all(self) -> list[dict[str, Any]]:
        result = self._gateway.list_tasks()
        # Cancelled tasks are explicit remote tombstones, not live AICC board
        # tasks. Other unknown states still fail closed in the projection.
        return [
            aios_task_to_aicc_dict(task, evidence=result.evidence)
            for task in result.tasks
            if task.state != "cancelled"
        ]

    def close(self) -> None:
        close = getattr(self._gateway, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> AIOSTasksRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _reconcile(self, aicc_id: str) -> tasks_gateway.TaskResult | None:
        listed = self._gateway.list_tasks()
        matches = [task for task in listed.tasks if task.payload.get("aicc_id") == aicc_id]
        if len(matches) > 1:
            raise tasks_gateway.TasksGatewayContractError(
                f"multiple AIOS tasks claim AICC identity {aicc_id!r}"
            )
        if not matches:
            return None
        self._id_map.put(aicc_id, matches[0].id)
        return tasks_gateway.TaskResult(matches[0], listed.evidence)

    def _current(self, aicc_id: str) -> tasks_gateway.TaskResult | None:
        remote_id = self._id_map.get(aicc_id)
        if remote_id:
            return self._gateway.get_task(remote_id)
        return self._reconcile(aicc_id)

    def _advance(
        self,
        result: tasks_gateway.TaskResult,
        target_state: str,
        *,
        assignee: str = "",
    ) -> tasks_gateway.TaskResult:
        current = result.task.state
        evidence = list(result.evidence)
        if current == "cancelled":
            raise tasks_gateway.UnsupportedTaskStateError(
                "cancelled AIOS tasks have no lossless AICC lane"
            )
        if current == target_state:
            return result
        if target_state == "assigned":
            if current != "open" or not assignee:
                raise tasks_gateway.UnsupportedTaskTransitionError(
                    "Next requires an open task and a non-empty owner for AIOS assignment"
                )
            transitioned = self._gateway.assign_task(result.task.id, assignee)
            return tasks_gateway.TaskResult(transitioned.task, tuple(evidence) + transitioned.evidence)
        if target_state == "in_progress":
            if current not in {"open", "assigned"}:
                raise tasks_gateway.UnsupportedTaskTransitionError(
                    f"cannot transition AIOS task from {current!r} to in_progress"
                )
            transitioned = self._gateway.start_task(result.task.id)
            return tasks_gateway.TaskResult(transitioned.task, tuple(evidence) + transitioned.evidence)
        if target_state == "completed":
            if current in {"open", "assigned"}:
                started = self._gateway.start_task(result.task.id)
                evidence.extend(started.evidence)
                result = started
                current = started.task.state
            if current != "in_progress":
                raise tasks_gateway.UnsupportedTaskTransitionError(
                    f"cannot transition AIOS task from {current!r} to completed"
                )
            completed = self._gateway.complete_task(result.task.id)
            return tasks_gateway.TaskResult(completed.task, tuple(evidence) + completed.evidence)
        raise tasks_gateway.UnsupportedTaskTransitionError(
            f"cannot reverse AIOS task from {current!r} to {target_state!r}"
        )

    def create(self, task_dict: dict[str, Any], **_: Any) -> dict[str, Any]:
        request, target_state = aicc_dict_to_create_request(task_dict)
        aicc_id = str(task_dict.get("id") or "")
        remote_id = self._id_map.get(aicc_id)
        if remote_id:
            result = self._gateway.get_task(remote_id)
        else:
            result = self._gateway.create_task(
                request,
                idempotency_key=task_idempotency_key(task_dict),
            )
            claimed_id = result.task.payload.get("aicc_id")
            if claimed_id != aicc_id:
                raise tasks_gateway.TasksGatewayContractError(
                    "AIOS create response did not preserve the requested AICC identity"
                )
            self._id_map.put(aicc_id, result.task.id)
        result = self._advance(
            result,
            target_state,
            assignee=str(task_dict.get("owner") or ""),
        )
        return aios_task_to_aicc_dict(result.task, evidence=result.evidence)

    def upsert_all(self, tasks: list[dict[str, Any]]) -> None:
        for task in tasks:
            self.upsert(task)

    def upsert(self, task_dict: dict[str, Any]) -> None:
        if tasks_repository.is_master_projection_task(task_dict):
            # Mirrors the structural drop in `tasks_repository.save_tasks`
            # (VOYN-W0-AICC-WIRE-BACKLOG-API): a master-projection record is
            # a read-only view of the canonical backlog, never a second task
            # store — every backend this repository facade can front must
            # refuse to persist one, not just the default JSON store. Today
            # no caller routes a master record here (`get_repository` only
            # returns this class under `AICC_TASKS_BACKEND=aios`, and that
            # backend never reads the backlog projection), but the guard
            # keeps "no second task store" true regardless of backend.
            logger.warning(
                "AIOSTasksRepository.upsert: ignored master projection record %s",
                task_dict.get("id"),
            )
            return
        aicc_id = str(task_dict.get("id") or "")
        if self._current(aicc_id) is None:
            self.create(task_dict)
        else:
            self.update_status(aicc_id, str(task_dict.get("status") or "Backlog"))

    def update_status(self, task_id: str, new_status: str) -> dict[str, Any] | None:
        current = self._current(task_id)
        if current is None:
            return None
        target = _target_state(new_status)
        assignee = str(current.task.payload.get("owner") or "")
        result = self._advance(current, target, assignee=assignee)
        return aios_task_to_aicc_dict(result.task, evidence=result.evidence)

    def delete(self, task_id: str) -> bool:
        current = self._current(task_id)
        if current is None:
            return False
        if current.task.state == "cancelled":
            # Already deleted remotely (e.g. a replayed delete after the map
            # entry was removed): a second cancel would be an
            # invalid_transition upstream, so treat it as the no-op it is.
            self._id_map.remove(task_id)
            return False
        self._gateway.cancel_task(current.task.id)
        self._id_map.remove(task_id)
        return True


def build_aios_tasks_repository(
    *,
    url: str,
    token: str,
    map_path: Path,
    id_map: AIOSIdMap | None = None,
    sdk: ModuleType | Any | None = None,
    transport: Any | None = None,
) -> AIOSTasksRepository:
    sdk_module = sdk or _load_aios_sdk()
    kwargs = {"transport": transport} if transport is not None else {}
    client = sdk_module.AIOSClient(url, token=token, **kwargs)
    return AIOSTasksRepository(
        AIOSSDKTasksGateway(client, sdk_module),
        id_map if id_map is not None else AIOSIdMap(map_path),
    )
