#!/bin/sh
# Runs the repository's test scripts (each is a plain `python file.py`, exit 0 = all checks passed) one file at a
# time, so a failure is attributable. Suites: analysis classify pc ui tools contracts docker registry all.
# Exit 1 if any file failed. A file that cannot run here is reported as SKIP with the reason and is NOT counted as a pass.
# Not run here at all: ui/test_app_js.js (needs node, not in the image); see docker/README.md.
cd /src || exit 2
suite="${1:-all}"
failed=0
passed=0
skipped=0

skip() {
    echo "=== SKIP $1 ($2)"
    skipped=$((skipped + 1))
}

run() {
    f="$1"
    case "$f" in
    pc/test_run_ledger.py)
        command -v powershell.exe >/dev/null 2>&1 || { skip "$f" "needs Windows PowerShell, powershell.exe not found"; return; } ;;
    esac
    echo "=== RUN  $f"
    python "$f"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "=== PASS $f"
        passed=$((passed + 1))
    elif [ "$rc" -eq 77 ]; then
        skip "$f" "the test reported exit 77: a prerequisite is missing, see its output"
    else
        echo "=== FAIL $f (exit $rc)"
        failed=$((failed + 1))
    fi
}

files() {
    case "$1" in
    analysis)  ls analysis/test_*.py ;;
    classify)  ls classify/test_*.py ;;
    pc)        ls pc/test_*.py ;;
    ui)        ls ui/test_*.py ;;
    tools)     ls tools/test_*.py ;;
    contracts) ls contracts/test_contracts.py ;;
    docker)    ls docker/test_*.py ;;
    registry)  ls scripts/test_features_registry.py ;;
    *) echo "unknown suite: $1 (analysis classify pc ui tools contracts docker all)" >&2; return 1 ;;
    esac
}

if [ "$suite" = all ]; then
    suites="analysis classify pc ui tools contracts docker registry"
else
    suites="$suite"
fi
for s in $suites; do
    list=$(files "$s") || exit 2
    for f in $list; do
        run "$f"
    done
done
echo "=== $passed passed, $failed failed, $skipped skipped"
[ "$failed" -eq 0 ]
