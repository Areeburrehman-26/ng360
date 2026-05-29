#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SKIP_TESTS=0
TESTS_ONLY=0
NGROK_PID=""

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

ensure_python_dep() {
	local module_name="$1"
	python - <<PY >/dev/null 2>&1
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("${module_name}") else 1)
PY
}

start_ngrok_tunnel() {
	if [[ "${ENABLE_NGROK:-0}" != "1" ]]; then
		return
	fi

	local port="${WEBHOOK_PORT:-${GHL_WEBHOOK_PORT:-8004}}"

	if [[ -n "${NGROK_URL:-}" ]]; then
		log "Using configured NGROK_URL: ${NGROK_URL}"
		log "Public webhook URL: ${NGROK_URL%/}/webhook"
		return
	fi

	if [[ -n "${NGROK_DOMAIN:-}" ]]; then
		NGROK_URL="https://${NGROK_DOMAIN}"
		export NGROK_URL
		log "Using configured NGROK_DOMAIN: ${NGROK_DOMAIN}"
		log "Public webhook URL: ${NGROK_URL%/}/webhook"
		return
	fi

	if [[ -z "${NGROK_AUTH_TOKEN:-}" ]]; then
		die "ENABLE_NGROK=1 but NGROK_AUTH_TOKEN is empty (or set NGROK_URL/NGROK_DOMAIN)"
	fi

	log "Starting ngrok tunnel on port ${port}..."
	if ensure_python_dep "pyngrok"; then
		python - "${port}" <<'PY' &
import json
import os
import signal
import sys
import time
from pathlib import Path
from pyngrok import ngrok

port = int(sys.argv[1])
token = os.environ.get("NGROK_AUTH_TOKEN", "").strip()
if token:
    ngrok.set_auth_token(token)
public = ngrok.connect(addr=port, bind_tls=True)
url = public.public_url
Path("data").mkdir(parents=True, exist_ok=True)
Path("data/ngrok_url.txt").write_text(url, encoding="utf-8")
print(json.dumps({"public_url": url}), flush=True)

running = True
def _stop(_sig, _frame):
    global running
    running = False

signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)
while running:
    time.sleep(1)
ngrok.kill()
PY
		NGROK_PID=$!

		for _ in {1..20}; do
			if [[ -f "data/ngrok_url.txt" ]]; then
				NGROK_URL="$(<data/ngrok_url.txt)"
				export NGROK_URL
				break
			fi
			sleep 0.5
		done
	else
		command -v ngrok >/dev/null 2>&1 || die "ngrok not installed and pyngrok unavailable"
		ngrok config add-authtoken "${NGROK_AUTH_TOKEN}" >/dev/null 2>&1 || true
		ngrok http "${port}" --log=stdout > "logs/ngrok.log" 2>&1 &
		NGROK_PID=$!
		sleep 2
		NGROK_URL="$(python - <<'PY'
import json
import urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3) as r:
        data = json.load(r)
    tunnels = data.get("tunnels", [])
    public = next((t.get("public_url") for t in tunnels if str(t.get("public_url", "")).startswith("https://")), "")
    print(public)
except Exception:
    print("")
PY
)"
		if [[ -n "${NGROK_URL}" ]]; then
			export NGROK_URL
		fi
	fi

	if [[ -z "${NGROK_URL:-}" ]]; then
		die "Failed to resolve ngrok public URL"
	fi

	log "ngrok PID: ${NGROK_PID:-n/a}"
	log "Public webhook URL: ${NGROK_URL%/}/webhook"
}

cleanup() {
	kill "${WEBHOOK_PID:-}" "${WORKER_PID:-}" "${NGROK_PID:-}" 2>/dev/null || true
}

start_services() {
	start_ngrok_tunnel

	log "Starting webhook server..."
	python -m core.webhook_server &
	WEBHOOK_PID=$!

	log "Starting worker..."
	python -m core.worker &
	WORKER_PID=$!

	log "Webhook PID: $WEBHOOK_PID"
	log "Worker PID:  $WORKER_PID"

	trap cleanup EXIT INT TERM
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
