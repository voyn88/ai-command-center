#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="${AICC_START_TASK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

PROJECT="${1:-}"
TASK_TYPE="${2:-implementation}"
TASK_TEXT="${3:-}"

if [[ -z "$PROJECT" ]]; then
  echo "Usage:"
  echo "  ./scripts/start-task.sh AIOS implementation \"Describe the task\""
  echo
  echo "Projects:"
  echo "  AICC"
  echo "  AIOS"
  echo "  AICOS"
  echo "  PRODUCT"
  echo "  ECOSYSTEM"
  echo "  ESF"
  echo "  AML"
  echo "  CRM"
  echo "  BANK"
  echo "  LEGAL"
  echo "  BUSINESS"
  echo "  PERSONAL"
  exit 1
fi

PROJECT_UPPER="$(printf '%s' "$PROJECT" | tr '[:lower:]' '[:upper:]')"
TASK_TYPE_LOWER="$(printf '%s' "$TASK_TYPE" | tr '[:upper:]' '[:lower:]')"

case "$PROJECT_UPPER" in
  AICC|AICOS|PRODUCT|ECOSYSTEM|ESF|AML|CRM|BUSINESS|PERSONAL)
    CONTEXT_FILE="$ROOT_DIR/CURRENT_STATE.md"
    PROJECT_FILE="$ROOT_DIR/projects/${PROJECT_UPPER}.md"
    ;;
  AIOS)
    CONTEXT_FILE="$ROOT_DIR/context/AIOS_CONTEXT.md"
    PROJECT_FILE="$ROOT_DIR/projects/AIOS.md"
    ;;
  BANK|BANK_STRATEGY)
    PROJECT_UPPER="BANK"
    CONTEXT_FILE="$ROOT_DIR/context/BANK_CONTEXT.md"
    PROJECT_FILE="$ROOT_DIR/projects/BANK_STRATEGY.md"
    ;;
  LEGAL)
    CONTEXT_FILE="$ROOT_DIR/context/LEGAL_CONTEXT.md"
    PROJECT_FILE="$ROOT_DIR/projects/LEGAL.md"
    ;;
  *)
    echo "Unknown project: $PROJECT"
    echo "Supported projects: AICC, AIOS, AICOS, PRODUCT, ECOSYSTEM, ESF, AML, CRM, BANK, LEGAL, BUSINESS, PERSONAL"
    exit 1
    ;;
esac

if [[ ! -f "$CONTEXT_FILE" ]]; then
  CONTEXT_FILE="$ROOT_DIR/CURRENT_STATE.md"
fi

if [[ ! -f "$PROJECT_FILE" ]]; then
  PROJECT_FILE="$ROOT_DIR/CURRENT_STATE.md"
fi

case "$TASK_TYPE_LOWER" in
  implementation|review|remediation|final_gate|architecture_review)
    ;;
  *)
    echo "Unknown task type: $TASK_TYPE"
    echo "Supported task types:"
    echo "  implementation"
    echo "  review"
    echo "  remediation"
    echo "  final_gate"
    echo "  architecture_review"
    exit 1
    ;;
esac

if [[ ! -f "$CONTEXT_FILE" ]]; then
  echo "Missing context file: $CONTEXT_FILE"
  exit 1
fi

if [[ ! -f "$PROJECT_FILE" ]]; then
  echo "Missing project file: $PROJECT_FILE"
  exit 1
fi

if [[ -z "$TASK_TEXT" ]]; then
  echo
  echo "Enter the task objective."
  echo "Finish with Ctrl-D:"
  TASK_TEXT="$(cat)"
fi

if [[ -z "${TASK_TEXT// }" ]]; then
  echo "Task objective cannot be empty."
  exit 1
fi

TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
DATE_HUMAN="$(date '+%Y-%m-%d %H:%M:%S')"

OUTPUT_DIR="$ROOT_DIR/generated/$PROJECT_UPPER"
REPORT_DIR="$ROOT_DIR/reports/$PROJECT_UPPER"

mkdir -p "$OUTPUT_DIR" "$REPORT_DIR"

OUTPUT_FILE="$OUTPUT_DIR/${TIMESTAMP}_${TASK_TYPE_LOWER}.md"
REPORT_FILE="$REPORT_DIR/${TIMESTAMP}_${TASK_TYPE_LOWER}_report.md"

case "$TASK_TYPE_LOWER" in
  implementation)
    ROLE="You are a senior implementation engineer working under strict repository controls."
    EXECUTION_RULES=$(cat <<'RULES'
- Inspect the repository before changing files.
- Implement only the stated objective.
- Modify only files required for the task.
- Add or update tests for the changed behavior.
- Do not weaken tests.
- Do not commit, push, merge, reset, stash or rebase.
- Run all applicable validation.
RULES
)
    ;;
  review)
    ROLE="You are an independent principal software engineer performing a read-only review."
    EXECUTION_RULES=$(cat <<'RULES'
- Do not modify any file.
- Do not commit, push, merge, reset, stash or rebase.
- Review actual repository state, not prior claims.
- Verify behavior, tests, contracts, security and compatibility.
- Report findings with exact file and line references.
- Return APPROVED only when no blocking issue remains.
RULES
)
    ;;
  remediation)
    ROLE="You are a senior remediation engineer fixing only independently confirmed findings."
    EXECUTION_RULES=$(cat <<'RULES'
- Fix only the listed findings.
- Do not redesign unrelated architecture.
- Do not modify unrelated files.
- Add regression tests for every corrected defect.
- Do not weaken existing tests.
- Do not commit, push, merge, reset, stash or rebase.
- Run all applicable validation.
RULES
)
    ;;
  final_gate)
    ROLE="You are an independent release gate reviewer performing a final read-only verification."
    EXECUTION_RULES=$(cat <<'RULES'
- Do not modify any file.
- Verify the complete diff and working-tree state.
- Confirm all required findings are resolved.
- Confirm required tests genuinely cover the corrected behavior.
- Check packaging, generated artifacts and documentation where applicable.
- Return APPROVED FOR COMMIT or NOT APPROVED FOR COMMIT.
RULES
)
    ;;
  architecture_review)
    ROLE="You are an independent principal software architect performing a read-only architecture review."
    EXECUTION_RULES=$(cat <<'RULES'
- Do not modify any file.
- Evaluate invariants, ownership, contracts, state transitions and failure semantics.
- Check traceability between requirements, architecture, runtime and tests.
- Identify ambiguous or non-authoritative behavior.
- Report findings with severity and exact references.
RULES
)
    ;;
esac

cat > "$OUTPUT_FILE" <<EOF
# Agent Task

Generated: $DATE_HUMAN
Project: $PROJECT_UPPER
Task type: $TASK_TYPE_LOWER

## Role

$ROLE

## Program Context

Read and treat the following files as authoritative context:

- $CONTEXT_FILE
- $PROJECT_FILE
- $ROOT_DIR/CURRENT_STATE.md

Do not modify the Command Center files unless explicitly instructed.

## Objective

$TASK_TEXT

## Mandatory Execution Rules

$EXECUTION_RULES

## Repository Safety

Before acting, report:

- repository path;
- current branch;
- HEAD commit;
- git status;
- active worktree;
- whether the branch is ahead or behind its base.

Repository state is more authoritative than conversation history.

## Required Validation

Run all checks applicable to the changed or reviewed scope, including where relevant:

- targeted tests;
- full test suite;
- lint;
- type checking;
- schema or contract validation;
- generated-artifact drift checks;
- build and packaging checks;
- git diff;
- git status.

Never claim that a check passed unless it was executed.

## Required Final Report

Return:

1. Verdict or result
2. Scope inspected
3. Root cause or architectural assessment
4. Files changed or reviewed
5. Findings with severity
6. Tests added or assessed
7. Exact commands executed
8. Exact command results
9. Remaining risks
10. Git diff summary
11. Git status
12. Recommendation

Save the final report content so it can be copied into:

$REPORT_FILE
EOF

echo "Task prompt generated: $OUTPUT_FILE"
echo "Final report target: $REPORT_FILE"
