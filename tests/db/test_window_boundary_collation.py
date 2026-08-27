"""The reproduction and the gate for `VOYN-W0-AICC-SRV-07e`.

SQLite compares `TEXT` byte-for-byte — `BINARY`, its only collation for text —
regardless of the machine it runs on. PostgreSQL compares `text` in whatever
collation the *database* was created with, and that is a per-deployment choice:
`initdb --locale-provider=icu` gives every text column ICU ordering. A
reconciliation (or a windowed backfill's own seek boundaries — see
`PostgresTableMirror.list_records_window`) that reads the mirror's key order
without saying which collation it means inherits that choice silently.

Measured, not assumed: five rows ordered under `COLLATE "und-x-icu"` disagreed
with SQLite's own order on a byte-identical mirror; the same five under
`COLLATE "C"` did not. This server's own default collation happened to already
be `C`, which is exactly why the fix cannot be "trust the default" — the
failure is invisible on a server where nobody chose ICU, and the deployments
that matter are the ones where someone did.

Skipped wholesale unless `AICC_TEST_PG_ADMIN_DSN` is set — see `conftest`, same
as the rest of `tests/db`.
"""

from __future__ import annotations

import pytest

from command_center.db.owner_item_store import PostgresOwnerItemMirror
from command_center.db.provenance_store import PostgresProviderAttemptMirror
from tests.db.test_mirror_contract import _ensure_parents, sample_row

#: Chosen for one guaranteed property: `Banana` sorts before `apple` in byte
#: order (`B` is 0x42, `a` is 0x61) and after it under essentially every
#: locale-aware collation, including ICU's root locale, which treats case as a
#: tertiary difference and sorts alphabetically first. The exact ICU output is
#: not asserted — only that it disagrees with byte order, which is the
#: property every non-`C` collation shares and the one this fix depends on.
ADVERSARIAL_IDS = ["Banana", "apple", "_underscore", "Cherry", "1number", "date"]


def _owner_item(pg_connection_factory) -> PostgresOwnerItemMirror:
    return PostgresOwnerItemMirror(connection_factory=pg_connection_factory)


def test_list_records_orders_text_keys_by_byte_value_not_database_collation(
    pg_connection_factory, admin_conn
) -> None:
    mirror = _owner_item(pg_connection_factory)
    for row_id in ADVERSARIAL_IDS:
        mirror.upsert(sample_row(mirror.spec, row_id))

    byte_order = sorted(ADVERSARIAL_IDS)
    assert [row["id"] for row in mirror.list_records()] == byte_order

    # The reproduction: the same rows, ordered by the collation the bug report
    # names, on this same server and this same data. If this assertion ever
    # started failing, `COLLATE "C"` would have stopped being a meaningful fix
    # here — the test above would be passing by coincidence, not by contract.
    with admin_conn.cursor() as cur:
        try:
            cur.execute('SELECT id FROM owner_item ORDER BY id COLLATE "und-x-icu"')
        except Exception as exc:  # noqa: BLE001 - environmental, not a defect
            pytest.skip(f'COLLATE "und-x-icu" unavailable on this server: {exc}')
        icu_order = [row[0] for row in cur.fetchall()]
    assert icu_order != byte_order, (
        "the adversarial id set no longer demonstrates the divergence this test "
        "exists to guard against — pick ids where ICU and byte order disagree"
    )


def test_list_records_window_pages_through_the_same_order_with_no_gap_or_overlap(
    pg_connection_factory,
) -> None:
    mirror = _owner_item(pg_connection_factory)
    for row_id in ADVERSARIAL_IDS:
        mirror.upsert(sample_row(mirror.spec, row_id))

    seen: list[str] = []
    after = None
    for _ in range(len(ADVERSARIAL_IDS) + 1):  # one extra call must come back empty
        page = mirror.list_records_window(after=after, limit=2)
        if not page:
            break
        seen.extend(row["id"] for row in page)
        after = (page[-1]["id"],)
    else:
        raise AssertionError("windowing did not terminate within the expected page count")

    assert seen == sorted(ADVERSARIAL_IDS)
    assert len(seen) == len(set(seen))  # no row crossed a window boundary twice


def test_composite_key_window_orders_the_numeric_column_numerically(
    pg_connection_factory,
) -> None:
    """`provider_attempt`'s key mixes a `text` column (`run_id`) with a
    numeric one (`attempt_number`). Byte order and numeric order agree on
    single-digit values and disagree the moment a second digit appears
    (`"10" < "2"` lexicographically) — attempt eleven is the case that would
    catch `attempt_number` wrongly picking up `COLLATE "C"`."""
    mirror = PostgresProviderAttemptMirror(connection_factory=pg_connection_factory)
    _ensure_parents(mirror.spec, pg_connection_factory)

    attempt_numbers = [1, 2, 9, 10, 11]
    for number in attempt_numbers:
        row = sample_row(mirror.spec)
        row["attempt_number"] = number
        mirror.upsert(row)

    assert [row["attempt_number"] for row in mirror.list_records()] == sorted(attempt_numbers)

    seen: list[int] = []
    after = None
    for _ in range(len(attempt_numbers) + 1):
        page = mirror.list_records_window(after=after, limit=2)
        if not page:
            break
        seen.extend(row["attempt_number"] for row in page)
        after = (page[-1]["run_id"], page[-1]["attempt_number"])
    else:
        raise AssertionError("windowing did not terminate within the expected page count")

    assert seen == sorted(attempt_numbers)
