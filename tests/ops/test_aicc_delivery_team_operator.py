"""Operator admission without a database, network, service or model."""
from datetime import UTC, datetime
import hashlib
import json

import pytest

from ops import aicc_delivery_team_operator as cli

SHA = "a" * 40


class Connection:
    def __init__(self, *, authorized=True, admit=True, changed=False):
        self.authorized, self.admitted = authorized, admit
        self.changed = changed
        self.writes = 0
        self.committed = self.rolled_back = self.closed = False
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        self.committed = kind is None
        self.rolled_back = kind is not None
        self.closed = True

    def cursor(self):
        connection = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                connection.queries.append((sql, params))
                if "session_user = current_user" in sql:
                    self.row = (connection.authorized,)
                elif "delivery_team_release_scope" in sql:
                    sha = "b" * 40 if connection.changed and connection.writes else SHA
                    self.row = ({"repo": "AICC", "source_sha": sha,
                                 "criteria": ["criterion-1"]},)
                elif "delivery_team_record_" in sql:
                    connection.writes += 1
                    self.row = (connection.admitted,)
                else:
                    raise AssertionError(sql)

            def fetchone(self):
                return self.row
        return Cursor()


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("AICC_TEAM_OPERATOR_DSN", "private-dsn")
    monkeypatch.setenv("AICC_TEAM_ARTIFACT_BEARER", "private-artifact-token")
    monkeypatch.setenv("AICC_TEAM_OBSERVATION_BEARER", "private-observer-token")


def check_args(monkeypatch, *, corrective=False):
    value = {"schema": "delivery-acceptance-observation/v1", "task_id": "TASK",
             "revision": 2, "sha": SHA, "environment": "production",
             "criterion_id": "criterion-1", "check_id": "trusted-run-7",
             "outcome": "passed", "observed_at": datetime.now(UTC).isoformat()}
    extra = ["--repo", "AICC"]
    if corrective:
        value.pop("task_id")
        value.pop("environment")
        value.update(schema="delivery-corrective-observation/v1", parent_task_id="TASK",
                     incident_key="c" * 64, corrective_task_id="FIX", corrective_revision=3)
        extra = ["--incident-key", "c" * 64, "--corrective-task-id", "FIX",
                 "--corrective-revision", "3"]
    raw = json.dumps(value).encode()
    def fetch(url, *, bearer):
        assert url == "https://evidence.example/check.json"
        assert bearer == "private-artifact-token"
        return raw
    monkeypatch.setattr(cli.receipts, "fetch_artifact_bytes", fetch)
    return ["corrective" if corrective else "acceptance", "--task-id", "TASK",
            "--revision", "2", "--expected-sha", SHA,
            "--artifact-origin", "https://evidence.example", "--criterion-id", "criterion-1",
            "--check-id", "trusted-run-7", "--artifact-url", "https://evidence.example/check.json",
            "--sha256", hashlib.sha256(raw).hexdigest(), *extra]


def run(args, connection):
    def connect(dsn, **kwargs):
        assert dsn == "private-dsn"
        assert kwargs["autocommit"] is False
        assert kwargs["connect_timeout"] == 10
        return connection
    return cli.main(args, connect=connect)


@pytest.mark.parametrize("corrective", [False, True])
def test_real_check_adapter_commits(credentials, monkeypatch, capsys, corrective):
    args = check_args(monkeypatch, corrective=corrective)
    connection = Connection()
    assert run(args, connection) == 0
    assert connection.committed and connection.closed and connection.writes == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["admitted"] is True
    assert "private" not in output.out + output.err


@pytest.mark.parametrize("problem", ["authority", "refusal", "scope_change", "bad_hash", "foreign_origin"])
def test_refusal_rolls_back(credentials, monkeypatch, capsys, problem):
    args = check_args(monkeypatch)
    connection = Connection(authorized=problem != "authority", admit=problem != "refusal",
                            changed=problem == "scope_change")
    if problem == "bad_hash":
        args[args.index("--sha256") + 1] = "0" * 64
    if problem == "foreign_origin":
        args[args.index("--artifact-origin") + 1] = "https://other.example"
    assert run(args, connection) == 1
    assert connection.rolled_back and not connection.committed and connection.closed
    if problem in {"authority", "bad_hash", "foreign_origin"}:
        assert connection.writes == 0
    output = capsys.readouterr()
    assert not output.out
    assert "private" not in output.err


def test_expected_sha_refused_before_fetch(credentials, monkeypatch):
    args = check_args(monkeypatch)
    args[args.index("--expected-sha") + 1] = "b" * 40
    monkeypatch.setattr(cli.receipts, "fetch_artifact_bytes",
                        lambda *a, **k: pytest.fail("must not fetch"))
    connection = Connection()
    assert run(args, connection) == 1
    assert connection.rolled_back and connection.writes == 0


def test_missing_dsn_never_connects(credentials, monkeypatch):
    args = check_args(monkeypatch)
    monkeypatch.delenv("AICC_TEAM_OPERATOR_DSN")
    assert cli.main(args, connect=lambda *a, **k: pytest.fail("must not connect")) == 1


def test_driver_error_does_not_echo_secret(credentials, monkeypatch, capsys):
    args = check_args(monkeypatch)
    def connect(*args, **kwargs):
        raise RuntimeError("private-dsn private-artifact-token")
    assert cli.main(args, connect=connect) == 1
    output = capsys.readouterr()
    assert "private" not in output.err + output.out


@pytest.mark.parametrize("readback_matches", [True, False])
def test_release_real_adapter_readback_and_commit(
        credentials, monkeypatch, tmp_path, readback_matches):
    args = ["release", "--task-id", "TASK", "--revision", "2", "--expected-sha", SHA,
            "--repo", "AICC", "--artifact-origin", "https://evidence.example",
            "--observation-origin", "https://runtime.example",
            "--version-url", "https://runtime.example/api/version",
            "--health-url", "https://runtime.example/healthz",
            "--artifact-directory", str(tmp_path),
            "--artifact-base-url", "https://evidence.example/release"]
    observations = []
    def observe(url, *, bearer):
        assert bearer == "private-observer-token"
        observations.append(url)
        return {"status": "ok"} if url.endswith("healthz") else {"sha": SHA}
    def read(url, *, bearer):
        assert bearer == "private-artifact-token"
        return (tmp_path / url.rsplit("/", 1)[1]).read_bytes() if readback_matches else b"wrong"
    monkeypatch.setattr(cli.receipts, "fetch_observation", observe)
    monkeypatch.setattr(cli.receipts, "fetch_artifact_bytes", read)
    connection = Connection()
    assert run(args, connection) == (0 if readback_matches else 1)
    assert connection.committed is readback_matches
    assert connection.rolled_back is not readback_matches
    assert connection.writes == int(readback_matches)
    assert len(observations) == 3 and observations[0] == observations[2]
    artifact, = tmp_path.iterdir()
    assert json.loads(artifact.read_text())["business_acceptance"] is False
