#!/usr/bin/env bash
# AML Service entrypoint
# 1. Требует явно заданный адрес прослушивания (fail-closed)
# 2. Создаёт директорию данных (если не смонтирована)
# 3. Засевает правила 115-ФЗ (идемпотентно — пропускает уже существующие)
# 4. Запускает Streamlit

set -euo pipefail

# The application has NO authentication layer yet performs privileged git/gh and
# subprocess operations, so the interface it listens on is a security decision
# and must be made deliberately. There is deliberately NO default: a default is
# something an operator can inherit without ever seeing it, and the previous
# default here was 0.0.0.0. Refusing to start makes the omission impossible
# instead of merely unlikely, and the cost of that refusal is one restart with
# the variable set, whereas the cost of a silent wrong default is an
# unauthenticated privileged console on a reachable interface.
#
# docker-compose.aml.yml sets this to 0.0.0.0 on purpose: inside the container's
# own network namespace that reaches nothing by itself, and the compose file
# publishes the port on loopback only unless the operator overrides it.
if [[ -z "${STREAMLIT_SERVER_ADDRESS:-}" ]]; then
    echo "[AML] FATAL: STREAMLIT_SERVER_ADDRESS is not set." >&2
    echo "[AML] This service has no authentication; the listening interface must be" >&2
    echo "[AML] chosen explicitly. Use 127.0.0.1 to keep it on the host, or 0.0.0.0" >&2
    echo "[AML] only when the container network namespace is private and the port is" >&2
    echo "[AML] published on a specific host address." >&2
    echo "[AML] Publishing off loopback is not authorized without the reverse proxy" >&2
    echo "[AML] required by docs/adr/0011-streamlit-console-identity-boundary.md." >&2
    exit 78  # EX_CONFIG
fi

DATA_DIR="${AICC_DATA_DIR:-/data}"
RULES_DB="${DATA_DIR}/aml_rules.db"

echo "[AML] Data directory: ${DATA_DIR}"
mkdir -p "${DATA_DIR}"

echo "[AML] Seeding 115-ФЗ rules (idempotent)..."
python -m command_center.seed_rules_115fz --db "${RULES_DB}"
echo "[AML] Rules seeded."

echo "[AML] Starting Streamlit on ${STREAMLIT_SERVER_ADDRESS}:${STREAMLIT_SERVER_PORT:-8501} ..."
exec streamlit run app.py \
    --server.port "${STREAMLIT_SERVER_PORT:-8501}" \
    --server.address "${STREAMLIT_SERVER_ADDRESS}" \
    --server.headless true \
    --browser.gatherUsageStats false
