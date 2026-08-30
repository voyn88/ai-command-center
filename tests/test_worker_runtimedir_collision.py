"""Structural regression guard for the shared-RuntimeDirectory credential
collision (VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION).

Root cause of the 2026-08-27..29 queue collapse: worker-01 ran three units
(voyn-aicc-worker.service, voyn-aicc-worker@3, voyn-aicc-worker@4) that all
declared `RuntimeDirectory=voyn-aicc-worker` — one shared path — and each
installed its lease/app credential to /run/voyn-aicc-worker/pgpass. systemd
does not refcount RuntimeDirectory across units: whenever ANY of them
stopped, the directory was deleted out from under the still-running
siblings, which then failed authentication and dead-lettered their in-flight
task. The fix committed in deploy/systemd/voyn-aicc-worker@.service scopes
the directory (and PGPASSFILE) per instance via `%i`.

A first attempt at this guard (PR #487) was rejected by adversarial review at
commit dfc117a5c70c2633a183e575e253ec5b7cd2dd05: its parser captured only the
first `RuntimeDirectory=` directive per unit and treated the entire
whitespace-separated value as one path. systemd's directive is list-valued —
a single directive may name more than one directory, and the directive may
be repeated (values accumulate; an empty assignment resets the list). Under
the old parser, a unit declaring
`RuntimeDirectory=voyn-aicc-worker/%i shared` alongside a sibling declaring
`RuntimeDirectory=shared` passed both checks: the `%i` satisfied the
per-instance test while the colliding `shared` directory was never compared
against the sibling's. This module enumerates every directory named by every
RuntimeDirectory= directive before checking collisions or per-instance
scoping, so that evasion is closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SYSTEMD_DIR = ROOT / "deploy" / "systemd"

_INSTANCE_SPECIFIERS = ("%i", "%n", "%N")


def _runtime_directories(unit_text: str) -> list[str]:
    """Return every directory named by RuntimeDirectory= directives in `unit_text`.

    Mirrors systemd's own semantics for this directive: it is list-valued, a
    single directive may name more than one directory separated by
    whitespace, the directive may be repeated with values accumulating
    across occurrences, and assigning an empty value resets the list
    collected so far.
    """
    directories: list[str] = []
    for raw_line in unit_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        key, sep, value = line.partition("=")
        if sep != "=" or key.strip() != "RuntimeDirectory":
            continue
        value = value.strip()
        if not value:
            directories = []
            continue
        directories.extend(value.split())
    return directories


def _unit_files() -> list[Path]:
    return sorted(SYSTEMD_DIR.glob("*.service"))


def _is_templated(unit_path: Path) -> bool:
    return "@" in unit_path.name


def _is_instance_scoped(directory: str) -> bool:
    return any(specifier in directory for specifier in _INSTANCE_SPECIFIERS)


def test_parser_collects_every_directive_and_every_value_in_a_directive():
    """Guard the parser itself against the exact evasion the review flagged."""
    text = "\n".join(
        [
            "[Service]",
            "RuntimeDirectory=voyn-aicc-worker/%i shared",
            "RuntimeDirectory=extra",
        ]
    )
    assert _runtime_directories(text) == ["voyn-aicc-worker/%i", "shared", "extra"]


def test_parser_resets_the_list_on_an_empty_assignment():
    text = "\n".join(
        [
            "[Service]",
            "RuntimeDirectory=voyn-aicc-worker/%i",
            "RuntimeDirectory=",
            "RuntimeDirectory=voyn-aicc-worker-2",
        ]
    )
    assert _runtime_directories(text) == ["voyn-aicc-worker-2"]


def test_parser_ignores_commented_directives():
    text = "\n".join(
        [
            "[Service]",
            "# RuntimeDirectory=voyn-aicc-worker",
            "RuntimeDirectory=voyn-aicc-worker/%i",
        ]
    )
    assert _runtime_directories(text) == ["voyn-aicc-worker/%i"]


@pytest.mark.parametrize("unit_path", _unit_files(), ids=lambda p: p.name)
def test_templated_units_scope_every_runtime_directory_to_the_instance(unit_path):
    """Every directory a template unit declares must vary with the instance.

    A literal directory sitting alongside a %i-scoped one would still
    collide across instances of the SAME template (worker@3 vs worker@4) —
    exactly the historical bug, just with one fewer culprit needed.
    """
    if not _is_templated(unit_path):
        pytest.skip("not a template unit")
    directories = _runtime_directories(unit_path.read_text(encoding="utf-8"))
    for directory in directories:
        assert _is_instance_scoped(directory), (
            f"{unit_path.name} declares RuntimeDirectory={directory!r} "
            "without an instance specifier (%i/%n/%N): every instance of "
            "this template would share that path, reintroducing the "
            "sibling-restart credential collision "
            "(VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION)."
        )


def test_no_two_units_declare_colliding_runtime_directories():
    """No two units may claim the same effective /run path.

    Directories that vary by instance (%i/%n/%N) are excluded from the
    cross-unit comparison since systemd expands them per instance; every
    other directory is a literal path shared by every process that names
    it, so two units naming the same one collide the moment either one
    stops or restarts and systemd tears the directory down.
    """
    owners: dict[str, str] = {}
    for unit_path in _unit_files():
        text = unit_path.read_text(encoding="utf-8")
        for directory in _runtime_directories(text):
            if _is_instance_scoped(directory):
                continue
            previous = owners.get(directory)
            assert previous is None, (
                f"{unit_path.name} and {previous} both declare "
                f"RuntimeDirectory={directory!r}: a restart of either "
                "deletes the credential directory out from under the "
                "other (VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION)."
            )
            owners[directory] = unit_path.name


def test_worker_template_pgpassfile_lives_inside_its_own_runtime_directory():
    """PGPASSFILE must point inside the SAME per-instance directory the unit creates.

    Otherwise the %i fix is cosmetic: the daemon would create an
    instance-scoped directory but still read its credential from a path
    outside it.
    """
    unit_path = SYSTEMD_DIR / "voyn-aicc-worker@.service"
    text = unit_path.read_text(encoding="utf-8")

    directories = _runtime_directories(text)
    assert directories, "voyn-aicc-worker@.service must declare RuntimeDirectory"
    assert all(_is_instance_scoped(d) for d in directories)

    pgpassfile = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("Environment=PGPASSFILE="):
            pgpassfile = line.split("=", 2)[2]
    assert pgpassfile is not None, "voyn-aicc-worker@.service must set PGPASSFILE"
    assert any(
        pgpassfile == f"/run/{directory}/pgpass" for directory in directories
    ), (
        f"PGPASSFILE={pgpassfile!r} does not resolve inside any declared "
        f"RuntimeDirectory {directories!r}"
    )
