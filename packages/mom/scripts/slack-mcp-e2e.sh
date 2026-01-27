#!/usr/bin/env bash
#
# Mom Slack E2E Test
#
# Fully self-contained script that:
#   1. Loads secrets from env vars or optional .env file
#   2. Validates required tools and env vars
#   3. Creates a temp workspace and Docker sandbox container
#   4. Starts slack-mcp-server in background
#   5. Starts mom from source in background
#   6. Sends a test message via mcporter (ad-hoc HTTP)
#   7. Polls conversation history for mom's response
#   8. Reports pass/fail and cleans up everything
#
# Usage:
#   ./packages/mom/scripts/slack-mcp-e2e.sh
#
# Required env vars (or in .env file):
#   MOM_SLACK_APP_TOKEN    - Slack app-level token (xapp-...)
#   MOM_SLACK_BOT_TOKEN    - Slack bot token (xoxb-...)
#   ANTHROPIC_API_KEY      - (or other provider key) for mom's LLM calls
#   SLACK_CHANNEL          - channel ID or #name to test in
#   MOM_MENTION            - bot mention string (e.g., <@U123>)
#   MOM_BOT_USER_ID        - bot user ID for response verification
#
# For slack-mcp-server (user token required to trigger app_mention):
#   SLACK_MCP_XOXP_TOKEN   - user token for slack-mcp-server (preferred)
#   SLACK_MCP_XOXB_TOKEN   - bot token (won't trigger app_mention if same as MOM_SLACK_BOT_TOKEN)
#
# Optional:
#   DOCKER_CONTAINER_NAME  - sandbox container name (default: mom-e2e-sandbox)
#   SLACK_MCP_SERVER_PORT  - port for slack-mcp-server (default: 13080)
#   TIMEOUT_SECONDS        - how long to wait for mom's response (default: 120)
#   POLL_INTERVAL_SECONDS  - polling interval (default: 3)
#   TEST_MESSAGE           - custom test message (default: auto-generated)
#   MOM_MODEL              - provider/model to use (e.g., anthropic/claude-sonnet-4-20250514)

set -euo pipefail

# ============================================================================
# Constants
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# ============================================================================
# .env loading
# ============================================================================

# Load .env from repo root if present (do not override existing env vars)
if [[ -f "$REPO_ROOT/.env" ]]; then
	echo "Loading .env from $REPO_ROOT/.env"
	while IFS= read -r line || [[ -n "$line" ]]; do
		# Skip comments and blank lines
		[[ "$line" =~ ^[[:space:]]*# ]] && continue
		[[ -z "${line// /}" ]] && continue
		# Extract key
		key="${line%%=*}"
		key="${key// /}"
		# Only set if not already in env
		if [[ -z "${!key:-}" ]]; then
			export "$line"
		fi
	done < "$REPO_ROOT/.env"
fi

# Also check for .env in the mom package dir
if [[ -f "$SCRIPT_DIR/../.env" ]]; then
	echo "Loading .env from $SCRIPT_DIR/../.env"
	while IFS= read -r line || [[ -n "$line" ]]; do
		[[ "$line" =~ ^[[:space:]]*# ]] && continue
		[[ -z "${line// /}" ]] && continue
		key="${line%%=*}"
		key="${key// /}"
		if [[ -z "${!key:-}" ]]; then
			export "$line"
		fi
	done < "$SCRIPT_DIR/../.env"
fi

# ============================================================================
# Defaults
# ============================================================================

: "${DOCKER_CONTAINER_NAME:=mom-e2e-sandbox}"
: "${SLACK_MCP_SERVER_PORT:=13080}"
: "${TIMEOUT_SECONDS:=120}"
: "${POLL_INTERVAL_SECONDS:=3}"
: "${TEST_MESSAGE:=mom-e2e-test-$(date +%s)-$$}"

SLACK_MCP_SERVER_URL="http://127.0.0.1:${SLACK_MCP_SERVER_PORT}/mcp"

# ============================================================================
# State (for cleanup)
# ============================================================================

MOM_PID=""
SLACK_MCP_PID=""
TEMP_WORKSPACE=""
MOM_LOG=""
SLACK_MCP_LOG=""
CONTAINER_CREATED=false

# ============================================================================
# Cleanup
# ============================================================================

cleanup() {
	local exit_code=$?
	echo ""
	echo "--- Cleanup ---"

	# Kill mom
	if [[ -n "$MOM_PID" ]] && kill -0 "$MOM_PID" 2>/dev/null; then
		echo "Stopping mom (PID $MOM_PID)..."
		kill "$MOM_PID" 2>/dev/null || true
		wait "$MOM_PID" 2>/dev/null || true
	fi

	# Kill slack-mcp-server
	if [[ -n "$SLACK_MCP_PID" ]] && kill -0 "$SLACK_MCP_PID" 2>/dev/null; then
		echo "Stopping slack-mcp-server (PID $SLACK_MCP_PID)..."
		kill "$SLACK_MCP_PID" 2>/dev/null || true
		wait "$SLACK_MCP_PID" 2>/dev/null || true
	fi

	# Remove Docker container if we created it
	if [[ "$CONTAINER_CREATED" == true ]]; then
		echo "Removing Docker container $DOCKER_CONTAINER_NAME..."
		docker rm -f "$DOCKER_CONTAINER_NAME" >/dev/null 2>&1 || true
	fi

	# Dump logs on failure
	if [[ $exit_code -ne 0 ]]; then
		if [[ -n "$MOM_LOG" && -f "$MOM_LOG" ]]; then
			echo ""
			echo "--- Mom log (last 50 lines) ---"
			tail -50 "$MOM_LOG" 2>/dev/null || true
		fi
		if [[ -n "$SLACK_MCP_LOG" && -f "$SLACK_MCP_LOG" ]]; then
			echo ""
			echo "--- slack-mcp-server log (last 30 lines) ---"
			tail -30 "$SLACK_MCP_LOG" 2>/dev/null || true
		fi
	fi

	# Remove temp files
	if [[ -n "$TEMP_WORKSPACE" && -d "$TEMP_WORKSPACE" ]]; then
		echo "Removing temp workspace $TEMP_WORKSPACE..."
		rm -rf "$TEMP_WORKSPACE"
	fi

	if [[ $exit_code -eq 0 ]]; then
		echo "Cleanup complete."
	else
		echo "Cleanup complete (test failed with exit code $exit_code)."
	fi
}

trap cleanup EXIT

# ============================================================================
# Validation: tools
# ============================================================================

missing_tools=()

for tool in mcporter npx docker; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		missing_tools+=("$tool")
	fi
done

# tsx can be local (npx tsx) - check if npx can resolve it
if ! command -v tsx >/dev/null 2>&1; then
	if ! npx tsx --version >/dev/null 2>&1; then
		missing_tools+=("tsx (install with: npm install -g tsx)")
	fi
fi

if [[ ${#missing_tools[@]} -gt 0 ]]; then
	echo "ERROR: Missing required tools:" >&2
	for tool in "${missing_tools[@]}"; do
		echo "  - $tool" >&2
	done
	exit 1
fi

# Check Docker is running
if ! docker info >/dev/null 2>&1; then
	echo "ERROR: Docker is not running." >&2
	exit 1
fi

# ============================================================================
# Validation: env vars
# ============================================================================

missing_vars=()

for var in MOM_SLACK_APP_TOKEN MOM_SLACK_BOT_TOKEN SLACK_CHANNEL MOM_MENTION MOM_BOT_USER_ID; do
	if [[ -z "${!var:-}" ]]; then
		missing_vars+=("$var")
	fi
done

# Need at least one provider key
if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" && -z "${GOOGLE_API_KEY:-}" ]]; then
	missing_vars+=("ANTHROPIC_API_KEY (or OPENAI_API_KEY or GOOGLE_API_KEY)")
fi

# Need a user token for slack-mcp-server to post messages as a human.
# Using the same bot token (SLACK_MCP_XOXB_TOKEN) won't work because Slack
# doesn't fire app_mention events for a bot's own messages.
if [[ -z "${SLACK_MCP_XOXP_TOKEN:-}" ]]; then
	if [[ -n "${SLACK_MCP_XOXB_TOKEN:-}" ]]; then
		echo "WARNING: SLACK_MCP_XOXB_TOKEN is set but SLACK_MCP_XOXP_TOKEN is not." >&2
		echo "  If the bot token is the same as MOM_SLACK_BOT_TOKEN, the test message" >&2
		echo "  will appear to come from the bot itself, and Slack won't trigger an" >&2
		echo "  app_mention event. Set SLACK_MCP_XOXP_TOKEN (a user token) instead." >&2
	else
		missing_vars+=("SLACK_MCP_XOXP_TOKEN (user token for posting test messages)")
	fi
fi

if [[ ${#missing_vars[@]} -gt 0 ]]; then
	echo "ERROR: Missing required environment variables:" >&2
	for var in "${missing_vars[@]}"; do
		echo "  - $var" >&2
	done
	exit 1
fi

# ============================================================================
# Setup
# ============================================================================

echo "=== Mom Slack E2E Test ==="
echo "  Channel:    $SLACK_CHANNEL"
echo "  Message:    $TEST_MESSAGE"
echo "  Timeout:    ${TIMEOUT_SECONDS}s"
echo "  Container:  $DOCKER_CONTAINER_NAME"
echo "  MCP port:   $SLACK_MCP_SERVER_PORT"
echo ""

# Create temp workspace
TEMP_WORKSPACE="$(mktemp -d -t mom-e2e-XXXXXXXX)"
echo "Temp workspace: $TEMP_WORKSPACE"

# Log files
MOM_LOG="$TEMP_WORKSPACE/mom.log"
SLACK_MCP_LOG="$TEMP_WORKSPACE/slack-mcp-server.log"

# Common mcporter args for ad-hoc HTTP calls
MCPORTER_ARGS=(--http-url "$SLACK_MCP_SERVER_URL" --allow-http --name slack-mcp)

# Write settings.json if MOM_MODEL is set (format: provider/model-id)
if [[ -n "${MOM_MODEL:-}" ]]; then
	mom_provider="${MOM_MODEL%%/*}"
	mom_model_id="${MOM_MODEL#*/}"
	echo "Using model: $mom_provider/$mom_model_id"
	cat > "$TEMP_WORKSPACE/settings.json" << SETTINGS_EOF
{
  "defaultProvider": "$mom_provider",
  "defaultModel": "$mom_model_id"
}
SETTINGS_EOF
fi

# ============================================================================
# Step 1: Create Docker sandbox container
# ============================================================================

echo ""
echo "--- Step 1: Docker sandbox ---"

if docker ps -a --format '{{.Names}}' | rg -q "^${DOCKER_CONTAINER_NAME}$"; then
	echo "Container $DOCKER_CONTAINER_NAME already exists, removing..."
	docker rm -f "$DOCKER_CONTAINER_NAME" >/dev/null
fi

echo "Creating container $DOCKER_CONTAINER_NAME..."
docker run -d \
	--name "$DOCKER_CONTAINER_NAME" \
	-v "$TEMP_WORKSPACE:/workspace" \
	alpine:latest \
	tail -f /dev/null >/dev/null

CONTAINER_CREATED=true
echo "Container created."

# ============================================================================
# Step 2: Start slack-mcp-server
# ============================================================================

echo ""
echo "--- Step 2: Start slack-mcp-server ---"

# Kill anything on the target port from previous runs
if lsof -ti:"$SLACK_MCP_SERVER_PORT" >/dev/null 2>&1; then
	echo "Killing leftover process on port $SLACK_MCP_SERVER_PORT..."
	lsof -ti:"$SLACK_MCP_SERVER_PORT" | xargs kill 2>/dev/null || true
	sleep 1
fi

# Build env for slack-mcp-server
SLACK_MCP_ENV=()
SLACK_MCP_ENV+=("SLACK_MCP_ADD_MESSAGE_TOOL=true")
SLACK_MCP_ENV+=("SLACK_MCP_PORT=$SLACK_MCP_SERVER_PORT")
[[ -n "${SLACK_MCP_XOXB_TOKEN:-}" ]] && SLACK_MCP_ENV+=("SLACK_MCP_XOXB_TOKEN=$SLACK_MCP_XOXB_TOKEN")
[[ -n "${SLACK_MCP_XOXP_TOKEN:-}" ]] && SLACK_MCP_ENV+=("SLACK_MCP_XOXP_TOKEN=$SLACK_MCP_XOXP_TOKEN")

echo "Starting slack-mcp-server on port $SLACK_MCP_SERVER_PORT..."
env "${SLACK_MCP_ENV[@]}" \
	npx slack-mcp-server -transport http \
	>"$SLACK_MCP_LOG" 2>&1 &
SLACK_MCP_PID=$!
echo "slack-mcp-server PID: $SLACK_MCP_PID"

# Wait for it to be ready (poll the HTTP endpoint)
echo "Waiting for slack-mcp-server to be ready..."
deadline=$((SECONDS + 30))
while (( SECONDS < deadline )); do
	if ! kill -0 "$SLACK_MCP_PID" 2>/dev/null; then
		echo "ERROR: slack-mcp-server exited prematurely." >&2
		tail -20 "$SLACK_MCP_LOG" 2>/dev/null || true
		exit 1
	fi
	# Try a simple HTTP request to the MCP endpoint
	if curl -sf -o /dev/null -X POST \
		-H "Content-Type: application/json" \
		-d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.0.1"}}}' \
		"$SLACK_MCP_SERVER_URL" 2>/dev/null; then
		echo "slack-mcp-server is ready."
		break
	fi
	sleep 1
done

if (( SECONDS >= deadline )); then
	echo "ERROR: slack-mcp-server did not become ready within 30s." >&2
	exit 1
fi

# ============================================================================
# Step 3: Start mom
# ============================================================================

echo ""
echo "--- Step 3: Start mom ---"

echo "Starting mom from source..."
cd "$REPO_ROOT"
npx tsx packages/mom/src/main.ts \
	"--sandbox=docker:$DOCKER_CONTAINER_NAME" \
	"$TEMP_WORKSPACE" \
	>"$MOM_LOG" 2>&1 &
MOM_PID=$!
echo "Mom PID: $MOM_PID"

# Wait for mom to connect to Slack
# The log outputs: "⚡️ Mom bot connected and listening!"
echo "Waiting for mom to connect to Slack..."
deadline=$((SECONDS + 60))
while (( SECONDS < deadline )); do
	if ! kill -0 "$MOM_PID" 2>/dev/null; then
		echo "ERROR: Mom exited prematurely." >&2
		tail -30 "$MOM_LOG" 2>/dev/null || true
		exit 1
	fi
	if rg -q "Mom bot connected and listening" "$MOM_LOG" 2>/dev/null; then
		echo "Mom is connected and listening."
		break
	fi
	sleep 1
done

if (( SECONDS >= deadline )); then
	echo "ERROR: Mom did not connect within 60s." >&2
	exit 1
fi

# Give mom a moment to finish backfill and stabilize
sleep 2

# ============================================================================
# Step 4: Send test message
# ============================================================================

echo ""
echo "--- Step 4: Send test message ---"

payload="${MOM_MENTION} ${TEST_MESSAGE}"
echo "Sending: $payload"
echo "  to channel: $SLACK_CHANNEL"

mcporter call "${MCPORTER_ARGS[@]}" conversations_add_message \
	channel_id="$SLACK_CHANNEL" \
	payload="$payload" \
	content_type="text/plain" \
	--output text \
	>/dev/null

echo "Message sent."

# ============================================================================
# Step 5: Poll for mom's response
# ============================================================================

echo ""
echo "--- Step 5: Wait for mom's response ---"

echo "Polling every ${POLL_INTERVAL_SECONDS}s (timeout: ${TIMEOUT_SECONDS}s)..."

deadline=$((SECONDS + TIMEOUT_SECONDS))
attempt=0
while (( SECONDS < deadline )); do
	attempt=$((attempt + 1))

	# Check mom is still alive
	if ! kill -0 "$MOM_PID" 2>/dev/null; then
		echo "ERROR: Mom process died while waiting for response." >&2
		exit 1
	fi

	response_csv=$(mcporter call "${MCPORTER_ARGS[@]}" conversations_history \
		channel_id="$SLACK_CHANNEL" \
		limit=20 \
		--output text 2>/dev/null) || true

	if [[ -n "$response_csv" ]]; then
		# CSV columns: MsgID,UserID,UserName,RealName,Channel,ThreadTs,Text,...
		# Match lines where UserID (2nd column) is the bot AND the text contains
		# our test marker. This avoids false matches on the user's mention message.
		bot_lines=$(echo "$response_csv" | awk -F',' -v bot="$MOM_BOT_USER_ID" '$2 == bot') || true
		if [[ -n "$bot_lines" ]] && echo "$bot_lines" | rg -qF "$TEST_MESSAGE"; then
			echo ""
			echo "=== PASS ==="
			echo "Mom responded to the test message."
			echo ""
			echo "Bot response line(s):"
			echo "$bot_lines" | rg -F "$TEST_MESSAGE"
			echo ""
			echo "Recent conversation history:"
			echo "$response_csv" | head -30
			exit 0
		fi
	fi

	# Print progress every 5 attempts
	if (( attempt % 5 == 0 )); then
		remaining=$((deadline - SECONDS))
		echo "  Still waiting... (${remaining}s remaining)"
	fi

	sleep "$POLL_INTERVAL_SECONDS"
done

echo ""
echo "=== FAIL ==="
echo "Timed out after ${TIMEOUT_SECONDS}s waiting for mom's response." >&2
echo ""
echo "Last conversation history:"
mcporter call "${MCPORTER_ARGS[@]}" conversations_history \
	channel_id="$SLACK_CHANNEL" \
	limit=20 \
	--output text 2>/dev/null | head -30 || true
exit 1
