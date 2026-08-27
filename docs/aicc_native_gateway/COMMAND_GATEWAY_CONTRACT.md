# Command Gateway — contract requirements (no implementation in v1)

The read-only Gateway v1 deliberately exposes no write surface. Any future
mutating capability for native clients ("commands") must be a **separate
contract revision** implemented behind these non-negotiable requirements.
This document is the frozen requirement set; implementing a command endpoint
that satisfies fewer than all of them is prohibited.

## 1. Authorization

- Commands require a distinct scope (e.g. `command:<verb>`), never the v1
  `read` scope. Read tokens must be structurally unable to mutate.
- Device identity and user identity are both bound to every command; a
  device token alone is insufficient for destructive verbs.
- Scope grants are provisioned out-of-band by the operator, are revocable
  individually, and default to deny.

## 2. Policy decision

- Every command passes a server-side policy check (verb × target × identity ×
  current state) before any effect. Policy unavailability is fail-closed:
  the command is refused, never queued "for later" implicitly.
- Policy decisions are versioned; the policy version that admitted a command
  is recorded in its audit record.

## 3. Idempotency

- Every command carries a client-generated idempotency key. Replays return
  the original outcome without re-execution.
- Keys are stored durably with the outcome for at least the retention window
  of the audit log; a replayed key with a *different* payload is a `409`.

## 4. Explicit confirmation

- Commands with irreversible or outward-facing effect require a two-step
  flow: the server returns a confirmation challenge describing the exact
  effect; the client must echo it back. Single-shot execution of such verbs
  is prohibited regardless of scope.

## 5. Durable audit

- Every accepted, refused and confirmed command produces an append-only
  audit record (who, which device, verb, target, policy version,
  idempotency key, outcome, timestamps) persisted **before** the effect is
  acknowledged. No durable record → no acknowledgement.
- Audit records are redacted with the same boundary as v1 responses and are
  readable through a future read route, not through raw logs.

## 6. Ownership

- AIOS remains the executor and owner of all state transitions. The Command
  Gateway only validates, records and forwards intents into AIOS-owned
  queues; it never mutates AIOS state directly.
