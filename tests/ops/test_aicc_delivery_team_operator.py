"""Operator admission without a database, network, service or model."""
from datetime import UTC, datetime
import hashlib
import json
import os

import pytest

from ops import aicc_delivery_team_operator as cli

SHA = "a" * 40
POSIX_PRIVATE_FILES = (os.name == "posix" and hasattr(os, "O_NOFOLLOW")
                       and callable(getattr(os, "geteuid", None)))
requires_private_files = pytest.mark.skipif(
    not POSIX_PRIVATE_FILES,
    reason="Private credential file security requires POSIX O_NOFOLLOW and geteuid",
)


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
@pytest.mark.parametrize("auth_mode", [
    "bearer", pytest.param("basic", marks=requires_private_files),
])
def test_release_real_adapter_readback_and_commit(
        credentials, monkeypatch, tmp_path, readback_matches, auth_mode):
    args = ["release", "--task-id", "TASK", "--revision", "2", "--expected-sha", SHA,
            "--repo", "AICC", "--artifact-origin", "https://evidence.example",
            "--observation-origin", "https://runtime.example",
            "--version-url", "https://runtime.example/api/version",
            "--health-url", "https://runtime.example/healthz",
            "--artifact-directory", str(tmp_path),
            "--artifact-base-url", "https://evidence.example/release"]
    if auth_mode == "basic":
        auth_path = tmp_path / "auth.json"
        auth_path.write_text(json.dumps({"username": "operator", "password": "private-pass"}))
        auth_path.chmod(0o600)
        monkeypatch.setenv("AICC_TEAM_AUTH_MODE", "basic")
        monkeypatch.setenv("AICC_TEAM_BASIC_CREDENTIALS_FILE", str(auth_path))
        monkeypatch.delenv("AICC_TEAM_ARTIFACT_BEARER")
        monkeypatch.delenv("AICC_TEAM_OBSERVATION_BEARER")
        args = [v.replace("evidence.example", "runtime.example") for v in args]
    observations = []
    def observe(url, *, bearer=None, basic=None):
        if auth_mode == "basic":
            assert basic == ("operator", "private-pass") and bearer is None
        else:
            assert bearer == "private-observer-token" and basic is None
        observations.append(url)
        return {"status": "ok"} if url.endswith("healthz") else {"sha": SHA}
    def read(url, *, bearer=None, basic=None):
        if auth_mode == "basic":
            assert basic == ("operator", "private-pass") and bearer is None
        else:
            assert bearer == "private-artifact-token" and basic is None
        return (tmp_path / url.rsplit("/", 1)[1]).read_bytes() if readback_matches else b"wrong"
    monkeypatch.setattr(cli.receipts, "fetch_observation", observe)
    monkeypatch.setattr(cli.receipts, "fetch_artifact_bytes", read)
    connection = Connection()
    assert run(args, connection) == (0 if readback_matches else 1)
    assert connection.committed is readback_matches
    assert connection.rolled_back is not readback_matches
    assert connection.writes == int(readback_matches)
    assert len(observations) == 3 and observations[0] == observations[2]
    artifact, = (p for p in tmp_path.iterdir() if p.name != "auth.json")
    assert json.loads(artifact.read_text())["business_acceptance"] is False


@pytest.fixture
def basic_credentials(credentials, monkeypatch, tmp_path):
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps({'username': 'operator', 'password': 'private-pass'}))
    path.chmod(0o600)
    monkeypatch.delenv('AICC_TEAM_ARTIFACT_BEARER')
    monkeypatch.delenv('AICC_TEAM_OBSERVATION_BEARER')
    monkeypatch.setenv('AICC_TEAM_AUTH_MODE', 'basic')
    monkeypatch.setenv('AICC_TEAM_BASIC_CREDENTIALS_FILE', str(path))
    return path


@requires_private_files
@pytest.mark.parametrize('accepted', [True, False])
def test_basic_real_acceptance_adapter_transaction(basic_credentials, monkeypatch, capsys, accepted):
    args = check_args(monkeypatch)
    prior_fetch = cli.receipts.fetch_artifact_bytes
    def fetch(url, *, basic):
        assert basic == ('operator', 'private-pass')
        return prior_fetch(url, bearer='private-artifact-token')
    monkeypatch.setattr(cli.receipts, 'fetch_artifact_bytes', fetch)
    connection = Connection(admit=accepted)
    assert run(args, connection) == (0 if accepted else 1)
    assert connection.committed is accepted
    assert connection.rolled_back is not accepted
    output = capsys.readouterr()
    assert 'private-pass' not in output.out + output.err


@requires_private_files
@pytest.mark.parametrize('problem', ['public', 'symlink', 'directory', 'oversize', 'malformed',
                                    'duplicate', 'missing', 'colon_username'])
def test_private_basic_file_refusal(basic_credentials, monkeypatch, problem, tmp_path):
    path = basic_credentials
    if problem == 'public':
        path.chmod(0o640)
    elif problem == 'symlink':
        link = tmp_path / 'link'
        link.symlink_to(path)
        path = link
    elif problem == 'directory':
        path = tmp_path
    elif problem == 'oversize':
        path.write_text('x' * 4097)
    elif problem == 'malformed':
        path.write_text('not-json-private-pass')
    elif problem == 'duplicate':
        path.write_text('{"username":"a","username":"b","password":"private-pass"}')
    elif problem == 'missing':
        path.unlink()
    elif problem == 'colon_username':
        path.write_text('{"username":"a:b","password":"private-pass"}')
    with pytest.raises(ValueError, match='^invalid private authentication file$'):
        cli._read_basic_credentials(path)


@pytest.mark.parametrize('problem', ['bearer', 'observation_bearer', 'unknown_mode', 'bearer_file'])
def test_conflicting_config_before_connection(basic_credentials, monkeypatch, problem):
    args = check_args(monkeypatch)
    key, value = {
        'bearer': ('AICC_TEAM_ARTIFACT_BEARER', 'token'),
        'observation_bearer': ('AICC_TEAM_OBSERVATION_BEARER', 'token'),
        'unknown_mode': ('AICC_TEAM_AUTH_MODE', 'other'),
        'bearer_file': ('AICC_TEAM_AUTH_MODE', 'bearer'),
    }[problem]
    monkeypatch.setenv(key, value)
    assert cli.main(args, connect=lambda *a, **k: pytest.fail('must not connect')) == 1


def test_basic_release_shared_origin_refusal(basic_credentials, monkeypatch):
    from argparse import Namespace
    args = Namespace(command='release', artifact_origin='https://one.example',
                     observation_origin='https://other.example')
    with pytest.raises(ValueError, match='same HTTPS origin'):
        cli._authentication(args)
    args.observation_origin = args.artifact_origin
    monkeypatch.setattr(cli, "_read_basic_credentials",
                        lambda path: ("operator", "private-pass"))
    assert cli._authentication(args) == (None, None, ('operator', 'private-pass'))


@pytest.mark.parametrize("capability", ["O_NOFOLLOW", "geteuid", "posix"])
def test_missing_private_file_capability_fails_before_open(monkeypatch, capability):
    # Restore the shared os module before autouse temporary-directory cleanup.
    with monkeypatch.context() as patch:
        if capability == "posix":
            patch.setattr(cli.os, "name", "nt")
        else:
            patch.delattr(cli.os, capability, raising=False)
        patch.setattr(cli.os, "open", lambda *a, **k: pytest.fail("must not read file"))
        with pytest.raises(ValueError, match="require POSIX security capabilities"):
            cli._read_basic_credentials("unused-private-credentials.json")
