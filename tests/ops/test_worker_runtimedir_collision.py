from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"


def _module():
    path = REPO_ROOT / "ops" / "aicc_runtime_directory_check.py"
    spec = importlib.util.spec_from_file_location("aicc_runtime_directory_check", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rtd():
    return _module()


def _write_unit(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _patterns(rtd, tmp_path, name, service_body):
    unit = _write_unit(tmp_path, name, f"[Unit]\nDescription=x\n\n[Service]\n{service_body}\n")
    return rtd.runtime_directory_patterns(unit)


# --- Real fleet regression: the actual bug this task is about -------------


def test_no_two_units_declare_colliding_runtime_directories(rtd):
    unit_paths = sorted(SYSTEMD_DIR.glob("*.service"))
    collisions = rtd.find_collisions(unit_paths)
    assert collisions == [], (
        "colliding RuntimeDirectory= across units -- a sibling stop deletes "
        f"the directory out from under the survivor: {collisions}"
    )


def test_worker_template_runtime_directory_is_keyed_by_instance(rtd):
    unit = SYSTEMD_DIR / "voyn-aicc-worker@.service"
    (pattern,) = rtd.runtime_directory_patterns(unit)
    assert pattern.raw == "voyn-aicc-worker/%i"


# --- Directive-value enumeration (PR #487 rejection) -----------------------
# "only captures the first RuntimeDirectory= directive and treats its entire
# whitespace-separated value as one path" -- every value, from every
# directive occurrence, must be enumerated as its own independent path.


def test_multiple_values_on_one_directive_are_all_enumerated(rtd, tmp_path):
    patterns = _patterns(rtd, tmp_path, "a.service", "RuntimeDirectory=foo bar baz")
    assert [p.raw for p in patterns] == ["foo", "bar", "baz"]


def test_repeated_directive_accumulates_values(rtd, tmp_path):
    patterns = _patterns(
        rtd, tmp_path, "a.service", "RuntimeDirectory=foo\nRuntimeDirectory=bar"
    )
    assert [p.raw for p in patterns] == ["foo", "bar"]


def test_empty_assignment_resets_accumulated_values(rtd, tmp_path):
    patterns = _patterns(
        rtd,
        tmp_path,
        "a.service",
        "RuntimeDirectory=foo\nRuntimeDirectory=\nRuntimeDirectory=bar",
    )
    assert [p.raw for p in patterns] == ["bar"]


def test_second_value_of_a_mixed_directive_is_still_checked_for_collision(rtd, tmp_path):
    # The exact PR #487 counter-example: a line that both contains "%i"
    # AND a second, unrelated, unqualified path. A parser that treats the
    # whole line as one blob (or greps for "%i" anywhere in it) would wave
    # this whole unit through; the second path is a real bare collision.
    unit_a = _write_unit(
        tmp_path,
        "a@.service",
        "[Service]\nRuntimeDirectory=voyn-aicc-worker/%i shared\n",
    )
    unit_b = _write_unit(tmp_path, "b.service", "[Service]\nRuntimeDirectory=shared\n")
    collisions = rtd.find_collisions([unit_a, unit_b])
    assert len(collisions) == 1
    assert collisions[0].pattern_a == "shared" or collisions[0].pattern_b == "shared"


# --- systemd quoting / escaping / continuation (PR #497 medium) ------------
# "value.split() ignores quoting, escaping, and continued lines"


def test_quoted_value_preserves_embedded_whitespace_as_one_path(rtd, tmp_path):
    patterns = _patterns(rtd, tmp_path, "a.service", 'RuntimeDirectory="foo bar" baz')
    assert [p.raw for p in patterns] == ["foo bar", "baz"]


def test_backslash_escaped_space_is_not_a_separator(rtd, tmp_path):
    patterns = _patterns(rtd, tmp_path, "a.service", r"RuntimeDirectory=foo\ bar")
    assert [p.raw for p in patterns] == ["foo bar"]


def test_line_continuation_is_joined_before_splitting(rtd, tmp_path):
    patterns = _patterns(
        rtd, tmp_path, "a.service", "RuntimeDirectory=foo \\\n          bar\n"
    )
    assert [p.raw for p in patterns] == ["foo", "bar"]


def test_directive_outside_service_section_is_ignored(rtd, tmp_path):
    unit = _write_unit(
        tmp_path,
        "a.service",
        "[Unit]\nRuntimeDirectory=not-a-real-place\n\n[Service]\nExecStart=/bin/true\n",
    )
    assert rtd.runtime_directory_patterns(unit) == []


# --- Specifier semantics across templates (PR #497 high) -------------------
# "skips every path containing %i, %n, or %N ... Two different templates can
# therefore declare the same path ... The test must compare possible
# effective paths across templates, accounting for specifier semantics
# rather than excluding them wholesale."


def test_two_different_templates_sharing_a_specifier_path_collide(rtd, tmp_path):
    unit_a = _write_unit(tmp_path, "alpha@.service", "[Service]\nRuntimeDirectory=shared/%i\n")
    unit_b = _write_unit(tmp_path, "beta@.service", "[Service]\nRuntimeDirectory=shared/%i\n")
    collisions = rtd.find_collisions([unit_a, unit_b])
    assert len(collisions) == 1


def test_templates_with_distinguishing_literal_text_do_not_collide(rtd, tmp_path):
    # Each lane's directory is keyed by BOTH its owning template and its
    # instance -- the actual fix's shape (per-service literal prefix plus
    # %i) -- so no shared instance name can make these equal.
    unit_a = _write_unit(
        tmp_path, "alpha@.service", "[Service]\nRuntimeDirectory=shared/alpha-%i\n"
    )
    unit_b = _write_unit(
        tmp_path, "beta@.service", "[Service]\nRuntimeDirectory=shared/beta-%i\n"
    )
    assert rtd.find_collisions([unit_a, unit_b]) == []


def test_specifier_path_collides_with_a_plain_unit_that_happens_to_match(rtd, tmp_path):
    # A bare literal is not automatically "safe" just because the other
    # side has a specifier: if some valid instance name equals the literal
    # outright, that IS a real collision (e.g. a lane named "shared").
    unit_a = _write_unit(tmp_path, "alpha@.service", "[Service]\nRuntimeDirectory=%i\n")
    unit_b = _write_unit(tmp_path, "b.service", "[Service]\nRuntimeDirectory=shared\n")
    assert len(rtd.find_collisions([unit_a, unit_b])) == 1


def test_specifier_path_does_not_collide_when_length_cannot_match(rtd, tmp_path):
    unit_a = _write_unit(tmp_path, "alpha@.service", "[Service]\nRuntimeDirectory=abc-%i\n")
    unit_b = _write_unit(tmp_path, "b.service", "[Service]\nRuntimeDirectory=ab\n")
    assert rtd.find_collisions([unit_a, unit_b]) == []


def test_two_instances_of_the_same_template_are_not_compared_to_each_other(rtd, tmp_path):
    # This is exactly the correct, already-fixed shape: one template file,
    # instantiated twice. It must never be flagged against itself -- the
    # per-instance %i is precisely what keeps @3 and @4 disjoint.
    unit = _write_unit(tmp_path, "worker@.service", "[Service]\nRuntimeDirectory=worker/%i\n")
    other = _write_unit(tmp_path, "unrelated.service", "[Service]\nRuntimeDirectory=other\n")
    assert rtd.find_collisions([unit, other]) == []


def test_percent_i_outside_a_template_is_a_syntax_error(rtd, tmp_path):
    unit = _write_unit(tmp_path, "plain.service", "[Service]\nRuntimeDirectory=%i\n")
    with pytest.raises(rtd.UnitSyntaxError):
        rtd.runtime_directory_patterns(unit)


def test_percent_n_and_percent_p_resolve_without_a_shared_instance_var(rtd, tmp_path):
    unit = _write_unit(tmp_path, "aicc-worker.service", "[Service]\nRuntimeDirectory=%p-state\n")
    (pattern,) = rtd.runtime_directory_patterns(unit)
    assert pattern.components == (rtd._LiteralComponent("aicc-worker-state"),)


def test_multiple_specifiers_in_one_segment_are_a_conservative_collision(rtd, tmp_path):
    # "%i-%i" is not expressible as our pre/post model (two independent
    # occurrences of the instance variable in one segment); rather than
    # silently under-approximating, any comparison touching it must count
    # as a possible collision.
    unit_a = _write_unit(tmp_path, "alpha@.service", "[Service]\nRuntimeDirectory=%i-%i\n")
    unit_b = _write_unit(tmp_path, "b.service", "[Service]\nRuntimeDirectory=totally-unrelated\n")
    assert len(rtd.find_collisions([unit_a, unit_b])) == 1
