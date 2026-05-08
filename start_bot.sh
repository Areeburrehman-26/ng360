#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SKIP_TESTS=0
TESTS_ONLY=0

# Supported skip styles:
#   ./start_bot.sh /skipt_test=1
#   ./start_bot.sh --skip-tests
#   SKIP_TESTS=1 ./start_bot.sh
#   skipt_test=1 ./start_bot.sh
# Supported test-only styles:
#   ./start_bot.sh --tests-only
#   TESTS_ONLY=1 ./start_bot.sh
for arg in "$@"; do
	case "$arg" in
		/skipt_test=1|/skip_test=1|--skip-tests|--skip-test)
			SKIP_TESTS=1
			;;
		--tests-only)
			TESTS_ONLY=1
			;;
	esac
done

if [[ "${SKIP_TESTS:-0}" != "1" && "${skipt_test:-0}" == "1" ]]; then
	SKIP_TESTS=1
fi
if [[ "${SKIP_TESTS:-0}" != "1" && "${SKIP_TESTS:-0}" == "1" ]]; then
	SKIP_TESTS=1
fi
if [[ "${TESTS_ONLY:-0}" != "1" && "${TESTS_ONLY:-0}" == "1" ]]; then
	TESTS_ONLY=1
fi

log() {
	printf '[start_bot] %s\n' "$1"
}

die() {
	printf '[start_bot] ERROR: %s\n' "$1" >&2
	exit 1
}

require_file() {
	local target="$1"
	[[ -f "$target" ]] || die "Missing required file: $target"
}

run_preflight_checks() {
	log "Running preflight checks..."

	command -v python >/dev/null 2>&1 || die "python command not found in PATH"

	require_file "core/webhook_server.py"
	require_file "core/worker.py"
	require_file "core/bridge_bot.py"
	require_file "core/queue_manager.py"
	require_file "services/ghl_client.py"
	require_file "services/drive_uploader.py"
	require_file "services/notifier.py"
	require_file "utils/data_formatter.py"

	log "Checking Python syntax for all source files..."
	mapfile -t py_files < <(find core services utils scripts -type f -name "*.py" 2>/dev/null | sort)
	[[ ${#py_files[@]} -gt 0 ]] || die "No Python files found under core/services/utils/scripts"
	python -m py_compile "${py_files[@]}"

	log "Running import smoke checks..."
	python - <<'PY'
import importlib

modules = [
		"core.webhook_server",
		"core.worker",
		"core.bridge_bot",
		"core.queue_manager",
		"services.ghl_client",
		"services.drive_uploader",
		"services.notifier",
		"utils.data_formatter",
]

for name in modules:
		importlib.import_module(name)

print("[start_bot] Import checks passed")
PY

	mapfile -t test_files < <(find . -type f \( -name "test_*.py" -o -name "*_test.py" \) ! -path "./.venv/*" ! -path "./venv/*" | sort)
	if [[ ${#test_files[@]} -gt 0 ]]; then
		command -v pytest >/dev/null 2>&1 || die "pytest not found but test files exist"
		log "Running pytest..."
		pytest -q
	else
		log "No test files found; skipping pytest run."
	fi

	log "Preflight checks completed successfully."
}

run_tests_only() {
	log "Running test suite only..."
	command -v python >/dev/null 2>&1 || die "python command not found in PATH"
	command -v pytest >/dev/null 2>&1 || die "pytest not found in PATH"
	pytest -q
	log "Test suite completed successfully."
}

start_services() {
	log "Starting webhook server..."
	python -m core.webhook_server &
	WEBHOOK_PID=$!

	log "Starting worker..."
	python -m core.worker &
	WORKER_PID=$!

	log "Webhook PID: $WEBHOOK_PID"
	log "Worker PID:  $WORKER_PID"

	trap 'kill "$WEBHOOK_PID" "$WORKER_PID" 2>/dev/null || true' EXIT
	wait
}

if [[ "$TESTS_ONLY" == "1" ]]; then
	run_tests_only
	exit 0
fi

if [[ "$SKIP_TESTS" == "1" ]]; then
	log "Skip flag detected. Preflight tests/checks are skipped."
else
	run_preflight_checks
fi

start_services
