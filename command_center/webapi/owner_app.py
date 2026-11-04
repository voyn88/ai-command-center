"""Server deployment of the existing React client, with optional owner commands.

The authenticated edge supplies separate private read and command credentials.
Reads use the canonical backlog export. Explicitly enabled commands use the
canonical database and planner; neither mode creates a local task store.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import secrets
import time
from contextlib import asynccontextmanager
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from command_center.backlog_client import load_projection
from command_center.deployment.runtime_version import capture_runtime_version
from native_gateway.redaction import sanitize_value


# Resolve while this module is imported, before a deployment can switch current.
_OWNER_MODULE_FILE = Path(__file__).resolve()


def _project(owner: str) -> str:
    # Only names of explicitly recognized repositories leave the server.
    name = owner.strip().lower().removesuffix('.git').rstrip('/').split('/')[-1]
    return {'ai-command-center': 'AICC', 'aicc': 'AICC', 'aios': 'AIOS',
            'voyn-logistics-crm': 'VOYN', 'crm': 'VOYN'}.get(name, 'Не указан')


def read_backlog(path: Path) -> dict:
    try:
        projection = load_projection(path)
    except (OSError, UnicodeError):
        raise HTTPException(503, 'Список задач временно недоступен') from None
    if not projection.exists or projection.errors:
        raise HTTPException(503, 'Список задач временно недоступен')
    records = projection.records
    modified = projection.source_mtime or 0
    return {
        'source': 'server_backlog',
        'read_only': True,
        'updated_at': datetime.fromtimestamp(modified, timezone.utc).isoformat(),
        'stale': time.time() - modified > 180,
        'total': len(records),
        'tasks': [{
            'id': sanitize_value(r.issue_id),
            'title': sanitize_value(r.title),
            'status': sanitize_value(r.status),
            'priority': r.priority if r.priority in {'P0', 'P1', 'P2', 'P3'} else None,
            'project': _project(r.owner),
            'updated_at': r.ts,
        } for r in records],
        'counts': dict(Counter(r.status for r in records)),
    }



def _read_team_artifact(root: Path | None, name: str) -> bytes | None:
    """Serve only allowlisted observation shapes; serving does not admit evidence."""
    if (root is None or not root.is_absolute()
            or not re.fullmatch(r'[a-f0-9]{64}\.json', name)
            or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')
            or os.open not in os.supports_dir_fd):
        return None
    directory_fd = None
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                return None
            raw = stream.read(65537)
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != name[:-5]:
            return None

        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate field')
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=unique_fields)
        fields = {
            'schema', 'task_id', 'revision', 'repo', 'environment', 'sha',
            'version_url', 'health_url', 'health', 'criteria', 'started_at',
            'observed_at', 'business_acceptance',
        }
        if not isinstance(value, dict):
            return None
        schema = value.get('schema')
        if schema == 'delivery-release-observation/v1':
            if set(value) != fields or value.get('business_acceptance') is not False:
                return None
            return raw

        # Exact shapes from deployment.delivery_receipts admission adapters.
        # Producer trust and authoritative SQL scope remain operator decisions.
        common = {'schema', 'revision', 'sha', 'criterion_id', 'check_id',
                  'outcome', 'observed_at'}
        if schema == 'delivery-acceptance-observation/v1':
            fields = common | {'task_id', 'environment'}
            identifiers = ('task_id', 'criterion_id', 'check_id')
            revisions = ('revision',)
            if value.get('environment') != 'production':
                return None
        elif schema == 'delivery-corrective-observation/v1':
            fields = common | {'parent_task_id', 'incident_key',
                               'corrective_task_id', 'corrective_revision'}
            identifiers = ('parent_task_id', 'corrective_task_id', 'criterion_id', 'check_id')
            revisions = ('revision', 'corrective_revision')
            if (not isinstance(value.get('incident_key'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', value['incident_key'])):
                return None
        else:
            return None
        if (set(value) != fields or value.get('outcome') != 'passed'
                or any(not isinstance(value.get(key), str) or not value[key].strip()
                       for key in identifiers)
                or any(type(value.get(key)) is not int or value[key] < 1
                       for key in revisions)
                or not isinstance(value.get('sha'), str)
                or not re.fullmatch(r'[a-f0-9]{40}', value['sha'])
                or not isinstance(value.get('observed_at'), str)):
            return None
        observed_at = datetime.fromisoformat(value['observed_at'].replace('Z', '+00:00'))
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return None
        return raw
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

def create_app() -> FastAPI:
    runtime_version = capture_runtime_version(_OWNER_MODULE_FILE)
    artifact_setting = os.environ.get('AICC_TEAM_ARTIFACT_DIR', '')
    artifact_root = Path(artifact_setting) if artifact_setting else None
    credential = os.environ.get('AICC_OWNER_UPSTREAM_KEY', '')
    if len(credential) < 32:
        raise RuntimeError('AICC_OWNER_UPSTREAM_KEY must contain at least 32 characters')
    projection = Path(os.environ['AICC_MASTER_BACKLOG'])
    dist = Path(os.environ.get('AICC_WEB_DIST', str(Path(__file__).resolve().parents[2] / 'web' / 'dist')))
    command_key = os.environ.get('AICC_OWNER_COMMAND_KEY', '')
    if command_key and (len(command_key) < 32 or command_key == credential):
        raise RuntimeError('Command credential must be distinct and at least 32 characters')
    if command_key and not os.environ.get('AICC_OWNER_ID'):
        raise RuntimeError('Command credential must be bound to AICC_OWNER_ID')

    @asynccontextmanager
    async def lifespan(app):
        uses_database = bool(command_key or os.environ.get('AICC_PG_HOST'))
        if uses_database:
            from command_center.db import pool
            pool.open_pool()
        try:
            yield
        finally:
            if uses_database:
                pool.close_pool()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        is_command_route = request.url.path.startswith('/api/owner/')
        required = command_key if is_command_route else credential
        expected = 'Bearer ' + required
        supplied = request.headers.get('authorization', '')
        if not required or not secrets.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({'detail': 'Authentication required'}, status_code=401)
        if request.method not in {'GET', 'HEAD'} and not (is_command_route and command_key):
            return JSONResponse({'detail': 'Read-only deployment'}, status_code=405)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.get('/api/backlog')
    def backlog():
        return {**read_backlog(projection), 'commands_enabled': bool(command_key), 'read_only': not bool(command_key)}

    @app.get('/api/team')
    def team(limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        from command_center.api.delivery_team_routes import read_team
        return read_team(limit=limit, offset=offset)

    @app.get('/api/team/artifacts/{artifact_name}')
    def team_artifact(artifact_name: str):
        raw = _read_team_artifact(artifact_root, artifact_name)
        if raw is None:
            raise HTTPException(404, 'Artifact not found')
        return Response(content=raw, media_type='application/json')

    @app.get('/api/version')
    def version():
        if runtime_version.sha is None:
            return JSONResponse({'status': 'unknown', 'sha': None}, status_code=503)
        return {'status': 'known', 'sha': runtime_version.sha,
                'source': 'immutable_release_at_startup'}

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    if command_key:
        from command_center.webapi.owner_commands import create_router
        app.include_router(create_router())
    app.mount('/', StaticFiles(directory=dist, html=True), name='spa')
    return app
