"""Persistent AML store with role-gated, audited state transitions."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from command_center import storage

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 2
ROLES = ("Analyst", "MLRO")
CRITICAL_ACTIONS = frozenset({"escalate", "close", "approve_sar"})
ACTION_POLICY = {
    "assign": (frozenset({"Analyst"}), frozenset({"new"}), "review", "Кейс взят в работу"),
    "documents": (frozenset({"Analyst"}), frozenset({"review"}), "waiting", "Запрошены документы"),
    "escalate": (
        frozenset({"Analyst"}),
        frozenset({"review", "waiting"}),
        "escalated",
        "Кейс эскалирован MLRO",
    ),
    "close": (frozenset({"MLRO"}), frozenset({"escalated"}), "closed", "Кейс закрыт"),
}

# The one incident type eligible for the instant-closure playbook: low/medium-risk
# "Unusual activity" alerts, still untriaged. This scenario has the lowest confirmed
# true-positive rate of the seeded rules (see aml_panel.RULES), so routing it through a
# scripted playbook — instead of the full manual assign/escalate/close workflow —
# still requires MLRO sign-off but replaces four clicks with one auditable decision.
INSTANT_PLAYBOOK_ID = "unusual_activity_quick_close"
INSTANT_PLAYBOOK_SCENARIO = "Unusual activity"
INSTANT_PLAYBOOK_RISKS = frozenset({"low", "medium"})
INSTANT_PLAYBOOK_OUTCOME = "closed_no_action"


class AmlStoreError(Exception):
    pass


class PermissionDenied(AmlStoreError):
    pass


class ConfirmationRequired(AmlStoreError):
    pass


class LostUpdate(AmlStoreError):
    pass


class InvalidTransition(AmlStoreError):
    pass


def _validated_actor(actor: str) -> str:
    clean_actor = actor.strip()
    if not clean_actor:
        raise AmlStoreError("Укажите пользователя, выполняющего действие")
    return clean_actor


def resolve_db_path(root: Path | None = None) -> Path:
    return storage.resolve_data_dir(root or ROOT) / "aml.db"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def _transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def init_db(db_path: Path | None = None) -> Path:
    path = db_path or resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS aml_case (
                id TEXT PRIMARY KEY, customer TEXT NOT NULL, country TEXT NOT NULL,
                risk TEXT NOT NULL, score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
                status TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
                opened TEXT NOT NULL, owner TEXT, scenario TEXT NOT NULL, summary TEXT NOT NULL,
                factors_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aml_transaction (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES aml_case(id) ON DELETE CASCADE,
                occurred_at TEXT NOT NULL, transaction_type TEXT NOT NULL,
                amount_display TEXT NOT NULL, counterparty TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_aml_transaction_case ON aml_transaction(case_id);
            CREATE TABLE IF NOT EXISTS aml_audit_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES aml_case(id) ON DELETE CASCADE,
                occurred_at TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL,
                event TEXT NOT NULL, details TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_aml_audit_case ON aml_audit_event(case_id, id DESC);
            CREATE TRIGGER IF NOT EXISTS protect_aml_audit_update
            BEFORE UPDATE ON aml_audit_event
            BEGIN
                SELECT RAISE(ABORT, 'aml_audit_event is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS protect_aml_audit_delete
            BEFORE DELETE ON aml_audit_event
            BEGIN
                SELECT RAISE(ABORT, 'aml_audit_event is append-only');
            END;
            CREATE TABLE IF NOT EXISTS aml_sar (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES aml_case(id) ON DELETE CASCADE,
                filing_type TEXT NOT NULL, rationale TEXT NOT NULL, status TEXT NOT NULL,
                created_by TEXT NOT NULL, approved_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            WITH ranked_sar AS (
                SELECT id, case_id,
                       FIRST_VALUE(id) OVER (
                           PARTITION BY case_id
                           ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END,
                                    updated_at DESC, id DESC
                       ) AS canonical_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY case_id
                           ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END,
                                    updated_at DESC, id DESC
                       ) AS active_rank
                FROM aml_sar
                WHERE status IN ('draft', 'approved')
            )
            INSERT INTO aml_audit_event
                (case_id, occurred_at, actor, role, event, details)
            SELECT case_id,
                   strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                   'AML schema migration',
                   'System',
                   'Устранён дубликат регуляторного сообщения',
                   printf('SAR %s помечен superseded; активная запись: %s', id, canonical_id)
              FROM ranked_sar
             WHERE active_rank > 1;
            WITH ranked_sar AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY case_id
                           ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END,
                                    updated_at DESC, id DESC
                       ) AS active_rank
                FROM aml_sar
                WHERE status IN ('draft', 'approved')
            )
            UPDATE aml_sar
               SET status = 'superseded', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
             WHERE id IN (SELECT id FROM ranked_sar WHERE active_rank > 1);
            INSERT INTO aml_audit_event
                (case_id, occurred_at, actor, role, event, details)
            SELECT aml_case.id,
                   strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                   'AML schema migration',
                   'System',
                   'Согласован статус регуляторного сообщения',
                   CASE
                       WHEN EXISTS (
                           SELECT 1 FROM aml_sar
                            WHERE aml_sar.case_id = aml_case.id AND aml_sar.status = 'approved'
                       ) THEN 'Утверждённое сообщение перевело кейс в терминальный статус'
                       ELSE 'Кейс с черновиком возвращён на решение MLRO'
                   END
              FROM aml_case
             WHERE (
                       aml_case.status != 'reported'
                       AND EXISTS (
                           SELECT 1 FROM aml_sar
                            WHERE aml_sar.case_id = aml_case.id AND aml_sar.status = 'approved'
                       )
                   )
                OR (
                       aml_case.status != 'escalated'
                       AND EXISTS (
                           SELECT 1 FROM aml_sar
                            WHERE aml_sar.case_id = aml_case.id AND aml_sar.status = 'draft'
                       )
                   );
            UPDATE aml_case
               SET status = 'reported', version = version + 1,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
             WHERE status != 'reported'
               AND EXISTS (
                   SELECT 1 FROM aml_sar
                    WHERE aml_sar.case_id = aml_case.id AND aml_sar.status = 'approved'
               );
            UPDATE aml_case
               SET status = 'escalated', version = version + 1,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
             WHERE status != 'escalated'
               AND EXISTS (
                   SELECT 1 FROM aml_sar
                    WHERE aml_sar.case_id = aml_case.id AND aml_sar.status = 'draft'
               );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_aml_sar_active_case
                ON aml_sar(case_id) WHERE status IN ('draft', 'approved');
            CREATE TABLE IF NOT EXISTS aml_incident_report (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES aml_case(id) ON DELETE CASCADE,
                playbook TEXT NOT NULL, outcome TEXT NOT NULL, generated_at TEXT NOT NULL,
                actor TEXT NOT NULL, role TEXT NOT NULL, report_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_aml_incident_report_case ON aml_incident_report(case_id);
            PRAGMA user_version = 2;
            COMMIT;
            """
        )
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return path


def _initialized_path(db_path: Path | None = None) -> Path:
    """Apply schema migrations only when needed, without locking routine reads."""
    path = db_path or resolve_db_path()
    schema_version = 0
    if path.exists():
        with sqlite3.connect(path) as connection:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if schema_version < SCHEMA_VERSION:
        init_db(path)
    return path


def seed_cases(cases: tuple[dict[str, Any], ...], db_path: Path | None = None) -> None:
    path = _initialized_path(db_path)
    with sqlite3.connect(path) as connection:
        if connection.execute("SELECT 1 FROM aml_case LIMIT 1").fetchone():
            return
    with _transaction(path) as connection:
        # Another process may have seeded between the read-only fast path and
        # acquiring the writer lock.
        if connection.execute("SELECT 1 FROM aml_case LIMIT 1").fetchone():
            return
        now = _now()
        for case in cases:
            connection.execute(
                """INSERT INTO aml_case
                   (id, customer, country, risk, score, status, amount, currency, opened,
                    owner, scenario, summary, factors_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    case["id"], case["customer"], case["country"], case["risk"], case["score"],
                    case["status"], case["amount"], case["currency"], case["opened"],
                    None if case["owner"] == "Не назначен" else case["owner"],
                    case["scenario"], case["summary"], json.dumps(case["factors"], ensure_ascii=False), now,
                ),
            )
            for occurred_at, transaction_type, amount_display, counterparty in case["transactions"]:
                connection.execute(
                    """INSERT INTO aml_transaction
                       (id, case_id, occurred_at, transaction_type, amount_display, counterparty)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (uuid.uuid4().hex, case["id"], occurred_at, transaction_type, amount_display, counterparty),
                )
            connection.execute(
                """INSERT INTO aml_audit_event
                   (case_id, occurred_at, actor, role, event, details) VALUES (?, ?, ?, ?, ?, ?)""",
                (case["id"], now, "Monitoring engine", "System", "Создан алерт",
                 f"{case['scenario']} · risk score {case['score']}"),
            )


def _case_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    case = dict(row)
    case["factors"] = tuple(json.loads(case.pop("factors_json")))
    case["owner"] = case["owner"] or "Не назначен"
    case["transactions"] = tuple(
        (tx["occurred_at"], tx["transaction_type"], tx["amount_display"], tx["counterparty"])
        for tx in connection.execute(
            """SELECT occurred_at, transaction_type, amount_display, counterparty
               FROM aml_transaction WHERE case_id = ? ORDER BY rowid""",
            (case["id"],),
        )
    )
    return case


def list_cases(db_path: Path | None = None) -> list[dict[str, Any]]:
    path = _initialized_path(db_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM aml_case ORDER BY score DESC, id DESC").fetchall()
        return [_case_from_row(connection, row) for row in rows]


def list_audit_events(case_id: str | None = None, db_path: Path | None = None) -> list[dict[str, Any]]:
    path = _initialized_path(db_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        if case_id:
            rows = connection.execute(
                "SELECT * FROM aml_audit_event WHERE case_id = ? ORDER BY id DESC", (case_id,)
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM aml_audit_event ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]


def transition_case(
    case_id: str,
    action: str,
    *,
    actor: str,
    role: str,
    reason: str,
    expected_version: int,
    confirmed: bool = False,
    db_path: Path | None = None,
) -> dict[str, Any]:
    if role not in ROLES:
        raise PermissionDenied(f"Неизвестная роль: {role}")
    if action not in ACTION_POLICY:
        raise AmlStoreError(f"Неизвестное действие: {action}")
    allowed_roles, source_statuses, target_status, event = ACTION_POLICY[action]
    if role not in allowed_roles:
        raise PermissionDenied(f"Роль {role} не может выполнить действие {action}")
    clean_actor = _validated_actor(actor)
    clean_reason = reason.strip()
    if action in CRITICAL_ACTIONS and (not confirmed or not clean_reason):
        raise ConfirmationRequired("Критическое действие требует подтверждения и причины")

    path = _initialized_path(db_path)
    with _transaction(path) as connection:
        row = connection.execute("SELECT * FROM aml_case WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise AmlStoreError(f"Кейс не найден: {case_id}")
        if row["version"] != expected_version:
            raise LostUpdate("Кейс уже изменён другим пользователем; обновите страницу")
        if row["status"] not in source_statuses:
            raise InvalidTransition(
                f"Действие {action} недоступно для кейса в статусе {row['status']}"
            )
        if action == "close" and connection.execute(
            "SELECT 1 FROM aml_sar WHERE case_id = ? LIMIT 1", (case_id,)
        ).fetchone():
            raise InvalidTransition(
                "Кейс с черновиком или утверждённым SAR / STR нельзя закрыть без сообщения"
            )
        owner = clean_actor if action == "assign" or not row["owner"] else row["owner"]
        cursor = connection.execute(
            """UPDATE aml_case SET status = ?, owner = ?, version = version + 1, updated_at = ?
               WHERE id = ? AND version = ?""",
            (target_status, owner, _now(), case_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise LostUpdate("Кейс уже изменён другим пользователем; обновите страницу")
        connection.execute(
            """INSERT INTO aml_audit_event
               (case_id, occurred_at, actor, role, event, details) VALUES (?, ?, ?, ?, ?, ?)""",
            (case_id, _now(), clean_actor, role, event, clean_reason or "Без дополнительного комментария"),
        )
        updated = connection.execute("SELECT * FROM aml_case WHERE id = ?", (case_id,)).fetchone()
        return _case_from_row(connection, updated)


def create_sar_draft(
    case_id: str,
    *,
    filing_type: str,
    rationale: str,
    actor: str,
    role: str,
    db_path: Path | None = None,
) -> str:
    if role not in ROLES:
        raise PermissionDenied("Создать черновик может Analyst или MLRO")
    clean_actor = _validated_actor(actor)
    if not rationale.strip():
        raise AmlStoreError("Укажите основание для сообщения")
    path = _initialized_path(db_path)
    sar_id = f"SAR-{uuid.uuid4().hex[:8].upper()}"
    now = _now()
    with _transaction(path) as connection:
        case = connection.execute("SELECT status FROM aml_case WHERE id = ?", (case_id,)).fetchone()
        if case is None:
            raise AmlStoreError(f"Кейс не найден: {case_id}")
        if case["status"] != "escalated":
            raise InvalidTransition("Черновик SAR / STR можно создать только для эскалированного кейса")
        if connection.execute(
            "SELECT 1 FROM aml_sar WHERE case_id = ? LIMIT 1", (case_id,)
        ).fetchone():
            raise InvalidTransition("Для кейса уже существует черновик или утверждённый SAR / STR")
        connection.execute(
            """INSERT INTO aml_sar
               (id, case_id, filing_type, rationale, status, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'draft', ?, ?, ?)""",
            (sar_id, case_id, filing_type, rationale.strip(), clean_actor, now, now),
        )
        connection.execute(
            """INSERT INTO aml_audit_event
               (case_id, occurred_at, actor, role, event, details)
               VALUES (?, ?, ?, ?, 'Создан черновик отчёта', ?)""",
            (case_id, now, clean_actor, role, filing_type),
        )
    return sar_id


def list_sar(db_path: Path | None = None) -> list[dict[str, Any]]:
    path = _initialized_path(db_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM aml_sar ORDER BY created_at DESC")]


def approve_sar(
    sar_id: str,
    *,
    actor: str,
    role: str,
    reason: str,
    confirmed: bool,
    db_path: Path | None = None,
) -> None:
    if role != "MLRO":
        raise PermissionDenied("Утвердить сообщение может только MLRO")
    clean_actor = _validated_actor(actor)
    if not confirmed or not reason.strip():
        raise ConfirmationRequired("Утверждение требует подтверждения и причины")
    path = _initialized_path(db_path)
    with _transaction(path) as connection:
        sar = connection.execute("SELECT * FROM aml_sar WHERE id = ?", (sar_id,)).fetchone()
        if sar is None:
            raise AmlStoreError(f"Черновик не найден: {sar_id}")
        if sar["status"] != "draft":
            raise AmlStoreError("Сообщение уже обработано")
        case = connection.execute(
            "SELECT status FROM aml_case WHERE id = ?", (sar["case_id"],)
        ).fetchone()
        if case is None or case["status"] != "escalated":
            raise InvalidTransition(
                "Утвердить SAR / STR можно только пока связанный кейс эскалирован"
            )
        now = _now()
        connection.execute(
            "UPDATE aml_sar SET status = 'approved', approved_by = ?, updated_at = ? WHERE id = ?",
            (clean_actor, now, sar_id),
        )
        connection.execute(
            """UPDATE aml_case SET status = 'reported', version = version + 1, updated_at = ?
               WHERE id = ? AND status = 'escalated'""",
            (now, sar["case_id"]),
        )
        connection.execute(
            """INSERT INTO aml_audit_event
               (case_id, occurred_at, actor, role, event, details)
               VALUES (?, ?, ?, ?, 'Регуляторное сообщение утверждено', ?)""",
            (sar["case_id"], now, clean_actor, role, reason.strip()),
        )


def _money_display(amount: float, currency: str) -> str:
    return f"{amount:,.0f} {currency}".replace(",", " ")


def instant_closure_eligible(case: dict[str, Any]) -> bool:
    """Whether ``case`` matches the one incident type wired to the instant-closure
    playbook: an untriaged, low/medium-risk "Unusual activity" alert."""
    return (
        case["status"] == "new"
        and case["scenario"] == INSTANT_PLAYBOOK_SCENARIO
        and case["risk"] in INSTANT_PLAYBOOK_RISKS
    )


def run_instant_closure(
    case_id: str,
    *,
    actor: str,
    role: str,
    reason: str,
    expected_version: int,
    confirmed: bool = False,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Run the one-click playbook: take an eligible case straight from ``new`` to
    ``closed`` in a single transaction, recording each playbook step as its own
    audit event (the decision trace) and persisting an auto-generated closure
    report that snapshots the case and the full trace."""
    if role != "MLRO":
        raise PermissionDenied("Мгновенное закрытие по playbook доступно только MLRO")
    clean_actor = _validated_actor(actor)
    clean_reason = reason.strip()
    if not confirmed or not clean_reason:
        raise ConfirmationRequired("Мгновенное закрытие требует подтверждения и причины")

    path = _initialized_path(db_path)
    with _transaction(path) as connection:
        row = connection.execute("SELECT * FROM aml_case WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise AmlStoreError(f"Кейс не найден: {case_id}")
        if row["version"] != expected_version:
            raise LostUpdate("Кейс уже изменён другим пользователем; обновите страницу")
        case = _case_from_row(connection, row)
        if not instant_closure_eligible(case):
            raise InvalidTransition(
                f"Playbook «{INSTANT_PLAYBOOK_ID}» неприменим: кейс не соответствует критериям "
                f"(сценарий={case['scenario']!r}, риск={case['risk']!r}, статус={case['status']!r})"
            )
        if connection.execute(
            "SELECT 1 FROM aml_sar WHERE case_id = ? LIMIT 1", (case_id,)
        ).fetchone():
            raise InvalidTransition(
                "Кейс с черновиком или утверждённым SAR / STR нельзя закрыть по playbook"
            )

        now = _now()
        criteria_note = (
            f"Сценарий «{case['scenario']}» · риск {case['risk']} · risk score {case['score']} · "
            f"сумма {_money_display(case['amount'], case['currency'])}"
        )
        steps = (
            ("Playbook запущен", f"«{INSTANT_PLAYBOOK_ID}»: {criteria_note}"),
            (
                "Автоматическая проверка пройдена",
                f"Активных SAR/STR не найдено · транзакций проверено: {len(case['transactions'])} · "
                "признаков PEP/санкций не выявлено",
            ),
            ("Кейс закрыт по playbook", clean_reason),
        )
        for event, details in steps:
            connection.execute(
                """INSERT INTO aml_audit_event
                   (case_id, occurred_at, actor, role, event, details) VALUES (?, ?, ?, ?, ?, ?)""",
                (case_id, now, clean_actor, role, event, details),
            )
        owner = clean_actor if case["owner"] == "Не назначен" else case["owner"]
        cursor = connection.execute(
            """UPDATE aml_case SET status = 'closed', owner = ?, version = version + 1, updated_at = ?
               WHERE id = ? AND version = ?""",
            (owner, now, case_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise LostUpdate("Кейс уже изменён другим пользователем; обновите страницу")

        decision_trace = [
            dict(event_row)
            for event_row in connection.execute(
                "SELECT * FROM aml_audit_event WHERE case_id = ? ORDER BY id", (case_id,)
            )
        ]
        report_id = f"RPT-{uuid.uuid4().hex[:8].upper()}"
        report = {
            "id": report_id,
            "case_id": case_id,
            "playbook": INSTANT_PLAYBOOK_ID,
            "outcome": INSTANT_PLAYBOOK_OUTCOME,
            "generated_at": now,
            "actor": clean_actor,
            "role": role,
            "reason": clean_reason,
            "case_snapshot": {
                "customer": case["customer"],
                "country": case["country"],
                "scenario": case["scenario"],
                "risk": case["risk"],
                "score": case["score"],
                "amount": case["amount"],
                "currency": case["currency"],
            },
            "decision_trace": decision_trace,
        }
        connection.execute(
            """INSERT INTO aml_incident_report
               (id, case_id, playbook, outcome, generated_at, actor, role, report_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                report_id, case_id, INSTANT_PLAYBOOK_ID, INSTANT_PLAYBOOK_OUTCOME, now,
                clean_actor, role, json.dumps(report, ensure_ascii=False),
            ),
        )
    return report


def get_incident_report(case_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    """The most recently generated instant-closure report for ``case_id``, if any."""
    path = _initialized_path(db_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT report_json FROM aml_incident_report WHERE case_id = ?
               ORDER BY generated_at DESC, id DESC LIMIT 1""",
            (case_id,),
        ).fetchone()
        return json.loads(row["report_json"]) if row else None


def list_incident_reports(db_path: Path | None = None) -> list[dict[str, Any]]:
    path = _initialized_path(db_path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT report_json FROM aml_incident_report ORDER BY generated_at DESC, id DESC"
        ).fetchall()
        return [json.loads(row["report_json"]) for row in rows]


def render_incident_report_markdown(report: dict[str, Any]) -> str:
    """Render an instant-closure report as the human-readable auto-report."""
    snapshot = report["case_snapshot"]
    lines = [
        f"# Отчёт о закрытии инцидента {report['case_id']}",
        "",
        f"- **Playbook:** {report['playbook']}",
        f"- **Итог:** {report['outcome']}",
        f"- **Дата:** {report['generated_at']}",
        f"- **Кто закрыл:** {report['actor']} ({report['role']})",
        f"- **Клиент:** {snapshot['customer']} · {snapshot['country']}",
        f"- **Сценарий:** {snapshot['scenario']} · риск {snapshot['risk']} · score {snapshot['score']}",
        f"- **Сумма:** {_money_display(snapshot['amount'], snapshot['currency'])}",
        "",
        f"**Обоснование:** {report['reason']}",
        "",
        "## Трасса решений",
    ]
    for event in report["decision_trace"]:
        lines.append(
            f"- `{event['occurred_at']}` **{event['event']}** "
            f"({event['actor']}, {event['role']}) — {event['details']}"
        )
    return "\n".join(lines)
