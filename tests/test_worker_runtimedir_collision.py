"""Guard against RuntimeDirectory= collisions across systemd units.

systemd does not refcount RuntimeDirectory= across units sharing a path:
whenever any one of them stops, the directory is deleted out from under
every other unit still relying on it. That is exactly what happened to
worker-01 (VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION): three units all
declared `RuntimeDirectory=voyn-aicc-worker`, and a sibling restart wiped
the credential file the surviving lanes were authenticating with.

Three prior attempts at this guard were each rejected by adversarial review
for the same shape of problem: the collision check was unsound rather than
merely incomplete, so it could be made to pass with the credential-deleting
collision reintroduced. This file both exercises
ops/aicc_runtime_directory_lint.py against every one of those rejections
directly (as regression tests) and scans this repository's actual unit
files with it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "ops" / "aicc_runtime_directory_lint.py"
    spec = importlib.util.spec_from_file_location("aicc_runtime_directory_lint", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lint():
    return _module()


def _unit(lint, name, text, *dropins):
    return lint.UnitSource(name=name, fragments=(text, *dropins))


# --------------------------------------------------------------------------
# Directive parsing: enumerate every directive, every word, quoting/escaping,
# continuation lines, drop-ins, and reset-on-empty (round 1 + round 2 medium).
# --------------------------------------------------------------------------


def test_effective_directory_words_accumulates_every_directive_and_every_word(lint):
    unit = _unit(
        lint,
        "multi.service",
        "[Service]\n"
        "RuntimeDirectory=first second\n"
        "RuntimeDirectory=third\n",
    )
    assert lint.effective_directory_words(unit) == ["first", "second", "third"]


def test_effective_directory_words_honors_reset_on_empty_assignment(lint):
    unit = _unit(
        lint,
        "reset.service",
        "[Service]\n"
        "RuntimeDirectory=stale-a stale-b\n"
        "RuntimeDirectory=\n"
        "RuntimeDirectory=kept\n",
    )
    assert lint.effective_directory_words(unit) == ["kept"]


def test_effective_directory_words_honors_quoting_and_escaping(lint):
    # A naive value.split() shatters a quoted, space-containing directory
    # name into two bogus words (round 2, medium finding).
    unit = _unit(
        lint,
        "quoted.service",
        '[Service]\nRuntimeDirectory="has space" plain\n',
    )
    assert lint.effective_directory_words(unit) == ["has space", "plain"]


def test_effective_directory_words_joins_backslash_continued_lines(lint):
    unit = _unit(
        lint,
        "continued.service",
        "[Service]\nRuntimeDirectory=voyn-aicc-worker/\\\n%i\n",
    )
    assert lint.effective_directory_words(unit) == ["voyn-aicc-worker/%i"]


def test_effective_directory_words_reads_drop_ins_in_order(lint):
    unit = _unit(
        lint,
        "base.service",
        "[Service]\nRuntimeDirectory=from-base\n",
        "[Service]\nRuntimeDirectory=from-dropin\n",
    )
    assert lint.effective_directory_words(unit) == ["from-base", "from-dropin"]


def test_effective_directory_words_ignores_comments_and_unrelated_keys(lint):
    unit = _unit(
        lint,
        "noise.service",
        "[Service]\n"
        "# RuntimeDirectory=commented-out\n"
        "RuntimeDirectoryMode=0750\n"
        "RuntimeDirectory=real\n",
    )
    assert lint.effective_directory_words(unit) == ["real"]


# --------------------------------------------------------------------------
# patterns_could_collide: the three adversarial-review regressions.
# --------------------------------------------------------------------------


def test_flags_two_templates_sharing_one_specifier_pattern_round2(lint):
    # PR #497 round 2, high finding: alpha@.service and beta@.service both
    # declare `RuntimeDirectory=shared/%i`. Starting alpha@3 and beta@3
    # collides at /run/shared/3. The old test explicitly skipped every path
    # containing %i and so never caught this.
    alpha = lint.tokenize("shared/%i")
    beta = lint.tokenize("shared/%i")
    assert lint.patterns_could_collide(alpha, beta) is True


def test_flags_differing_instance_strings_round3(lint):
    # PR #525 round 3, critical finding: alpha@.service declares
    # `shared/a-%i`, beta@.service declares `shared/%i`. alpha@x and
    # beta@a-x both resolve to shared/a-x -- the two templates need
    # DIFFERENT instance strings to collide, which a checker that assumes a
    # shared instance variable across templates will miss.
    alpha = lint.tokenize("shared/a-%i")
    beta = lint.tokenize("shared/%i")
    assert lint.patterns_could_collide(alpha, beta) is True
    # Symmetric: argument order must not matter.
    assert lint.patterns_could_collide(beta, alpha) is True


def test_enumerates_every_runtime_directory_directive_round1(lint):
    # PR #487 round 1: a broken config declares
    # `RuntimeDirectory=voyn-aicc-worker/%i shared` on one unit and
    # `RuntimeDirectory=shared` on another. The %i path is a red herring;
    # the real collision is the second, un-templated word "shared" that a
    # first-directive-only parser never even reaches.
    victim = _unit(
        lint,
        "victim@.service",
        "[Service]\nRuntimeDirectory=voyn-aicc-worker/%i shared\n",
    )
    other = _unit(lint, "other.service", "[Service]\nRuntimeDirectory=shared\n")
    hazards = lint.find_hazards([victim, other])
    assert any("victim@.service" in h and "other.service" in h for h in hazards)


# --------------------------------------------------------------------------
# patterns_could_collide: general correctness (prefix/suffix compatibility,
# fixed-literal-vs-glob, and safe/incompatible patterns).
# --------------------------------------------------------------------------


def test_rejects_patterns_with_incompatible_prefixes(lint):
    lane = lint.tokenize("lane-%i")
    worker = lint.tokenize("worker-%i")
    assert lint.patterns_could_collide(lane, worker) is False


def test_rejects_patterns_with_incompatible_suffixes(lint):
    primary = lint.tokenize("%i-primary")
    secondary = lint.tokenize("%i-secondary")
    assert lint.patterns_could_collide(primary, secondary) is False


def test_fixed_literal_can_still_match_a_glob_pattern(lint):
    fixed = lint.tokenize("voyn-aicc-worker-3")
    templated = lint.tokenize("voyn-aicc-worker-%i")
    assert lint.patterns_could_collide(fixed, templated) is True


def test_fixed_literal_too_short_for_both_glob_anchors_is_safe(lint):
    # "a*a" (both a leading and trailing "a" required) cannot match the
    # single-character fixed string "a": that would need length >= 2.
    fixed = lint.tokenize("a")
    templated = lint.tokenize("a%ia")
    assert lint.patterns_could_collide(fixed, templated) is False


def test_two_disjoint_fixed_literals_do_not_collide(lint):
    a = lint.tokenize("voyn-aicc-worker")
    b = lint.tokenize("voyn-aicc-credential-rotation")
    assert lint.patterns_could_collide(a, b) is False


def test_two_identical_fixed_literals_collide(lint):
    a = lint.tokenize("voyn-aicc-worker")
    b = lint.tokenize("voyn-aicc-worker")
    assert lint.patterns_could_collide(a, b) is True


def test_repeated_specifier_within_one_pattern_fails_closed(lint):
    # `%i` appearing twice in one pattern must resolve to the SAME string
    # both times -- a backreference constraint this module does not solve
    # exactly (round 3's second finding: treating repeated occurrences as
    # independently free is exactly the bug that produced false positives
    # AND false negatives before). It must not silently claim safety.
    repeated = lint.tokenize("%i-alpha/%i-beta")
    other = lint.tokenize("fixed-alpha/fixed-beta")
    assert lint.patterns_could_collide(repeated, other) is True


# --------------------------------------------------------------------------
# find_hazards: same-template self-collision (a template word with no
# specifier is shared verbatim by every instance).
# --------------------------------------------------------------------------


def test_flags_template_word_with_no_instance_specifier(lint):
    unit = _unit(
        lint, "voyn-aicc-worker@.service", "[Service]\nRuntimeDirectory=voyn-aicc-worker\n"
    )
    hazards = lint.find_hazards([unit])
    assert any("no instance specifier" in h for h in hazards)


def test_does_not_flag_template_word_with_instance_specifier(lint):
    unit = _unit(
        lint, "voyn-aicc-worker@.service", "[Service]\nRuntimeDirectory=voyn-aicc-worker/%i\n"
    )
    assert lint.find_hazards([unit]) == []


def test_does_not_flag_plain_unit_with_a_fixed_directory(lint):
    # A non-templated unit only ever has one running instance -- a fixed
    # RuntimeDirectory is the normal, safe case (e.g. the real
    # voyn-aicc-credential-rotation.service).
    unit = _unit(
        lint,
        "aicc-credential-rotation.service",
        "[Service]\nRuntimeDirectory=aicc-credential-rotation\n",
    )
    assert lint.find_hazards([unit]) == []


def test_repeated_specifier_in_a_template_is_still_safe_across_instances(lint):
    # Two DIFFERENT instances of the SAME template can never collide as
    # long as the word has at least one specifier, no matter how many times
    # it repeats: the render is injective in the instance string once the
    # surrounding literal structure is fixed and shared by both instances.
    unit = _unit(
        lint, "doubled@.service", "[Service]\nRuntimeDirectory=%i-lane-%i\n"
    )
    assert lint.find_hazards([unit]) == []


# --------------------------------------------------------------------------
# find_hazards: reconstruct the actual worker-01 incident.
# --------------------------------------------------------------------------


def test_reconstructs_the_original_three_unit_incident(lint):
    plain = _unit(
        lint, "voyn-aicc-worker.service", "[Service]\nRuntimeDirectory=voyn-aicc-worker\n"
    )
    stale_template = _unit(
        lint, "voyn-aicc-worker@.service", "[Service]\nRuntimeDirectory=voyn-aicc-worker\n"
    )
    hazards = lint.find_hazards([plain, stale_template])
    assert any(
        "voyn-aicc-worker.service" in h and "voyn-aicc-worker@.service" in h for h in hazards
    )
    assert any("no instance specifier" in h for h in hazards)


def test_fixed_template_reproduces_no_hazard(lint):
    plain = _unit(
        lint, "voyn-aicc-worker.service", "[Service]\nRuntimeDirectory=voyn-aicc-worker\n"
    )
    fixed_template = _unit(
        lint, "voyn-aicc-worker@.service", "[Service]\nRuntimeDirectory=voyn-aicc-worker/%i\n"
    )
    assert lint.find_hazards([plain, fixed_template]) == []


# --------------------------------------------------------------------------
# The actual regression guard: this repository's real unit files.
# --------------------------------------------------------------------------


def test_repo_deploy_systemd_units_declare_no_colliding_runtime_directories(lint):
    root = Path(__file__).parents[1] / "deploy" / "systemd"
    units = lint.discover_units(root)
    assert units, "expected to find unit files under deploy/systemd"
    assert lint.find_hazards(units) == []


def test_repo_worker_template_still_uses_a_per_instance_directory(lint):
    # Guards the premise of the test above against a trivial pass: if the
    # template stopped declaring RuntimeDirectory= entirely, or dropped the
    # %i and thereby made itself the very hazard this file exists to catch,
    # the scan above would (correctly) start failing -- but only if this
    # unit is actually found and actually still carries a specifier.
    root = Path(__file__).parents[1] / "deploy" / "systemd"
    units = {u.name: u for u in lint.discover_units(root)}
    template = units["voyn-aicc-worker@.service"]
    words = lint.effective_directory_words(template)
    assert words
    assert any(
        any(isinstance(tok, lint.Specifier) for tok in lint.tokenize(word)) for word in words
    )
