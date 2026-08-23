"""Where a table lives is decided by git, never by the working tree.

`0002_queue_claim.up.sql` opens with a "verified placement, not assumed" block
that justifies the queue protocol depending on nothing else. It backs that with
`grep -rn <token>` over the checkout, and every claim in it had rotted by the
time anyone re-ran it:

* A recursive filesystem search reads whatever is on disk, including files no
  ref contains. `__pycache__`, `.ruff_cache`, `.mypy_cache` and `.pytest_cache`
  went on naming a since-deleted `<token>_store.py` for as long as they were
  not swept, so the search reported a repository-lease store in AICC — the
  OPPOSITE of what the repository says. That inversion is the whole finding.
* It inverts the other way too: the token appears in the comment making the
  claim, so `grep -rn` matched the sentence asserting it matched nothing.
* And "`principal` exists in NO database today" was true when it was written
  and false from 0003 onwards. A claim pinned to a date measures nothing.

So the probes here read tracked content at a ref — `git grep <ref>` and
`git show <ref>:<path>` — which a cache, an untracked scratch file and a stale
editor buffer are all invisible to. The last test is the negative control: it
builds the exact stale-cache situation in a throwaway repository and shows the
filesystem answering yes where the ref probe answers no, so the reason these
tests are written the awkward way cannot be forgotten and simplified away.

**The header itself could not be fixed, and that is why this file exists.** The
ledger's checksum is the SHA-256 of the migration's bytes, so its comments are
as immutable as its statements: editing the block to say something true would
make `verify_checksums` refuse every database that already applied 0002 —
measured, not assumed, against the recorded checksum for version 2. The prose
correction therefore lives in `docs/postgres-foundation.md`
("Correcting a migration's prose") and the executable half lives here.

The lease token is split-joined throughout, for the same reason
`test_integration_privacy_fitness.py` split-joins its needles: a gate that
searches `*.py` for a token must not be the file that contains it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The ref every probe reads. `HEAD` rather than `origin/main`: the question is
#: what *this commit* declares, and CI checks out the merge result shallowly —
#: `git grep`/`git show` need only the one commit, so a depth-1 clone is fine.
REF = "HEAD"

CLAIM_MIGRATION = "command_center/db/sql/0002_queue_claim.up.sql"

#: `repo` + `_lease` — the AIOS platform table AICC must not grow a copy of.
LEASE_TOKEN = "repo" + "_lease"

#: The three authorities the queue-claim protocol says it does not touch.
FOREIGN_AUTHORITIES = ("principal", "identity_assert", LEASE_TOKEN)


def _git(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _git_available() -> bool:
    """True when this checkout can answer questions about `REF`."""
    if not (REPO_ROOT / ".git").exists():
        return False
    return _git("rev-parse", "--verify", f"{REF}^{{commit}}").returncode == 0


requires_git = pytest.mark.skipif(
    not _git_available(),
    reason=f"{REF} is not resolvable in this checkout (no .git, or an archive export)",
)


def _git_grep(pattern: str, *pathspecs: str, extended: bool = False) -> list[str]:
    """Matching lines for `pattern` in tracked content at `REF`.

    `git grep` exits 1 for "no match" and 0 for "matched"; anything else is a
    real failure and must not read as a clean sweep — the shape that let the
    sweep in `mirror_slice_checks.py` call a red suite covered.
    """
    flags = ["-nE"] if extended else ["-n"]
    result = _git("grep", *flags, pattern, REF, "--", *pathspecs)
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"git grep {pattern!r} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.splitlines()


def _blob_at_ref(path: str) -> str:
    result = _git("show", f"{REF}:{path}")
    assert result.returncode == 0, f"{path} is not present at {REF}: {result.stderr.strip()}"
    return result.stdout


def _executable_sql(text: str) -> str:
    """`text` with `--` line comments removed.

    Blunt on purpose. A `--` inside a string literal would truncate that line
    early, which can only make this function return *less* SQL — so the token
    checks below can under-report but never invent a hit, and a false green
    there would still be caught by the migration failing to run. There is no
    such literal in the claim migration today; the positive control below
    fails if the stripper ever starts eating real statements.
    """
    return "\n".join(re.sub(r"--.*$", "", line) for line in text.splitlines())


# --------------------------------------------------------------- the probes


@requires_git
def test_no_aicc_python_file_names_the_platform_lease_table():
    """The claim the stale caches inverted, asked of git instead.

    The caches named `<token>_store.py` after it was deleted; tracked content
    at the ref has never named it at all.
    """
    hits = _git_grep(LEASE_TOKEN, "*.py")
    assert not hits, (
        f"AICC Python code names `{LEASE_TOKEN}` at {REF}, but the queue-claim "
        "migration's placement block says the lease lives in the AIOS platform "
        f"database. Either the code is wrong or {CLAIM_MIGRATION} is:\n"
        + "\n".join(hits)
    )


@requires_git
def test_no_aicc_migration_declares_the_platform_lease_table():
    """AICC declaring its own lease table is the failure the block rules out.

    Whitespace-tolerant so `CREATE TABLE  <token>` and a newline-wrapped
    declaration are not a way through; searched over all tracked content
    rather than just the SQL directory, so a declaration smuggled into a
    Python string is caught too.
    """
    hits = _git_grep(rf"CREATE +TABLE +(IF +NOT +EXISTS +)?{LEASE_TOKEN}", extended=True)
    assert not hits, (
        f"a tracked file declares a `{LEASE_TOKEN}` table at {REF}; AICC is not "
        "the authority for repository writer leases (see the placement block in "
        f"{CLAIM_MIGRATION}):\n" + "\n".join(hits)
    )


@requires_git
def test_the_claim_protocol_reads_none_of_the_three_foreign_authorities():
    """0002's central claim — no dependency on SRV-02, SRV-03 or SRV-04a.

    Read out of git rather than off disk for the reason the whole file exists,
    and with comments stripped because the block *discusses* all three tokens
    at length. Replaces the old sentence "measured by a suite that runs
    against 0001 and 0002 and nothing else", which stopped being true when
    that suite's `migrations.upgrade(conn)` started applying 0003 as well.
    """
    sql = _executable_sql(_blob_at_ref(CLAIM_MIGRATION))
    named = sorted(token for token in FOREIGN_AUTHORITIES if token in sql)
    assert not named, (
        f"{CLAIM_MIGRATION}'s executable SQL names {named} at {REF}. The file's "
        "own header says the protocol calls `identity_assert()` on nothing and "
        f"reads neither `principal` nor `{LEASE_TOKEN}`; one of the two has to give."
    )


@requires_git
def test_the_comment_stripper_keeps_the_statements_it_must_search():
    """Positive control for `_executable_sql`.

    The claim migration is mostly comment. A stripper that ate the statements
    too would return an empty string, and the test above would pass by
    searching nothing — the "assertion that touched nothing" failure the
    queue-claim suite calls out by name.
    """
    sql = _executable_sql(_blob_at_ref(CLAIM_MIGRATION))
    assert "CREATE TABLE work_item" in sql
    assert "CREATE TABLE work_attempt" in sql
    assert sql.count("CREATE ") >= 5


# ------------------------------------------------- why it has to be this way


@requires_git
def test_a_stale_cache_inverts_a_filesystem_search_but_not_a_ref_probe(tmp_path):
    """The finding, reproduced: same question, opposite answers.

    A module is committed, then deleted, and the tool caches that named it are
    left behind exactly as an un-swept checkout leaves them. Searching the
    filesystem then reports the module as present. Asking git at the ref
    reports it as gone, which is the truth.

    Without this control the four probes above look like a stylistic
    preference for `git grep`, and the next person to simplify them back to
    `rglob`/`grep -rn` re-opens the defect.
    """
    module = f"{LEASE_TOKEN}_store.py"
    repo = tmp_path / "checkout"
    (repo / "command_center" / "db").mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)

    tracked = repo / "command_center" / "db" / module
    tracked.write_text("STORE = 'lease rows live here'\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git(
        "-c", "user.email=gate@example.invalid", "-c", "user.name=gate",
        "commit", "-q", "-m", "add the store", cwd=repo,
    )
    tracked.unlink()
    _git("add", "-A", cwd=repo)
    _git(
        "-c", "user.email=gate@example.invalid", "-c", "user.name=gate",
        "commit", "-q", "-m", "delete the store", cwd=repo,
    )

    # What the tools leave behind: a bytecode file named after the module, and
    # a cache whose payload embeds the source path. Neither is tracked; both
    # survive the deletion until something sweeps them.
    pycache = repo / "command_center" / "db" / "__pycache__"
    pycache.mkdir()
    (pycache / f"{LEASE_TOKEN}_store.cpython-313.pyc").write_bytes(b"\x00" * 16)
    ruff_cache = repo / ".ruff_cache" / "0.15.22"
    ruff_cache.mkdir(parents=True)
    (ruff_cache / "entries").write_text(
        f"command_center/db/{module}\n", encoding="utf-8"
    )

    on_disk = [
        path
        for path in repo.rglob("*")
        if ".git/" not in path.as_posix() and path.is_file()
        and (LEASE_TOKEN in path.name or LEASE_TOKEN in path.read_bytes().decode("utf-8", "replace"))
    ]
    assert on_disk, "the premise failed: no stale artifact names the deleted module"

    at_ref = _git("grep", "-n", LEASE_TOKEN, "HEAD", cwd=repo)
    assert at_ref.returncode == 1, at_ref.stdout or at_ref.stderr
    assert at_ref.stdout == ""

    shown = _git("show", f"HEAD:command_center/db/{module}", cwd=repo)
    assert shown.returncode != 0, "git resolved a path the commit does not contain"
