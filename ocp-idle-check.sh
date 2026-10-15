#!/bin/bash
#
# OpenShift Cluster Idle Detection Script - entrypoint shim.
#
# The implementation lives in ocp_idle_check.py (Python 3, stdlib only).
# This shim exists so every current caller keeps working unchanged:
#   - the Jenkins job ("bash ocp-idle-check.sh ...")
#   - cluster-monitor's vendored copy (subprocess.run(["bash", ...]))
#   - the README examples
#
# Exit codes:
#   0 = Cluster is ACTIVE
#   1 = Cluster is IDLE
#   2 = Error (cannot determine state)
#
set -uo pipefail

# Guard the interpreter before anything else: a missing python3 would make
# exec fail with 127, and a too-old python3 would die on import with exit 1,
# which callers would misread as IDLE.  Both must be a clean exit 2.
#
# The versioned name is tried first because the Jenkins agent image ships a
# bare python3 of 3.9 alongside a full python3.12, and the module needs 3.12+.
PYTHON_BIN=""
for candidate in python3.12 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    found=""
    for candidate in python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            found="${found} ${candidate}=$("$candidate" -V 2>&1)"
        fi
    done
    if [ -n "$found" ]; then
        echo "ERROR: python3 >= 3.12 required; found:${found}" >&2
    else
        echo "ERROR: python3 not found" >&2
    fi
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PYTHON_BIN" "$SCRIPT_DIR/ocp_idle_check.py" "$@"
 