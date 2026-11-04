import hashlib
import json

import pytest

from command_center.deployment.delivery_receipts import ReleaseTarget, collect_release_receipt
from command_center.deployment import delivery_receipts as receipts


SHA = "a" * 40
TARGET = ReleaseTarget("repo", "https://release.example/version", "https://release.example/healthz")
ARTIFACT_BASE = "https://release.example/api/team/artifacts"


class Connection:
    def __init__(self, *, accepted=True, repo="repo"):
        self.accepted = accepted
        self.scope = {"repo": repo, "source_sha": SHA, "criteria": ["c1"]}
        self.calls = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchone(self):
        return (self.scope if len(self.calls) == 1 else self.accepted,)


def observer(values):
    sequence = iter(values)
    return lambda url: next(sequence)


def served_artifacts(directory):
    def read(url):
        assert url.startswith(ARTIFACT_BASE + "/")
        return (directory / url.rsplit("/", 1)[-1]).read_bytes()
    return read


@pytest.mark.parametrize("values", [
    [{"sha": "b" * 40}, {"status": "ok"}, {"sha": SHA}],
    [{"sha": SHA}, {"status": "failed"}, {"sha": SHA}],
    [{"sha": SHA}, {"status": "ok"}, {"sha": "b" * 40}],
    [{}, {"status": "ok"}, {"sha": SHA}],
])
def test_failed_or_changing_release_cannot_create_receipt(tmp_path, values):
    connection = Connection()
    with pytest.raises(ValueError, match="observation"):
        collect_release_receipt(connection, task_id="VOYN-T", revision=1,
                                target=TARGET, artifact_directory=tmp_path,
                                artifact_base_url=ARTIFACT_BASE, read_artifact=served_artifacts(tmp_path),
                                observe=observer(values))
    assert len(connection.calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_matching_runtime_records_only_scoped_observation(tmp_path):
    connection = Connection()
    artifact = collect_release_receipt(
        connection, task_id="VOYN-T", revision=1, target=TARGET,
        artifact_directory=tmp_path, artifact_base_url=ARTIFACT_BASE,
        read_artifact=served_artifacts(tmp_path),
        observe=observer([{"sha": SHA, "secret": "must-not-copy"},
                          {"status": "ok"}, {"sha": SHA}]),
    )
    record = json.loads(artifact.read_text())
    assert record["criteria"] == ["c1"]
    assert record["business_acceptance"] is False
    assert "must-not-copy" not in artifact.read_text()
    assert "sha256=" in connection.calls[-1][1][-1]
    assert connection.calls[-1][1][-1] == ARTIFACT_BASE + "/" + artifact.name + "#sha256=" + artifact.stem
    assert "file://" not in connection.calls[-1][1][-1]


def test_scope_change_during_observation_is_not_success(tmp_path):
    with pytest.raises(ValueError, match="scope changed"):
        collect_release_receipt(Connection(accepted=False), task_id="VOYN-T", revision=1,
                                target=TARGET, artifact_directory=tmp_path,
                                artifact_base_url=ARTIFACT_BASE, read_artifact=served_artifacts(tmp_path),
                                observe=observer([{"sha": SHA}, {"status": "ok"}, {"sha": SHA}]))


def test_foreign_repo_is_rejected_before_network(tmp_path):
    def no_network(url):
        pytest.fail("foreign scope must not trigger network access")
    with pytest.raises(ValueError, match="foreign"):
        collect_release_receipt(Connection(repo="other"), task_id="VOYN-T", revision=1,
                                target=TARGET, artifact_directory=tmp_path, observe=no_network,
                                artifact_base_url=ARTIFACT_BASE, read_artifact=no_network)


@pytest.mark.parametrize("url", ["http://release.example/version", "https://user:pass@release.example/version", "https://other.example/version"])
def test_target_configuration_rejects_insecure_or_foreign_origin(url):
    with pytest.raises(ValueError):
        ReleaseTarget("repo", url, TARGET.health_url).validate()


class Response:
    status = 200

    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.raw[:limit]


def test_private_observer_pins_credential_to_origin_and_does_not_persist_it(monkeypatch, tmp_path):
    requests = []
    secret = "private-test-token"

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 15
            value = {"sha": SHA} if request.full_url.endswith("version") else {"status": "ok"}
            return Response(json.dumps({**value, "secret": secret}).encode())

    monkeypatch.setattr(receipts, "build_opener", lambda handler: Opener())
    observe = receipts.configured_observer("https://release.example", bearer=secret)
    artifact = collect_release_receipt(Connection(), task_id="VOYN-T", revision=1,
                                      target=TARGET, artifact_directory=tmp_path,
                                      artifact_base_url=ARTIFACT_BASE, read_artifact=served_artifacts(tmp_path),
                                      observe=observe)
    assert all(r.get_header("Authorization") == "Bearer " + secret for r in requests)
    assert secret not in artifact.read_text()
    with pytest.raises(ValueError, match="foreign"):
        observe("https://other.example/version")
    assert len(requests) == 3


@pytest.mark.parametrize("mode", ["unavailable", "mismatch"])
def test_release_receipt_requires_readable_exact_retained_artifact(tmp_path, mode):
    connection = Connection()

    def read(url):
        if mode == "unavailable":
            raise RuntimeError("private upstream credential must not appear")
        return b"different retained content"

    with pytest.raises(ValueError, match="release artifact readback") as caught:
        collect_release_receipt(connection, task_id="VOYN-T", revision=1,
                                target=TARGET, artifact_directory=tmp_path,
                                artifact_base_url=ARTIFACT_BASE, read_artifact=read,
                                observe=observer([{"sha": SHA}, {"status": "ok"}, {"sha": SHA}]))
    assert "credential" not in str(caught.value)
    assert len(connection.calls) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1  # Observation only, no DB receipt.


@pytest.mark.parametrize("base", [
    "", "http://release.example/artifacts", "https://user:secret@release.example/artifacts",
    "https://release.example/artifacts?token=secret", "https://release.example/artifacts#fragment",
])
def test_release_refuses_missing_or_unsafe_artifact_endpoint_before_observation(tmp_path, base):
    connection = Connection()
    with pytest.raises(ValueError):
        collect_release_receipt(connection, task_id="VOYN-T", revision=1,
                                target=TARGET, artifact_directory=tmp_path,
                                artifact_base_url=base,
                                observe=lambda url: pytest.fail("must refuse configuration before observing"))
    assert connection.calls == []
    assert list(tmp_path.iterdir()) == []


def test_fetch_failure_and_malformed_json_do_not_expose_response_or_credential(monkeypatch):
    secret = "private-test-token"

    class Opener:
        def open(self, request, timeout):
            raise RuntimeError("transport echoed " + secret)

    monkeypatch.setattr(receipts, "build_opener", lambda handler: Opener())
    with pytest.raises(ValueError) as caught:
        receipts.fetch_observation(TARGET.version_url, bearer=secret)
    assert secret not in str(caught.value)
    assert caught.value.__suppress_context__
    monkeypatch.setattr(receipts, "fetch_artifact_bytes", lambda *a, **k: secret.encode())
    with pytest.raises(ValueError) as caught:
        receipts.fetch_observation(TARGET.version_url, bearer=secret)
    assert secret not in str(caught.value)


def test_fetch_bounds_response_and_refuses_redirects(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            return Response(b"x" * 65537)

    monkeypatch.setattr(receipts, "build_opener", lambda handler: Opener())
    with pytest.raises(ValueError, match="fetch failed"):
        receipts.fetch_artifact_bytes(TARGET.version_url)
    with pytest.raises(ValueError, match="redirect"):
        receipts._NoRedirect().redirect_request(None, None, 302, "found", {}, "https://other.example")


def acceptance_bytes(**changes):
    return json.dumps({
        "schema": "delivery-acceptance-observation/v1", "task_id": "VOYN-T",
        "revision": 1, "sha": SHA, "environment": "production", "criterion_id": "c1",
        "check_id": "workflow-acceptance-v1", "outcome": "passed",
        "observed_at": "2026-11-04T12:00:00Z", **changes,
    }).encode()


def admit(connection, raw, **overrides):
    args = dict(task_id="VOYN-T", revision=1, repo="repo", criterion_id="c1",
                check_id="workflow-acceptance-v1", artifact_url="https://evidence.example/check.json",
                sha256=hashlib.sha256(raw).hexdigest(), read=lambda url: raw)
    args.update(overrides)
    return receipts.record_acceptance_artifact(connection, **args)


def test_acceptance_reads_hashed_artifact_and_records_exact_scope():
    connection = Connection()
    raw = acceptance_bytes()
    reference = admit(connection, raw)
    query, params = connection.calls[-1]
    assert "delivery_team_record_acceptance" in query
    assert params[:5] == ("VOYN-T", 1, SHA, "c1", "workflow-acceptance-v1")
    assert params[5] == reference == "https://evidence.example/check.json#sha256=" + hashlib.sha256(raw).hexdigest()
    assert params[6].isoformat() == "2026-11-04T12:00:00+00:00"


@pytest.mark.parametrize("changes", [
    {"task_id": "VOYN-OTHER"}, {"revision": 2}, {"revision": True},
    {"sha": "b" * 40}, {"environment": "staging"}, {"criterion_id": "c2"},
    {"check_id": "unrelated-check"}, {"outcome": "failed"},
    {"schema": "delivery-release-observation/v1"}, {"verified": True},
    {"observed_at": "2026-11-04T12:00:00"}, {"observed_at": None},
])
def test_acceptance_rejects_wrong_check_scope_and_model_claims(changes):
    connection = Connection()
    with pytest.raises(ValueError):
        admit(connection, acceptance_bytes(**changes))
    assert len(connection.calls) == 1


@pytest.mark.parametrize("raw", [b"{}", b"x" * 65537, b"{\"a\":1,\"a\":2}"])
def test_acceptance_rejects_missing_duplicate_or_oversized_artifact(raw):
    connection = Connection()
    with pytest.raises(ValueError):
        admit(connection, raw)
    assert len(connection.calls) == 1


def test_acceptance_refuses_hash_mismatch_stale_scope_and_database_refusal():
    connection = Connection()
    with pytest.raises(ValueError, match="hash"):
        admit(connection, acceptance_bytes(), sha256="0" * 64)
    assert len(connection.calls) == 1
    with pytest.raises(ValueError, match="foreign"):
        admit(Connection(repo="other"), acceptance_bytes(),
              read=lambda url: pytest.fail("foreign scope must not fetch"))
    with pytest.raises(ValueError, match="refused"):
        admit(Connection(accepted=False), acceptance_bytes())


@pytest.mark.parametrize("url", [
    "http://evidence.example/a", "https://user:secret@evidence.example/a",
    "https://evidence.example/a?token=secret", "https://evidence.example/a#sha256=bad",
])
def test_acceptance_rejects_credential_or_untrusted_urls_before_database(url):
    connection = Connection()
    with pytest.raises(ValueError):
        admit(connection, acceptance_bytes(), artifact_url=url)
    assert connection.calls == []


def test_private_artifact_reader_pins_origin(monkeypatch):
    calls = []
    monkeypatch.setattr(receipts, "fetch_artifact_bytes",
                        lambda url, **kwargs: calls.append((url, kwargs)) or b"check")
    read = receipts.configured_artifact_reader("https://evidence.example", bearer="private-token")
    assert read("https://evidence.example/check") == b"check"
    with pytest.raises(ValueError, match="foreign"):
        read("https://other.example/check")
    assert calls == [("https://evidence.example/check", {"bearer": "private-token"})]


def corrective_bytes(**changes):
    return json.dumps({
        "schema": "delivery-corrective-observation/v1", "parent_task_id": "VOYN-T",
        "revision": 1, "incident_key": "b" * 64, "corrective_task_id": "VOYN-FIX",
        "corrective_revision": 3, "sha": SHA, "criterion_id": "c1",
        "check_id": "recurrence-check-v1", "outcome": "passed",
        "observed_at": "2026-11-04T12:00:00Z", **changes,
    }).encode()


def admit_corrective(connection, raw, **overrides):
    args = dict(parent_task_id="VOYN-T", revision=1, incident_key="b" * 64,
                corrective_task_id="VOYN-FIX", corrective_revision=3, sha=SHA,
                criterion_id="c1", check_id="recurrence-check-v1",
                artifact_url="https://evidence.example/correction.json",
                sha256=hashlib.sha256(raw).hexdigest(), read=lambda url: raw)
    args.update(overrides)
    return receipts.record_corrective_artifact(connection, **args)


class CorrectiveConnection(Connection):
    def fetchone(self):
        return (self.accepted,)


def test_corrective_proof_preserves_all_assignment_and_check_bindings():
    connection = CorrectiveConnection()
    raw = corrective_bytes()
    reference = admit_corrective(connection, raw)
    query, params = connection.calls[0]
    assert "delivery_team_record_corrective_check" in query
    assert params[:8] == ("VOYN-T", 1, "b" * 64, "VOYN-FIX", 3, SHA, "c1", "recurrence-check-v1")
    assert params[8] == reference == "https://evidence.example/correction.json#sha256=" + hashlib.sha256(raw).hexdigest()
    assert params[9].isoformat() == "2026-11-04T12:00:00+00:00"


@pytest.mark.parametrize("changes", [
    {"parent_task_id": "VOYN-OTHER"}, {"revision": 2}, {"revision": True},
    {"incident_key": "c" * 64}, {"corrective_task_id": "VOYN-OTHER"},
    {"corrective_revision": 4}, {"corrective_revision": True}, {"sha": "c" * 40},
    {"criterion_id": "different-goal"}, {"check_id": "ordinary-green-ci"},
    {"outcome": "failed"}, {"schema": "delivery-acceptance-observation/v1"},
    {"observed_at": "2026-11-04T12:00:00"}, {"verified": True},
])
def test_corrective_rejects_unrelated_stale_or_model_generated_claims(changes):
    connection = CorrectiveConnection()
    with pytest.raises(ValueError):
        admit_corrective(connection, corrective_bytes(**changes))
    assert connection.calls == []


def test_corrective_hash_mismatch_and_sql_refusal_are_not_success():
    connection = CorrectiveConnection()
    with pytest.raises(ValueError, match="hash"):
        admit_corrective(connection, corrective_bytes(), sha256="0" * 64)
    assert connection.calls == []
    with pytest.raises(ValueError, match="refused"):
        admit_corrective(CorrectiveConnection(accepted=False), corrective_bytes())


def test_corrective_fetch_error_is_redacted():
    def failing_read(url):
        raise RuntimeError("credential-that-must-not-be-echoed")

    connection = CorrectiveConnection()
    with pytest.raises(ValueError) as caught:
        admit_corrective(connection, corrective_bytes(), read=failing_read)
    assert str(caught.value) == "corrective artifact fetch failed"
    assert caught.value.__suppress_context__
    assert connection.calls == []


@pytest.mark.parametrize('basic,bearer,expected', [
    (('operator', 'private-pass'), None, 'Basic b3BlcmF0b3I6cHJpdmF0ZS1wYXNz'),
    (None, 'private-token', 'Bearer private-token'),
])
def test_transport_auth_header_and_redirect_refusal(monkeypatch, basic, bearer, expected):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, bound):
            assert bound == 65537
            return b'{"status":"ok"}'

    class Opener:
        def open(self, request, timeout):
            assert request.get_header('Authorization') == expected
            assert timeout == 15
            return Response()

    def build(handler):
        with pytest.raises(ValueError, match='must not follow redirects'):
            handler.redirect_request(None, None, 302, 'Found', {}, 'https://other.example')
        return Opener()
    monkeypatch.setattr(receipts, 'build_opener', build)
    assert receipts.fetch_observation('https://edge.example/healthz',
                                      basic=basic, bearer=bearer) == {'status': 'ok'}


@pytest.mark.parametrize('factory', [receipts.configured_observer, receipts.configured_artifact_reader])
def test_basic_origin_pin_refuses_before_transport(monkeypatch, factory):
    monkeypatch.setattr(receipts, 'build_opener', lambda *a: pytest.fail('no network'))
    read = factory('https://edge.example', basic=('operator', 'private-pass'))
    for url in ('https://other.example/check', 'http://edge.example/check'):
        with pytest.raises(ValueError):
            read(url)
    with pytest.raises(ValueError, match='conflicting'):
        factory('https://edge.example', basic=('operator', 'private-pass'), bearer='token')


def test_basic_transport_failure_is_sanitized(monkeypatch):
    def fail(*args):
        raise RuntimeError('operator private-pass')
    monkeypatch.setattr(receipts, 'build_opener', fail)
    with pytest.raises(ValueError) as error:
        receipts.fetch_artifact_bytes('https://edge.example/check',
                                      basic=('operator', 'private-pass'))
    assert str(error.value) == 'observation fetch failed'
    assert error.value.__suppress_context__
