#!/usr/bin/env bash
# Reproducible, evidence-producing check of the *public* GitHub signals for
# voyn88/ai-command-center branch protection.
#
# Why this exists
# ---------------
# DR-GITHUB-BRANCH-PROTECTION-001 asserts operational facts about the live
# repository. Adversarial review of PR #663 correctly rejected baking those
# facts into DECISIONS.md without a cited evidence trail. This script is the
# reproducible check: anyone (or CI) can re-run it without admin credentials
# and compare the live public subset against the expected required checks.
#
# Scope (honest limits)
# ---------------------
# Unauthenticated GET .../branches/main returns only a subset:
#   protected, protection.enabled, protection.required_status_checks
# Fields that require authenticated GET .../branches/{branch}/protection
# (required_pull_request_reviews, enforce_admins, allow_force_pushes,
# allow_deletions, restrictions, ...) are deliberately NOT asserted here.
# When GH_TOKEN / gh auth is available, the script additionally dumps the
# authenticated protection document into the evidence directory but does not
# invent defaults for missing fields.
#
# Usage:
#   bash scripts/verify-github-branch-protection.sh
#   bash scripts/verify-github-branch-protection.sh --out /tmp/bp-evidence
# Exit 0 on match; exit 1 on mismatch / transport failure.
set -euo pipefail

OWNER_REPO="${OWNER_REPO:-voyn88/ai-command-center}"
BRANCH="${BRANCH:-main}"
API="${GITHUB_API_URL:-https://api.github.com}"
OUT=""
EXPECTED_CONTEXTS=(
  "Final merge gate"
  "Acceptance gate (independent verdict on exact SHA)"
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="${2:?}"; shift 2 ;;
    --repo) OWNER_REPO="${2:?}"; shift 2 ;;
    --branch) BRANCH="${2:?}"; shift 2 ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

CAPTURED_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
if [[ -z "$OUT" ]]; then
  OUT="$(mktemp -d "/tmp/bp-verify.XXXXXX")"
fi
mkdir -p "$OUT"

auth_args=()
if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
  token="${GH_TOKEN:-$GITHUB_TOKEN}"
  auth_args=(-H "Authorization: Bearer ${token}")
fi

fetch() {
  local path="$1" dest="$2"
  local code
  code="$(curl -sS -o "$dest" -w '%{http_code}' \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    "${auth_args[@]}" \
    "${API}${path}")"
  if [[ "$code" != "200" ]]; then
    echo "GET ${path} -> HTTP ${code}; body:" >&2
    head -c 2000 "$dest" >&2 || true
    echo >&2
    return 1
  fi
}

echo "→ capturing public GitHub protection signals for ${OWNER_REPO}@${BRANCH} at ${CAPTURED_AT}"
echo "$CAPTURED_AT" >"$OUT/captured_at_utc.txt"

fetch "/repos/${OWNER_REPO}" "$OUT/repo.json"
fetch "/repos/${OWNER_REPO}/branches/${BRANCH}" "$OUT/branch.json"
fetch "/repos/${OWNER_REPO}/rules/branches/${BRANCH}" "$OUT/rules_branch.json" || true
fetch "/repos/${OWNER_REPO}/rulesets" "$OUT/rulesets.json" || true

# Authenticated full protection doc when possible (evidence only; not required).
if [[ ${#auth_args[@]} -gt 0 ]]; then
  if fetch "/repos/${OWNER_REPO}/branches/${BRANCH}/protection" "$OUT/protection_authenticated.json"; then
    echo "  also captured authenticated protection document"
  else
    echo "  authenticated protection endpoint unavailable (continuing with public subset)"
    rm -f "$OUT/protection_authenticated.json"
  fi
fi

python3 - "$OUT" "$BRANCH" "${EXPECTED_CONTEXTS[@]}" <<'PY'
import json, sys, hashlib
from pathlib import Path

out = Path(sys.argv[1])
branch_name = sys.argv[2]
expected = list(sys.argv[3:])

repo = json.loads(out.joinpath("repo.json").read_text())
branch = json.loads(out.joinpath("branch.json").read_text())
rules = []
rules_path = out.joinpath("rules_branch.json")
if rules_path.exists():
    try:
        rules = json.loads(rules_path.read_text())
    except json.JSONDecodeError:
        rules = {"_error": "unparseable"}
rulesets = []
rulesets_path = out.joinpath("rulesets.json")
if rulesets_path.exists():
    try:
        rulesets = json.loads(rulesets_path.read_text())
    except json.JSONDecodeError:
        rulesets = {"_error": "unparseable"}

prot = branch.get("protection") or {}
rsc = prot.get("required_status_checks") or {}
contexts = list(rsc.get("contexts") or [])
captured = out.joinpath("captured_at_utc.txt").read_text().strip()

summary = {
    "captured_at_utc": captured,
    "source": "GitHub REST API via scripts/verify-github-branch-protection.sh",
    "visibility": {
        "full_name": repo.get("full_name"),
        "private": repo.get("private"),
        "visibility": repo.get("visibility"),
        "default_branch": repo.get("default_branch"),
    },
    "branch": {
        "name": branch.get("name"),
        "protected": branch.get("protected"),
        "protection_public_subset": prot,
        "head_sha_at_capture": (branch.get("commit") or {}).get("sha"),
    },
    "rules_applied_to_branch": rules,
    "rulesets": [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "target": r.get("target"),
            "enforcement": r.get("enforcement"),
        }
        for r in (rulesets if isinstance(rulesets, list) else [])
    ],
    "expected_required_contexts": expected,
    "observed_required_contexts": contexts,
    "authenticated_protection_captured": out.joinpath(
        "protection_authenticated.json"
    ).exists(),
    "limits": (
        "Unauthenticated branch endpoint returns only enabled + "
        "required_status_checks. Do not infer required_pull_request_reviews "
        "or enforce_admins from this subset."
    ),
}
raw = json.dumps(summary, indent=2, sort_keys=True) + "\n"
out.joinpath("SUMMARY.json").write_text(raw)
digest = hashlib.sha256(raw.encode()).hexdigest()
print(f"evidence_dir={out}")
print(f"summary_sha256={digest}")
print(f"summary_bytes={len(raw.encode())}")
print(f"visibility={repo.get('visibility')} private={repo.get('private')}")
print(f"protected={branch.get('protected')} enabled={prot.get('enabled')}")
print(f"required_contexts={contexts!r}")
print(f"enforcement_level={rsc.get('enforcement_level')!r}")

errors = []
if repo.get("private") is not False or repo.get("visibility") != "public":
    errors.append(
        f"expected public visibility; got private={repo.get('private')!r} "
        f"visibility={repo.get('visibility')!r}"
    )
if branch.get("name") != branch_name:
    errors.append(f"expected branch {branch_name!r}, got {branch.get('name')!r}")
if branch.get("protected") is not True:
    errors.append("branch.protected is not true")
if prot.get("enabled") is not True:
    errors.append("protection.enabled is not true")
missing = [c for c in expected if c not in contexts]
extra = [c for c in contexts if c not in expected]
if missing:
    errors.append(f"missing required contexts: {missing}")
if extra:
    errors.append(f"unexpected required contexts: {extra}")
if rsc.get("enforcement_level") not in (None, "everyone", "non_admins"):
    errors.append(f"unexpected enforcement_level: {rsc.get('enforcement_level')!r}")
# Prefer everyone; warn (non-fatal) if weaker.
if rsc.get("enforcement_level") == "non_admins":
    print("WARNING: enforcement_level=non_admins (admins may bypass required checks)")

if errors:
    print("VERIFY_FAIL")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
print("VERIFY_OK")
PY
