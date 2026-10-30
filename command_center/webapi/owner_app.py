"""Read-only server deployment of the existing React client.

The authenticated edge supplies a private upstream credential. This factory
has no database credential, scheduler, local task store, or mutation routes.
Its input is the canonical backlog export, never a second task database.
"""
from __future__ import annotations

import os
import secrets
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from command_center.backlog_client import load_projection
from native_gateway.redaction import sanitize_value


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


def create_app() -> FastAPI:
    credential = os.environ.get('AICC_OWNER_UPSTREAM_KEY', '')
    if len(credential) < 32:
        raise RuntimeError('AICC_OWNER_UPSTREAM_KEY must contain at least 32 characters')
    projection = Path(os.environ['AICC_MASTER_BACKLOG'])
    dist = Path(os.environ.get('AICC_WEB_DIST', str(Path(__file__).resolve().parents[2] / 'web' / 'dist')))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        expected = 'Bearer ' + credential
        supplied = request.headers.get('authorization', '')
        if not secrets.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({'detail': 'Authentication required'}, status_code=401)
        if request.method not in {'GET', 'HEAD'}:
            return JSONResponse({'detail': 'Read-only deployment'}, status_code=405)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.get('/api/backlog')
    def backlog():
        return read_backlog(projection)

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    app.mount('/', StaticFiles(directory=dist, html=True), name='spa')
    return app
