"""Which deployed functions can write an audit row — and can any of them raise?

The defect this answers (VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS) is measurable in
two probes that differ only in how a refusal is reported: audit rows after a
refusal that ``RAISE``s — 0; after a refusal that ``RETURN``s — 1. PostgreSQL
has no way to keep a row written by a transaction that then aborts, so a
function that can both write an audit row and raise can erase its own record of
a denial. Refusing a one-time ticket *is* the theft signal; a signal that rolls
itself back is not a signal.

This module computes the rule's subject rather than listing it, because a list
is exactly what goes stale when the fourth audit surface is added:

1. **Audit tables are discovered.** A table whose name ends in a literal
   ``_event`` is an audit trail (``backlog_event``, ``work_event``,
   ``principal_event`` today). The check is Python's ``str.endswith`` on the
   parsed name — deliberately not an SQL ``LIKE '%_event'``, where ``_`` is a
   single-character wildcard and ``solvent``/``xevent`` would match.
2. **Writers are discovered.** Any deployed function whose body inserts into
   one of those tables.
3. **The closure is computed.** Any function that calls a member of the closure
   is in the closure. Two hops, ten hops — the rule does not care.
4. **Offenders are the closure members that can raise**, with the non-aborting
   ``RAISE`` levels (``LOG``, ``NOTICE``, …) excluded: ``_principal_audit``
   deliberately ``RAISE LOG``s every denial, which is a diagnostic that does
   not abort anything.

Two modelling decisions worth stating, because both are deliberately
conservative — they can make the gate stricter, never blinder:

* **Definitions are keyed by (name, argument types), not by name.** PostgreSQL
  overloads by argument type, so a ``{name: body}`` mapping silently
  overwrites one overload's body with another's — and an overload that raises
  can then hide behind an overload that audits, or vice versa. The later
  *migration* wins for the same signature (that is what ``CREATE OR REPLACE``
  does to a running database); a different signature is a different function.
* **Call edges are matched by name**, so a call reaches every overload of that
  name. That over-approximates the call graph: it can only add members to the
  closure, never drop one.

Only ``*.up.sql`` is read. The up-migrations, applied in order, are what a
deployed database contains; ``*.down.sql`` describes the schema this repository
is moving away from and deliberately still holds the raising bodies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = REPO_ROOT / "command_center" / "db" / "sql"

#: An audit trail is a table whose name ends in this, literally.
AUDIT_TABLE_SUFFIX = "_event"

#: `RAISE <level>` forms that do not abort the transaction. Everything else —
#: `RAISE EXCEPTION`, a bare `RAISE;` re-raise, `RAISE <condition_name>` —
#: does, and is what this module is looking for.
NON_ABORTING_RAISE_LEVELS = frozenset({"debug", "log", "info", "notice", "warning"})

#: Argument-mode keywords that precede the name/type in a parameter.
_ARG_MODES = frozenset({"in", "out", "inout", "variadic"})

#: Type words that can legitimately open a multi-word type name. Used only to
#: tell `p_name text` (named) from `timestamp with time zone` (unnamed).
_TYPE_LEAD_WORDS = frozenset(
    {
        "bigint", "bigserial", "bit", "boolean", "bool", "bytea", "char",
        "character", "date", "double", "float", "inet", "int", "int2", "int4",
        "int8", "integer", "interval", "json", "jsonb", "money", "numeric",
        "real", "serial", "smallint", "text", "time", "timestamp",
        "timestamptz", "timetz", "uuid", "varchar", "void", "record",
    }
)

#: Type names PostgreSQL stores under a different spelling than the migrations
#: write. Normalised so a parsed signature can be compared with the catalogue's
#: (`tests/db/test_refusal_audit_survives.py` does exactly that).
_TYPE_ALIASES = {
    "bool": "boolean",
    "char": "character",
    "decimal": "numeric",
    "float4": "real",
    "float8": "double precision",
    "int": "integer",
    "int2": "smallint",
    "int4": "integer",
    "int8": "bigint",
    "serial4": "integer",
    "serial8": "bigint",
    "timestamptz": "timestamp with time zone",
    "timetz": "time with time zone",
    "varchar": "character varying",
}

_CREATE_FUNCTION = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+"
    r"(?:public\s*\.\s*)?\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*\(",
    re.IGNORECASE,
)
_DROP_FUNCTION = re.compile(
    r"\bDROP\s+FUNCTION\s+(?:IF\s+EXISTS\s+)?"
    r"(?:public\s*\.\s*)?\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*\(",
    re.IGNORECASE,
)
_CREATE_TABLE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:public\s*\.\s*)?\"?([A-Za-z_][A-Za-z0-9_]*)\"?",
    re.IGNORECASE,
)
_DROP_TABLE = re.compile(
    r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
    r"(?:public\s*\.\s*)?\"?([A-Za-z_][A-Za-z0-9_]*)\"?",
    re.IGNORECASE,
)
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.up\.sql$")
_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(")
_RAISE = re.compile(r"\bRAISE\b[ \t]*([A-Za-z_][A-Za-z0-9_]*)?", re.IGNORECASE)


@dataclass(frozen=True)
class FunctionDefinition:
    """One ``CREATE FUNCTION`` as a deployed database would hold it."""

    name: str
    arg_types: tuple[str, ...]
    body: str
    version: int
    source: str

    @property
    def signature(self) -> tuple[str, tuple[str, ...]]:
        return (self.name, self.arg_types)

    def __str__(self) -> str:  # pragma: no cover - failure messages only
        return f"{self.name}({', '.join(self.arg_types)})"


def strip_sql_noise(text: str) -> str:
    """Blank out comments and string-literal contents, character for character.

    Matching ``RAISE EXCEPTION`` or ``INSERT INTO backlog_event`` against raw
    SQL would find them in prose and in quoted payloads; this repository's
    migrations are mostly narrative, so that is not a hypothetical. The result
    has the SAME LENGTH as its input so that offsets into it still address the
    original file — `build_model` needs statement order, and a `DROP FUNCTION`
    that a migration issues BEFORE re-creating the same signature is not the
    same thing as one issued after.
    """
    out = list(text)
    i, n = 0, len(text)

    def blank(start: int, stop: int) -> None:
        for index in range(start, min(stop, n)):
            if out[index] != "\n":
                out[index] = " "

    while i < n:
        ch = text[i]
        if ch == "-" and text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
            continue
        if ch == "/" and text.startswith("/*", i):
            start, depth, i = i, 1, i + 2
            while i < n and depth:
                if text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            blank(start, i)
            continue
        if ch == "'":
            start = i
            i += 1
            while i < n:
                if text[i] == "'":
                    if text.startswith("''", i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            blank(start + 1, i - 1)
            continue
        i += 1
    return "".join(out)


def _split_top_level(arg_text: str) -> list[str]:
    parts, depth, current = [], 0, []
    for ch in arg_text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [part.strip() for part in parts if part.strip()]


def _argument_type(argument: str) -> str | None:
    """The declared type of one parameter, or None if it is not part of the
    function's identity — PostgreSQL identifies a function by its IN/INOUT/
    VARIADIC argument types, and `OUT` parameters belong to the result."""
    text = re.split(r"\bDEFAULT\b|=", argument, maxsplit=1, flags=re.IGNORECASE)[0]
    tokens = text.split()
    if tokens and tokens[0].lower() in _ARG_MODES:
        if tokens[0].lower() == "out":
            return None
        tokens = tokens[1:]
    if len(tokens) > 1 and tokens[0].lower() not in _TYPE_LEAD_WORDS:
        tokens = tokens[1:]  # `p_task_id text` — drop the parameter name
    declared = re.sub(r"\s+", " ", " ".join(tokens)).lower()
    return _TYPE_ALIASES.get(declared, declared)


def _argument_types(arg_text: str) -> tuple[str, ...]:
    """The identity argument types of a parameter list, comments removed.

    The comments matter: this schema documents parameters inline, and
    `p_secret_hash text,      -- sha256 of the host's OWN new secret` parses as
    a three-word type unless the comment is blanked first.
    """
    parts = _split_top_level(strip_sql_noise(arg_text))
    types = (_argument_type(part) for part in parts)
    return tuple(t for t in types if t)


def _matching_paren(text: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unbalanced parentheses in a CREATE FUNCTION argument list")


def _body_after(text: str, start: int) -> tuple[str, int]:
    """The dollar-quoted body that follows a function header, and where it ends."""
    tag_match = _DOLLAR_TAG.search(text, start)
    if tag_match is None:
        return "", len(text)
    tag = tag_match.group(0)
    body_start = tag_match.end()
    body_end = text.find(tag, body_start)
    if body_end == -1:
        return text[body_start:], len(text)
    return text[body_start:body_end], body_end + len(tag)


def parse_migration(sql: str, version: int, source: str) -> list[tuple[str, object]]:
    """The migration's schema operations, IN THE ORDER IT ISSUES THEM.

    Order is not cosmetic: 0010 drops `backlog_transition(text, text, bigint)`
    and immediately re-creates it, which is a replacement — applying every drop
    after every create would leave the function gone, and the gate blind to
    whatever the new body does.
    """
    functions: list[tuple[int, FunctionDefinition]] = []
    spans: list[tuple[int, int]] = []
    for match in _CREATE_FUNCTION.finditer(sql):
        open_paren = match.end() - 1
        close_paren = _matching_paren(sql, open_paren)
        body, end = _body_after(sql, close_paren)
        functions.append(
            (
                match.start(),
                FunctionDefinition(
                    name=match.group(1).lower(),
                    arg_types=_argument_types(sql[open_paren + 1 : close_paren]),
                    body=body,
                    version=version,
                    source=source,
                ),
            )
        )
        spans.append((match.start(), end))

    # Statement-level scanning happens with the CREATE FUNCTION statements
    # blanked out (a body may contain `INSERT INTO`/`DROP TABLE` text of its
    # own) but the file's offsets preserved, so everything can be merged back
    # into one ordered sequence.
    blanked = list(sql)
    for start, end in spans:
        for index in range(start, end):
            if blanked[index] != "\n":
                blanked[index] = " "
    statements = strip_sql_noise("".join(blanked))

    operations: list[tuple[int, str, object]] = [
        (position, "create_function", function) for position, function in functions
    ]
    for pattern, kind in (
        (_DROP_FUNCTION, "drop_function"),
        (_CREATE_TABLE, "create_table"),
        (_DROP_TABLE, "drop_table"),
    ):
        operations.extend(
            (match.start(), kind, match.group(1).lower())
            for match in pattern.finditer(statements)
        )
    return [(kind, payload) for _position, kind, payload in sorted(operations, key=lambda op: op[0])]


def migration_files(sql_dir: Path | str = SQL_DIR) -> list[tuple[int, Path]]:
    """Every up-migration, oldest first."""
    found = []
    for path in sorted(Path(sql_dir).iterdir()):
        match = _MIGRATION_NAME.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    return sorted(found)


@dataclass(frozen=True)
class SchemaModel:
    """The schema as the up-migrations, applied in order, leave it."""

    functions: dict[tuple[str, tuple[str, ...]], FunctionDefinition]
    tables: frozenset[str]

    @property
    def audit_tables(self) -> frozenset[str]:
        return frozenset(t for t in self.tables if t.endswith(AUDIT_TABLE_SUFFIX))


def build_model(sql_dir: Path | str = SQL_DIR) -> SchemaModel:
    """Apply every up-migration in order and report what is left standing."""
    functions: dict[tuple[str, tuple[str, ...]], FunctionDefinition] = {}
    tables: set[str] = set()
    for version, path in migration_files(sql_dir):
        for kind, payload in parse_migration(
            path.read_text(encoding="utf-8"), version, path.name
        ):
            if kind == "create_function":
                functions[payload.signature] = payload
            elif kind == "drop_function":
                # `DROP FUNCTION name(args)` without the arg list parsed: every
                # overload of that name goes. Conservative in the safe
                # direction -- a function this gate stops tracking is one it can
                # no longer accuse, and an over-broad drop is visible as a
                # mismatch against the deployed catalogue
                # (tests/db/test_refusal_audit_survives.py).
                for signature in [s for s in functions if s[0] == payload]:
                    del functions[signature]
            elif kind == "create_table":
                tables.add(payload)
            elif kind == "drop_table":
                tables.discard(payload)
    return SchemaModel(functions=functions, tables=frozenset(tables))


def writes_audit_table(body: str, audit_tables: frozenset[str]) -> str | None:
    """The audit table this body inserts into, if any."""
    clean = strip_sql_noise(body)
    for table in sorted(audit_tables):
        pattern = (
            r"\bINSERT\s+INTO\s+(?:public\s*\.\s*)?\"?"
            + re.escape(table)
            + r"\"?\b"
        )
        if re.search(pattern, clean, re.IGNORECASE):
            return table
    return None


def called_names(body: str) -> set[str]:
    return {match.group(1).lower() for match in _CALL.finditer(strip_sql_noise(body))}


def raise_levels(body: str) -> list[str]:
    """Every ``RAISE`` in the body, as the level word that follows it ('' if bare)."""
    return [(match.group(1) or "").lower() for match in _RAISE.finditer(strip_sql_noise(body))]


def can_raise(body: str) -> bool:
    return any(level not in NON_ABORTING_RAISE_LEVELS for level in raise_levels(body))


def audit_writers(model: SchemaModel) -> set[tuple[str, tuple[str, ...]]]:
    """The seed: functions that insert into an audit table themselves."""
    audit = model.audit_tables
    return {
        signature
        for signature, function in model.functions.items()
        if writes_audit_table(function.body, audit) is not None
    }


def audit_closure(model: SchemaModel) -> set[tuple[str, tuple[str, ...]]]:
    """Every function that can reach an audit write, itself included."""
    closure = audit_writers(model)
    calls = {
        signature: called_names(function.body)
        for signature, function in model.functions.items()
    }
    changed = True
    while changed:
        changed = False
        reachable_names = {signature[0] for signature in closure}
        for signature in model.functions:
            if signature in closure:
                continue
            if calls[signature] & reachable_names:
                closure.add(signature)
                changed = True
    return closure


def offenders(sql_dir: Path | str = SQL_DIR) -> list[FunctionDefinition]:
    """Deployed functions that can write an audit row AND can raise."""
    model = build_model(sql_dir)
    return sorted(
        (
            model.functions[signature]
            for signature in audit_closure(model)
            if can_raise(model.functions[signature].body)
        ),
        key=lambda function: (function.name, function.arg_types),
    )
