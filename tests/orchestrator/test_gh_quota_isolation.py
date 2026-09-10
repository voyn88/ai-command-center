"""The control plane's own GitHub identity, quota and cache.

VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS: on 2026-09-09 between
21:15 and 22:10 UTC every control tick died with `GraphQL: API rate limit
already exceeded for user ID 297853521` -- one human's OAuth token was the
ambient credential on control-01, its GraphQL quota is shared with that
human's own laptop tools, and nothing in the control plane had a quota of its
own. The PR-window tick could not label and the review/merge ticks skipped
every task for as long as the human's hour-long window stayed exhausted.

These tests run a REAL `gh` on PATH -- a stand-in binary that refuses every
call not carrying the fleet App's config dir with exactly that error -- so the
acceptance claim ("ticks keep working while the human token is exhausted") is
exercised end to end: git remote -> REST path -> `gh` invocation -> label
write -> quota telemetry, with no in-process patching of the transport.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from command_center.orchestrator import gh_access
from command_center.orchestrator.review_merge import (
    PrWindowConfig,
    reconcile_pr_window,
)

OWNER_REPO = "voyn88/ai-command-center"
HEAD = "a" * 40

#: A `gh` that behaves like the live one did during the incident: anything
#: not authenticated through the fleet App's config directory is refused with
#: the exhausted-quota error, and any GraphQL porcelain (`gh pr ...`) is
#: refused outright so a regression back onto it cannot pass silently.
FAKE_GH = '''#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
log = os.environ["FAKE_GH_LOG"]
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "config": os.environ.get("GH_CONFIG_DIR", "")}) + "\\n")

if "fleet" not in os.environ.get("GH_CONFIG_DIR", ""):
    sys.stderr.write("GraphQL: API rate limit already exceeded for user ID 297853521\\n")
    sys.exit(1)
if argv[0] != "api":
    sys.stderr.write("this stand-in serves REST only; %s is GraphQL porcelain\\n" % argv[0])
    sys.exit(1)

method = argv[2] if "--method" in argv else "GET"
path = argv[3] if "--method" in argv else argv[1]
head = "HEADSHA"

if path == "rate_limit":
    print(json.dumps({"resources": {
        "core": {"limit": 5000, "remaining": 4987, "reset": 1789000000},
        "graphql": {"limit": 5000, "remaining": 5000, "reset": 1789000000},
    }}))
elif "/labels" in path:
    print("[]")
elif "/pulls?" in path:
    page = int(path.split("page=")[-1])
    body = [] if page > 1 else [{
        "number": 42,
        "html_url": "https://github.com/OWNER_REPO/pull/42",
        "head": {"sha": head},
        "created_at": "2026-09-09T12:00:00Z",
        "user": {"login": "voyn-aicc-fleet[bot]"},
        "labels": [],
    }]
    print(json.dumps(body))
elif "/reviews" in path:
    page = int(path.split("page=")[-1])
    print(json.dumps([] if page > 1 else [{
        "state": "COMMENTED",
        "submitted_at": "2026-09-09T13:00:00Z",
        "body": "ACCEPTANCE: ACCEPT " + head,
        "user": {"login": "voyn88-acceptance-gate[bot]"},
    }]))
elif path.endswith("/pulls/42"):
    print(json.dumps({"mergeable_state": "clean"}))
elif "/check-runs" in path:
    page = int(path.split("page=")[-1])
    print(json.dumps({"check_runs": [] if page > 1 else [{
        "name": "Final merge gate",
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-09-09T12:30:00Z",
        "completed_at": "2026-09-09T12:40:00Z",
        "details_url": "https://github.com/OWNER_REPO/actions/runs/7",
    }]}))
elif path.split("?")[0].endswith("/status"):
    print(json.dumps({"state": "success", "statuses": []}))
elif "/commits/" in path:
    print("2026-09-09T12:29:00Z")
else:
    sys.stderr.write("unhandled path: %s %s\\n" % (method, path))
    sys.exit(1)
'''


@pytest.fixture(autouse=True)
def _fresh_identity():
    """The resolved identity is memoised for a minute inside the module; each
    test decides the host's configuration for itself."""
    gh_access._identity_cache = None
    yield
    gh_access._identity_cache = None


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """A `gh` on PATH, and the log of what it was called with."""
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir(parents=True)
    binary.write_text(FAKE_GH.replace("OWNER_REPO", OWNER_REPO).replace("HEADSHA", HEAD))
    binary.chmod(0o755)
    log = tmp_path / "gh-calls.jsonl"
    monkeypatch.setenv("PATH", f"{binary.parent}:{Path('/usr/bin')}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("AICC_GH_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return log


def _calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def checkout(tmp_path):
    """A clone whose `origin` is the repository the REST paths are built
    from -- read from git, never from GitHub."""
    repo = tmp_path / "clone"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", f"git@github.com:{OWNER_REPO}.git"],
        cwd=repo,
        check=True,
    )
    return repo


@pytest.fixture
def fleet_store(tmp_path, monkeypatch):
    """The store `voyn-aicc-github-token.timer` writes, as the control ticks
    find it: a `gh` config directory plus the token's expiry."""
    root = tmp_path / "fleet"
    (root / "gh").mkdir(parents=True)
    (root / "gh" / "hosts.yml").write_text(
        "github.com:\n    oauth_token: ghs_fleet\n    user: voyn-aicc-fleet[bot]\n"
    )
    (root / "expires_at").write_text("2099-01-01T00:00:00Z\n")
    monkeypatch.setenv(gh_access.FLEET_CONFIG_DIR_ENV, str(root / "gh"))
    return root


# -- identity ---------------------------------------------------------------


def test_the_fleet_app_store_is_preferred_over_the_ambient_credential(fleet_store):
    identity = gh_access.resolve_identity()
    assert identity.name == gh_access.FLEET
    assert identity.env["GH_CONFIG_DIR"] == str(fleet_store / "gh")


def test_an_inherited_gh_token_is_cleared_so_the_fleet_config_actually_wins(fleet_store):
    """`GH_TOKEN`/`GITHUB_TOKEN` take precedence over `GH_CONFIG_DIR` inside
    `gh`: leaving one in place would keep spending the human's quota under a
    report claiming the fleet identity."""
    identity = gh_access.resolve_identity()
    assert identity.env["GH_TOKEN"] is None
    assert identity.env["GITHUB_TOKEN"] is None


def test_an_expired_fleet_token_falls_back_instead_of_401ing_every_call(fleet_store):
    (fleet_store / "expires_at").write_text("2020-01-01T00:00:00Z\n")
    identity = gh_access.resolve_identity()
    assert identity.name == gh_access.AMBIENT
    assert identity.reason == "fleet_token_expired"


def test_an_unparseable_expiry_is_not_treated_as_expired(fleet_store):
    """The token itself is what GitHub validates; a stamp format this code
    does not recognise must not disable the fleet identity."""
    (fleet_store / "expires_at").write_text("later today\n")
    assert gh_access.resolve_identity().name == gh_access.FLEET


def test_a_host_without_the_store_keeps_working_on_the_ambient_credential(monkeypatch, tmp_path):
    monkeypatch.setenv(gh_access.FLEET_CONFIG_DIR_ENV, str(tmp_path / "absent"))
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    identity = gh_access.resolve_identity()
    assert identity.name == gh_access.AMBIENT
    assert identity.reason == "fleet_store_unreadable"


def test_an_operator_can_force_the_ambient_credential(fleet_store, monkeypatch):
    monkeypatch.setenv(gh_access.IDENTITY_ENV, "ambient")
    assert gh_access.resolve_identity().name == gh_access.AMBIENT


def test_a_unit_supplied_gh_config_dir_is_respected_not_overridden(fleet_store, monkeypatch):
    monkeypatch.setenv("GH_CONFIG_DIR", "/etc/somewhere/gh")
    identity = gh_access.resolve_identity()
    assert identity.name == gh_access.INHERITED
    assert identity.env == {}


# -- the acceptance claim ---------------------------------------------------


def test_the_window_tick_works_while_the_human_graphql_quota_is_exhausted(
    fake_gh, checkout, fleet_store
):
    """The incident's exact condition: the ambient credential is refused with
    `API rate limit already exceeded`. Under the fleet App's token the tick
    lists, examines and labels the pull request anyway."""
    report = reconcile_pr_window(str(checkout), PrWindowConfig(stale_seconds=10**12))

    assert report.error is None
    assert report.active == [(42, HEAD)]
    assert report.quota is not None
    assert report.quota.identity == gh_access.FLEET
    assert report.quota.rate_limited == 0
    assert report.quota.graphql_calls == 0, "the whole tick runs on REST"
    assert all(
        call["config"].endswith("fleet/gh") for call in _calls(fake_gh)
    ), "every call carried the fleet App's credential, not the human's"
    assert any(
        call["argv"][:3] == ["api", "--method", "POST"] for call in _calls(fake_gh)
    ), "the label write is REST too, so a spent GraphQL budget cannot block it"


def test_without_the_fleet_store_the_same_tick_reports_the_rate_limit(
    fake_gh, checkout, monkeypatch, tmp_path
):
    """The before-picture, kept as a test: on the ambient credential the tick
    fails -- and says so, with the rate limit counted in its own telemetry
    rather than buried in `gh` stderr."""
    monkeypatch.setenv(gh_access.FLEET_CONFIG_DIR_ENV, str(tmp_path / "absent"))

    report = reconcile_pr_window(str(checkout), PrWindowConfig())

    assert report.error is not None and report.error.startswith("pr_list_failed")
    assert report.quota is not None
    assert report.quota.identity == gh_access.AMBIENT
    assert report.quota.rate_limited >= 1
    assert "rate_limited=1" in report.quota.line()


def test_the_tick_report_carries_the_identity_and_its_remaining_budget(
    fake_gh, checkout, fleet_store
):
    """Quota telemetry in the tick report: `gh api rate_limit` is exempt from
    rate limiting, so reading the budget never costs what it measures."""
    report = reconcile_pr_window(str(checkout), PrWindowConfig(stale_seconds=10**12))

    quota = report.quota
    assert quota is not None
    assert (quota.core_remaining, quota.core_limit) == (4987, 5000)
    assert (quota.graphql_remaining, quota.graphql_limit) == (5000, 5000)
    line = quota.line()
    assert "identity=fleet-app" in line and "core=4987/5000" in line
    assert any(call["argv"] == ["api", "rate_limit"] for call in _calls(fake_gh))


def test_an_unchanged_head_is_served_from_cache_on_the_next_tick(
    fake_gh, checkout, fleet_store
):
    """Cache per (repo, PR, head): the second tick re-reads the listing (it
    must see new PRs) but spends nothing on details for a PR whose head has
    not moved."""
    config = PrWindowConfig(stale_seconds=10**12)
    reconcile_pr_window(str(checkout), config)
    first = len([c for c in _calls(fake_gh) if "/reviews" in c["argv"][-1]])

    second = reconcile_pr_window(str(checkout), config)

    detail_calls = [c for c in _calls(fake_gh) if "/reviews" in c["argv"][-1]]
    assert first == 1 and len(detail_calls) == 1, "the second tick fetched no details"
    assert second.active == [(42, HEAD)], "and still labelled from the cached details"
    assert second.quota is not None and second.quota.cache_hits == 1


def test_a_moved_head_is_never_served_from_a_stale_cache_entry(tmp_path):
    """The head sha is part of the KEY, so a push makes the old entry
    unreachable rather than stale -- the one property that makes caching a
    review/check verdict safe at all."""
    cache = gh_access.PrDetailCache(tmp_path, ttl=3600)
    cache.put(OWNER_REPO, 42, "old" + "0" * 37, {"reviews": ["stale"]})

    assert cache.get(OWNER_REPO, 42, "old" + "0" * 37) == {"reviews": ["stale"]}
    assert cache.get(OWNER_REPO, 42, "new" + "0" * 37) is None
    assert cache.get("other/repo", 42, "old" + "0" * 37) is None
    assert cache.get(OWNER_REPO, 43, "old" + "0" * 37) is None


def test_a_cache_entry_expires_and_is_pruned(tmp_path):
    clock = {"now": 1_000.0}
    cache = gh_access.PrDetailCache(tmp_path, ttl=60, now=lambda: clock["now"])
    cache.put(OWNER_REPO, 42, HEAD, {"reviews": []})

    clock["now"] += 61
    assert cache.get(OWNER_REPO, 42, HEAD) is None, "past the TTL, not served"
    # Pruning is one `stat` per file, so it is the entry's age on disk that
    # decides -- not the injected clock the TTL check above uses.
    import os

    entry = next(iter(tmp_path.iterdir()))
    os.utime(entry, (0, 0))
    assert gh_access.PrDetailCache(tmp_path, ttl=60).prune() == 1
    assert list(tmp_path.iterdir()) == []


def test_an_unwritable_cache_directory_degrades_to_no_cache_not_an_error():
    cache = gh_access.PrDetailCache(None, ttl=1800)
    cache.put(OWNER_REPO, 42, HEAD, {"reviews": []})
    assert cache.enabled is False
    assert cache.get(OWNER_REPO, 42, HEAD) is None
    assert cache.prune() == 0


# -- fallback and accounting -------------------------------------------------


def test_a_permission_the_app_lacks_falls_back_to_the_ambient_credential(
    tmp_path, monkeypatch, fleet_store
):
    """The fleet App is granted contents and pull-requests everywhere, and
    the extra read scopes only where the owner approved them. A refusal for
    THIS resource retries once on the ambient credential -- a best-effort
    path (a workflow rerun) keeps working -- and is counted."""
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if 'fleet' in os.environ.get('GH_CONFIG_DIR', ''):\n"
        "    sys.stderr.write('HTTP 403: Resource not accessible by integration\\n')\n"
        "    sys.exit(1)\n"
        "print('{\"attempt\": 1}')\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{Path('/usr/bin')}")

    with gh_access.tick() as quota:
        proc = gh_access.run(["run", "view", "7", "--json", "attempt"], str(tmp_path))

    assert proc.returncode == 0, "the ambient credential completed the call"
    assert quota.ambient_fallbacks == 1
    assert quota.degraded is False, "a resource refusal is not a credential failure"


def test_a_credential_failure_demotes_the_whole_tick_once_not_per_call(
    tmp_path, monkeypatch, fleet_store
):
    """Otherwise every remaining lookup pays a doubled call to rediscover the
    same dead token."""
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if 'fleet' in os.environ.get('GH_CONFIG_DIR', ''):\n"
        "    sys.stderr.write('HTTP 401: Bad credentials\\n')\n"
        "    sys.exit(1)\n"
        "print('[]')\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{Path('/usr/bin')}")

    with gh_access.tick() as quota:
        gh_access.run(["api", "repos/x/y/pulls"], str(tmp_path))
        gh_access.run(["api", "repos/x/y/pulls"], str(tmp_path))

    assert quota.degraded is True
    assert quota.ambient_fallbacks == 1, "the second call went straight to ambient"
    assert quota.rest_calls == 2


def test_rest_and_graphql_calls_are_counted_apart(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/usr/bin/env python3\nprint('[]')\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{Path('/usr/bin')}")
    monkeypatch.setenv(gh_access.IDENTITY_ENV, "ambient")

    with gh_access.tick() as quota:
        gh_access.run(["api", "repos/x/y/pulls"], str(tmp_path))
        gh_access.run(["pr", "view", "1", "--json", "state"], str(tmp_path))

    assert (quota.rest_calls, quota.graphql_calls) == (1, 1)
    assert "calls=2 (rest=1 graphql=1)" in quota.line()


def test_nested_tick_scopes_report_one_total(tmp_path, monkeypatch):
    """The review tick wraps four functions, two of which open their own
    scope; an operator must see one number, not four."""
    monkeypatch.setenv(gh_access.IDENTITY_ENV, "ambient")
    with gh_access.tick() as outer, gh_access.tick() as inner:
        assert inner is outer


def test_a_rate_limited_fleet_call_is_not_retried_on_the_human_credential(
    tmp_path, monkeypatch, fleet_store
):
    """Falling back exactly when a budget is exhausted would spend the human's
    quota at the worst possible moment -- the thing this whole change exists
    to stop. The call is counted as rate-limited and left refused."""
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('HTTP 403: API rate limit exceeded\\n')\n"
        "sys.exit(1)\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{Path('/usr/bin')}")

    with gh_access.tick() as quota:
        gh_access.run(["api", "repos/x/y/pulls"], str(tmp_path))

    assert (quota.rate_limited, quota.ambient_fallbacks) == (1, 0)
    assert quota.rest_calls == 1, "one call attempted, not two"
