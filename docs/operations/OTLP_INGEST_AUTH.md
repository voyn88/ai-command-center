# Authenticated OTLP export (SRV-08-OTLP-AUTH)

`command_center/otlp/` is AICC's client for shipping metrics, logs and traces
to an OTLP/HTTP collector. It exists to close one gap: the collector's own
protection used to be "bind on 127.0.0.1", which is not a boundary once a
worker runs on a different host from the collector. This package makes every
export an authenticated write, and makes an unauthenticated one impossible to
construct in the first place.

## The contract

* **Off by default.** `AICC_OTLP_ENDPOINT` unset means no client is built and
  nothing leaves the host.
* **No anonymous path.** Setting the endpoint without
  `AICC_OTLP_TOKEN_FILE` refuses to start. There is no
  `AICC_OTLP_INSECURE`, no "warn and continue" — `command_center/otlp/config.py`
  admits exactly two states, export-off and export-authenticated.
* **TLS, not `insecure: true`.** `https` is required; the only exemption is a
  loopback host, matching the topology `voyn-aicc-pgtunnel.service` already
  uses for the database (an SSH tunnel terminated on `127.0.0.1`). The
  exemption relaxes transport confidentiality only — loopback still requires
  a credential.
* **A worker credential, not a shared secret.** The bearer token lives in a
  file (`AICC_OTLP_TOKEN_FILE`), never an environment variable: AICC spawns
  agent subprocesses, and an env var would hand every one of them a token
  that can write into the trace/log store. Mode `0600` (or `0400` for a
  systemd `LoadCredential`) is enforced at load and on every re-read; a
  group- or world-readable file refuses.
* **Rotation without a restart.** The credential is identified by its path
  and re-read whenever the file's `(device, inode, mtime, size)` changes, so
  an operator (or `voyn-aicc-credential-rotation.service`-style automation,
  SRV-03) rotates the token by an atomic `os.replace` and the next export
  picks it up. A 401/403 forces one unconditional re-read before failing —
  closing the race where a rotation lands between the read and the ingest's
  verification — and a second rejection raises `OtlpAuthRejected` loudly
  rather than being swallowed as a transient error, because a silently empty
  trace store is worse than a paging alert.
* **No credential disclosure in transit.** The transport registers no
  redirect handler (a 3xx would otherwise carry the bearer token to whatever
  `Location` names) and no proxy handler (an inherited `http_proxy` cannot
  reroute telemetry through a third party). Any secret that reaches an
  exception or a log is passed through `Credential.redact` / `Config.redact`
  first.

See `command_center/otlp/config.py`, `credential.py` and `transport.py` for
the full reasoning — each has a module docstring that is the design record,
not just a summary.

## Configuration

```
AICC_OTLP_ENDPOINT=https://collector.internal:4318
AICC_OTLP_TOKEN_FILE=/etc/voyn/secrets/otlp_token
AICC_OTLP_TIMEOUT_SECONDS=10   # optional, default 10, range [0.1, 120]
```

See `.env.example` for the same, with the deployment reasoning inline.

## Deploying the token to a worker host

Same idiom the fleet already uses for the lease client's `pgpass` file:

```ini
# voyn-aicc-worker.service
[Service]
LoadCredential=otlp_token:/etc/voyn/secrets/otlp_token
Environment=AICC_OTLP_ENDPOINT=https://collector.internal:4318
Environment=AICC_OTLP_TOKEN_FILE=%d/otlp_token
```

`%d` is the systemd credentials directory (`$CREDENTIALS_DIRECTORY`),
mode-0400 and root-owned-but-readable only by the service's own user —
least privilege for the one process that needs to export telemetry, and
nothing an agent subprocess inherits.

## Verifying the credential

`python -m command_center.otlp check` posts an OTLP request whose payload is
an empty collection — well-formed, creates no telemetry, safe to run against
production. Run it as the same user and with the same environment the worker
uses, since a token readable by root and not by the service account passes
every other check:

```
AICC_OTLP_ENDPOINT=https://collector.internal:4318 \
AICC_OTLP_TOKEN_FILE=/etc/voyn/secrets/otlp_token \
python -m command_center.otlp check
```

Exit codes distinguish who needs to act:

| Code | Meaning | Who fixes it |
| --- | --- | --- |
| 0 | accepted | nobody — the credential works |
| 1 | rejected (401/403 twice) | rotate or re-grant the token |
| 2 | unreachable, or not an OTLP receiver | network/collector operator |
| 3 | misconfigured (bad env, bad file) | whoever set the environment |
| 4 | export switched off | nothing to check |

## Scope

This package is the consumer side: it decides whether AICC may export, and
proves the credential it holds actually works. Provisioning a
least-privilege, write-only OTLP credential on the collector side, and the
collector's own authentication enforcement, are the producer side (SRV-08a)
and are out of this package's scope — `command_center/otlp/cli.py check` is
the tool an operator runs once that provisioning exists, to confirm the two
sides agree.
