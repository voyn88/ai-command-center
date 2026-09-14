#!/bin/sh
set -eu

# The provider toolchain is no longer installed from npm on the host.
#
# This script used to run `npm install --global` as root, which made production
# resolve packages online AND execute third-party lifecycle scripts with root
# privileges. That was the first blocker on VOYN-W0-AICC-AGENT-PUBLISHER-
# PRINCIPAL-ISOLATION, and it is closed by building the toolchain once in CI
# and shipping it as a content-addressed artifact:
#
#   .github/workflows/build-agent-toolchain.yml  builds it with --ignore-scripts
#   deploy/agent-toolchain.lock.json             pins versions and the sha256
#   ops/aicc_toolchain_install.py                verifies, extracts, selects
#
# The file is kept rather than deleted because `ops/aicc_exact_sha_bootstrap.py`
# lists it among the entrypoints whose presence the exact-SHA attestation
# requires; removing it is a separate, reviewed change to that payload list.
# Until then it fails closed instead of silently doing nothing, so an operator
# following an old runbook is told where the toolchain now comes from rather
# than left believing packages were installed.

cat >&2 <<'MESSAGE'
install-agent-toolchain.sh is retired and installs nothing.

Production must not resolve npm. The provider toolchain is now a
content-addressed artifact built in CI and installed by:

  /usr/bin/python3 ops/aicc_toolchain_install.py --lock deploy/agent-toolchain.lock.json

That runs as part of deploy/install-agent-principal-isolation.sh; you do not
normally invoke it yourself. To change a CLI version, edit
deploy/agent-toolchain.lock.json, run the build-agent-toolchain workflow, and
record the digest it prints back into the lock as a reviewed change.
MESSAGE
exit 1
