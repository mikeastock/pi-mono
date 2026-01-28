#!/usr/bin/env python3
"""
Mom Slack E2E Test

Fully self-contained script that:
  1. Loads secrets from env vars or optional .env file
  2. Validates required tools and env vars
  3. Creates a temp workspace and Docker sandbox container
  4. Starts slack-mcp-server in background
  5. Starts mom from source in background
  6. Sends a test message via mcporter (ad-hoc HTTP)
  7. Polls conversation history for mom's response
  8. Reports pass/fail and cleans up everything

Usage:
  ./packages/mom/scripts/slack-mcp-e2e.py [--prompt "custom message"]

Options:
  --prompt TEXT          Custom prompt to send to mom (optional)

Required env vars (or in .env file):
  MOM_SLACK_APP_TOKEN    - Slack app-level token (xapp-...)
  MOM_SLACK_BOT_TOKEN    - Slack bot token (xoxb-...)
  ANTHROPIC_API_KEY      - (or other provider key) for mom's LLM calls
  SLACK_CHANNEL          - channel ID or #name to test in
  MOM_MENTION            - bot mention string (e.g. <@U123>)
  MOM_BOT_USER_ID        - bot user ID for response verification

For slack-mcp-server (user token required to trigger app_mention):
  SLACK_MCP_XOXP_TOKEN   - user token for slack-mcp-server (preferred)
  SLACK_MCP_XOXB_TOKEN   - bot token (won't trigger app_mention if same as MOM_SLACK_BOT_TOKEN)

Optional:
  DOCKER_CONTAINER_NAME  - sandbox container name (default: mom-e2e-sandbox)
  SLACK_MCP_SERVER_PORT  - port for slack-mcp-server (default: 13080)
  TIMEOUT_SECONDS        - how long to wait for mom's response (default: 120)
  POLL_INTERVAL_SECONDS  - polling interval (default: 3)
  TEST_MESSAGE           - custom test message (default: auto-generated)
  MOM_MODEL              - provider/model to use (e.g. anthropic/claude-sonnet-4-20250514)
"""

import argparse
import atexit
import csv
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional


# ============================================================================
# Unbuffered print
# ============================================================================

# Force unbuffered stdout so output appears immediately when piped
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


# ============================================================================
# Constants
# ============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

# Global config, set in main()
CONFIG: dict = {}


# ============================================================================
# .env loading
# ============================================================================

def load_env_file(env_path: Path) -> None:
    """Load .env file without overriding existing env vars."""
    if not env_path.exists():
        return

    print(f"Loading .env from {env_path}")
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            # Skip comments and blank lines
            if not line or line.startswith("#"):
                continue
            # Extract key
            if "=" not in line:
                continue
            key = line.split("=")[0].strip()
            # Only set if not already in env
            if key not in os.environ:
                os.environ[key] = line.split("=", 1)[1].strip()


def load_env_files() -> None:
    load_env_file(REPO_ROOT / ".env")
    load_env_file(SCRIPT_DIR.parent / ".env")


# ============================================================================
# Configuration
# ============================================================================

def get_config(prompt: Optional[str] = None) -> dict:
    """Get configuration from environment variables with defaults."""
    # Use provided prompt, env var TEST_MESSAGE, or generate auto message
    if prompt:
        test_marker = f"test-{int(time.time())}-{os.getpid()}"
        test_message = f"{prompt} ({test_marker})"
    elif os.environ.get("TEST_MESSAGE"):
        test_message = os.environ.get("TEST_MESSAGE")
        test_marker = None  # Use full message for matching
    else:
        test_marker = f"test-{int(time.time())}-{os.getpid()}"
        test_message = f"mom-e2e-{test_marker}"

    return {
        "docker_container_name": os.environ.get("DOCKER_CONTAINER_NAME", "mom-e2e-sandbox"),
        "slack_mcp_server_port": int(os.environ.get("SLACK_MCP_SERVER_PORT", "13080")),
        "timeout_seconds": int(os.environ.get("TIMEOUT_SECONDS", "120")),
        "poll_interval_seconds": int(os.environ.get("POLL_INTERVAL_SECONDS", "3")),
        "test_message": test_message,
        "test_marker": test_marker,
        "mom_model": os.environ.get("MOM_MODEL", ""),
        # Required vars
        "mom_slack_app_token": os.environ.get("MOM_SLACK_APP_TOKEN", ""),
        "mom_slack_bot_token": os.environ.get("MOM_SLACK_BOT_TOKEN", ""),
        "slack_channel": os.environ.get("SLACK_CHANNEL", ""),
        "mom_mention": os.environ.get("MOM_MENTION", ""),
        "mom_bot_user_id": os.environ.get("MOM_BOT_USER_ID", ""),
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
        "google_api_key": os.environ.get("GOOGLE_API_KEY", ""),
        "slack_mcp_xoxp_token": os.environ.get("SLACK_MCP_XOXP_TOKEN", ""),
        "slack_mcp_xoxb_token": os.environ.get("SLACK_MCP_XOXB_TOKEN", ""),
    }


# ============================================================================
# State (for cleanup)
# ============================================================================

class State:
    def __init__(self):
        self.mom_proc: Optional[subprocess.Popen] = None
        self.slack_mcp_proc: Optional[subprocess.Popen] = None
        self.temp_workspace: Optional[Path] = None
        self.mom_log: Optional[Path] = None
        self.slack_mcp_log: Optional[Path] = None
        self.container_created: bool = False
        self.exit_code: int = 0
        self.cleaned_up: bool = False


STATE = State()


# ============================================================================
# Cleanup
# ============================================================================

def _kill_proc_tree(proc: subprocess.Popen, label: str, timeout: int = 5) -> None:
    """Kill a process and its entire process group."""
    print(f"Stopping {label} (PID {proc.pid})...")
    try:
        # Kill the entire process group (npx + its children)
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def cleanup() -> None:
    """Clean up all resources. Called via atexit."""
    if STATE.cleaned_up:
        return
    STATE.cleaned_up = True

    print("")
    print("--- Cleanup ---")

    # Kill mom
    if STATE.mom_proc and STATE.mom_proc.poll() is None:
        _kill_proc_tree(STATE.mom_proc, "mom")

    # Kill slack-mcp-server
    if STATE.slack_mcp_proc and STATE.slack_mcp_proc.poll() is None:
        _kill_proc_tree(STATE.slack_mcp_proc, "slack-mcp-server")

    # Remove Docker container if we created it
    if STATE.container_created and CONFIG:
        print(f"Removing Docker container {CONFIG['docker_container_name']}...")
        subprocess.run(
            ["docker", "rm", "-f", CONFIG["docker_container_name"]],
            capture_output=True
        )

    # Dump logs on failure
    if STATE.exit_code != 0:
        if STATE.mom_log and STATE.mom_log.exists():
            print("")
            print("--- Mom log (last 50 lines) ---")
            _tail_file(STATE.mom_log, 50)
        if STATE.slack_mcp_log and STATE.slack_mcp_log.exists():
            print("")
            print("--- slack-mcp-server log (last 30 lines) ---")
            _tail_file(STATE.slack_mcp_log, 30)

    # Remove temp files
    if STATE.temp_workspace and STATE.temp_workspace.exists():
        print(f"Removing temp workspace {STATE.temp_workspace}...")
        shutil.rmtree(STATE.temp_workspace, ignore_errors=True)

    if STATE.exit_code == 0:
        print("Cleanup complete.")
    else:
        print(f"Cleanup complete (test failed with exit code {STATE.exit_code}).")


def _on_signal(signum: int, frame) -> None:
    """Handle SIGTERM/SIGINT by setting exit code and exiting (triggers atexit)."""
    STATE.exit_code = 1
    sys.exit(1)


def _tail_file(path: Path, lines: int) -> None:
    """Print the last N lines of a file."""
    try:
        with open(path, "r") as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                print(line.rstrip())
    except Exception:
        pass


# ============================================================================
# Validation: tools
# ============================================================================

def validate_tools() -> None:
    """Validate that required tools are available."""
    missing_tools = []

    for cmd in ("mcporter", "npx", "docker"):
        if not shutil.which(cmd):
            missing_tools.append(cmd)

    # Check tsx (local or via npx)
    if not shutil.which("tsx"):
        try:
            subprocess.run(
                ["npx", "tsx", "--version"],
                capture_output=True,
                check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            missing_tools.append("tsx (install with: npm install -g tsx)")

    if missing_tools:
        print("ERROR: Missing required tools:", file=sys.stderr)
        for tool in missing_tools:
            print(f"  - {tool}", file=sys.stderr)
        sys.exit(1)

    # Check Docker is running
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=True
        )
    except subprocess.CalledProcessError:
        print("ERROR: Docker is not running.", file=sys.stderr)
        sys.exit(1)


# ============================================================================
# Validation: env vars
# ============================================================================

def validate_env_vars(config: dict) -> None:
    """Validate that required environment variables are set."""
    missing_vars = []

    for var in ("MOM_SLACK_APP_TOKEN", "MOM_SLACK_BOT_TOKEN", "SLACK_CHANNEL",
                "MOM_MENTION", "MOM_BOT_USER_ID"):
        if not config[var.lower()]:
            missing_vars.append(var)

    # Need at least one provider key
    if not (config["anthropic_api_key"] or config["openai_api_key"] or config["google_api_key"]):
        missing_vars.append("ANTHROPIC_API_KEY (or OPENAI_API_KEY or GOOGLE_API_KEY)")

    # Need a user token for slack-mcp-server to post messages as a human.
    if not config["slack_mcp_xoxp_token"]:
        if config["slack_mcp_xoxb_token"]:
            print(
                "WARNING: SLACK_MCP_XOXB_TOKEN is set but SLACK_MCP_XOXP_TOKEN is not.\n"
                "  If the bot token is the same as MOM_SLACK_BOT_TOKEN, the test message\n"
                "  will appear to come from the bot itself, and Slack won't trigger an\n"
                "  app_mention event. Set SLACK_MCP_XOXP_TOKEN (a user token) instead.",
                file=sys.stderr
            )
        else:
            missing_vars.append("SLACK_MCP_XOXP_TOKEN (user token for posting test messages)")

    if missing_vars:
        print("ERROR: Missing required environment variables:", file=sys.stderr)
        for var in missing_vars:
            print(f"  - {var}", file=sys.stderr)
        sys.exit(1)


# ============================================================================
# Setup
# ============================================================================

def setup_workspace() -> Path:
    """Create temp workspace and return its path."""
    temp_dir = tempfile.mkdtemp(prefix="mom-e2e-")
    temp_path = Path(temp_dir)
    print(f"Temp workspace: {temp_path}")
    return temp_path


def write_settings(temp_workspace: Path, config: dict) -> None:
    """Write settings.json if MOM_MODEL is set."""
    if not config["mom_model"]:
        return

    provider, _, model_id = config["mom_model"].partition("/")
    if not provider or not model_id:
        print(f"WARNING: Invalid MOM_MODEL format: {config['mom_model']}")
        return

    print(f"Using model: {provider}/{model_id}")
    settings = {
        "defaultProvider": provider,
        "defaultModel": model_id
    }
    with open(temp_workspace / "settings.json", "w") as f:
        json.dump(settings, f, indent=2)


# ============================================================================
# Docker sandbox
# ============================================================================

def create_docker_container(config: dict) -> None:
    """Create Docker sandbox container."""
    print("")
    print("--- Step 1: Docker sandbox ---")

    container_name = config["docker_container_name"]

    # Check if container already exists
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True
        )
        existing_containers = result.stdout.strip().split("\n")
        if container_name in existing_containers:
            print(f"Container {container_name} already exists, removing...")
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True
            )
    except subprocess.CalledProcessError:
        pass

    print(f"Creating container {container_name}...")
    subprocess.run(
        [
            "docker", "run", "-d",
            "--name", container_name,
            "-v", f"{STATE.temp_workspace}:/workspace",
            "alpine:latest",
            "tail", "-f", "/dev/null"
        ],
        capture_output=True,
        check=True
    )
    STATE.container_created = True
    print("Container created.")


# ============================================================================
# slack-mcp-server
# ============================================================================

def kill_port_process(port: int) -> None:
    """Kill any process listening on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split("\n")
            for pid in pids:
                if pid:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except (ValueError, ProcessLookupError):
                        pass
            time.sleep(1)
    except FileNotFoundError:
        pass


def start_slack_mcp_server(config: dict) -> subprocess.Popen:
    """Start slack-mcp-server and return the Popen object."""
    print("")
    print("--- Step 2: Start slack-mcp-server ---")

    port = config["slack_mcp_server_port"]

    # Kill anything on the target port from previous runs
    kill_port_process(port)

    # Build env for slack-mcp-server
    env = os.environ.copy()
    env["SLACK_MCP_ADD_MESSAGE_TOOL"] = "true"
    env["SLACK_MCP_PORT"] = str(port)
    if config["slack_mcp_xoxb_token"]:
        env["SLACK_MCP_XOXB_TOKEN"] = config["slack_mcp_xoxb_token"]
    if config["slack_mcp_xoxp_token"]:
        env["SLACK_MCP_XOXP_TOKEN"] = config["slack_mcp_xoxp_token"]

    print(f"Starting slack-mcp-server on port {port}...")

    log_file = open(STATE.slack_mcp_log, "w")
    proc = subprocess.Popen(
        ["npx", "slack-mcp-server", "-transport", "http"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,  # Create new process group for clean killpg
    )

    print(f"slack-mcp-server PID: {proc.pid}")
    return proc


def wait_for_slack_mcp_server(proc: subprocess.Popen, port: int, timeout: int = 30) -> bool:
    """Wait for slack-mcp-server to be ready."""
    print("Waiting for slack-mcp-server to be ready...")

    slack_mcp_url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + timeout

    while time.time() < deadline:
        if proc.poll() is not None:
            print("ERROR: slack-mcp-server exited prematurely.", file=sys.stderr)
            _tail_file(STATE.slack_mcp_log, 20)
            return False

        try:
            result = subprocess.run(
                [
                    "curl", "-sf", "-o", "/dev/null",
                    "-X", "POST",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "0.0.1"}
                        }
                    }),
                    slack_mcp_url
                ],
                capture_output=True,
                timeout=2
            )
            if result.returncode == 0:
                print("slack-mcp-server is ready.")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        time.sleep(1)

    print("ERROR: slack-mcp-server did not become ready within 30s.", file=sys.stderr)
    return False


# ============================================================================
# Mom
# ============================================================================

def start_mom(config: dict) -> subprocess.Popen:
    """Start mom from source and return the Popen object."""
    print("")
    print("--- Step 3: Start mom ---")

    print("Starting mom from source...")

    log_file = open(STATE.mom_log, "w")
    proc = subprocess.Popen(
        [
            "npx", "tsx",
            str(REPO_ROOT / "packages/mom/src/main.ts"),
            f"--sandbox=docker:{config['docker_container_name']}",
            str(STATE.temp_workspace)
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=REPO_ROOT,
        start_new_session=True,  # Create new process group for clean killpg
    )

    print(f"Mom PID: {proc.pid}")
    return proc


def wait_for_mom(proc: subprocess.Popen, timeout: int = 60) -> bool:
    """Wait for mom to connect to Slack."""
    print("Waiting for mom to connect to Slack...")

    deadline = time.time() + timeout

    while time.time() < deadline:
        if proc.poll() is not None:
            print("ERROR: Mom exited prematurely.", file=sys.stderr)
            _tail_file(STATE.mom_log, 30)
            return False

        try:
            with open(STATE.mom_log, "r") as f:
                content = f.read()
                if "Mom bot connected and listening" in content:
                    print("Mom is connected and listening.")
                    return True
        except FileNotFoundError:
            pass

        time.sleep(1)

    print("ERROR: Mom did not connect within 60s.", file=sys.stderr)
    return False


# ============================================================================
# Test message
# ============================================================================

def send_test_message(config: dict) -> float:
    """Send test message via mcporter. Returns the send timestamp."""
    print("")
    print("--- Step 4: Send test message ---")

    slack_mcp_url = f"http://127.0.0.1:{config['slack_mcp_server_port']}/mcp"
    payload = f"{config['mom_mention']} {config['test_message']}"
    print(f"Sending: {payload}")
    print(f"  to channel: {config['slack_channel']}")

    send_ts = time.time()

    subprocess.run(
        [
            "mcporter", "call",
            "--http-url", slack_mcp_url,
            "--allow-http",
            "--name", "slack-mcp",
            "conversations_add_message",
            f"channel_id={config['slack_channel']}",
            f"payload={payload}",
            "content_type=text/plain",
            "--output", "text"
        ],
        capture_output=True,
        check=True
    )

    print("Message sent.")
    return send_ts


# ============================================================================
# Poll for response
# ============================================================================

def poll_for_response(config: dict, send_ts: float) -> bool:
    """Poll conversation history for mom's response after send_ts."""
    print("")
    print("--- Step 5: Wait for mom's response ---")
    print(f"Polling every {config['poll_interval_seconds']}s (timeout: {config['timeout_seconds']}s)...")

    slack_mcp_url = f"http://127.0.0.1:{config['slack_mcp_server_port']}/mcp"
    deadline = time.time() + config["timeout_seconds"]
    attempt = 0

    while time.time() < deadline:
        attempt += 1

        # Check mom is still alive
        if STATE.mom_proc and STATE.mom_proc.poll() is not None:
            print("ERROR: Mom process died while waiting for response.", file=sys.stderr)
            return False

        try:
            result = subprocess.run(
                [
                    "mcporter", "call",
                    "--http-url", slack_mcp_url,
                    "--allow-http",
                    "--name", "slack-mcp",
                    "conversations_history",
                    f"channel_id={config['slack_channel']}",
                    "limit=20",
                    "--output", "text"
                ],
                capture_output=True,
                text=True,
                timeout=10
            )

            response_csv = result.stdout if result.returncode == 0 else ""

            if response_csv:
                # CSV columns: MsgID,UserID,UserName,RealName,Channel,ThreadTs,Text,...
                # Find bot messages posted after we sent our test message.
                # MsgID is a Slack timestamp (e.g. 1769568204.270149).
                bot_responses = _find_bot_responses(
                    response_csv,
                    config["mom_bot_user_id"],
                    send_ts
                )
                if bot_responses:
                    print("")
                    print("=== PASS ===")
                    print("Mom responded to the test message.")
                    print("")
                    print("Bot response line(s):")
                    for row in bot_responses:
                        print(",".join(row))
                    print("")
                    print("Recent conversation history:")
                    print("\n".join(response_csv.split("\n")[:30]))
                    return True

        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Print progress every 5 attempts
        if attempt % 5 == 0:
            remaining = int(deadline - time.time())
            print(f"  Still waiting... ({remaining}s remaining)")

        time.sleep(config["poll_interval_seconds"])

    return False


def _find_bot_responses(
    csv_content: str,
    bot_user_id: str,
    after_ts: float
) -> list[list[str]]:
    """Find bot messages posted after the given unix timestamp."""
    results = []
    try:
        reader = csv.reader(io.StringIO(csv_content))
        for row in reader:
            if len(row) < 2:
                continue
            # Skip header row
            if row[0] == "MsgID":
                continue
            # Column 0: MsgID (Slack timestamp), Column 1: UserID
            if row[1] != bot_user_id:
                continue
            try:
                msg_ts = float(row[0])
            except ValueError:
                continue
            if msg_ts > after_ts:
                results.append(row)
    except csv.Error:
        pass
    return results


def print_final_history(config: dict) -> None:
    """Print final conversation history on failure."""
    slack_mcp_url = f"http://127.0.0.1:{config['slack_mcp_server_port']}/mcp"
    try:
        result = subprocess.run(
            [
                "mcporter", "call",
                "--http-url", slack_mcp_url,
                "--allow-http",
                "--name", "slack-mcp",
                "conversations_history",
                f"channel_id={config['slack_channel']}",
                "limit=20",
                "--output", "text"
            ],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            print("\n".join(result.stdout.split("\n")[:30]))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


# ============================================================================
# Main
# ============================================================================

def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Mom Slack E2E Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --prompt "hello mom"
  %(prog)s --prompt "what is the weather?"
"""
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Custom prompt to send to mom (optional)"
    )
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    global CONFIG

    # Parse command line arguments
    args = parse_args()

    # Load .env files
    load_env_files()

    # Get configuration
    CONFIG = get_config(prompt=args.prompt)

    # Print header
    print("=== Mom Slack E2E Test ===")
    print(f"  Channel:    {CONFIG['slack_channel']}")
    print(f"  Message:    {CONFIG['test_message']}")
    print(f"  Timeout:    {CONFIG['timeout_seconds']}s")
    print(f"  Container:  {CONFIG['docker_container_name']}")
    print(f"  MCP port:   {CONFIG['slack_mcp_server_port']}")
    print("")

    # Validate
    validate_tools()
    validate_env_vars(CONFIG)

    # Setup temp workspace
    STATE.temp_workspace = setup_workspace()
    STATE.mom_log = STATE.temp_workspace / "mom.log"
    STATE.slack_mcp_log = STATE.temp_workspace / "slack-mcp-server.log"

    # Write settings if model is specified
    write_settings(STATE.temp_workspace, CONFIG)

    try:
        # Step 1: Create Docker container
        create_docker_container(CONFIG)

        # Step 2: Start slack-mcp-server
        STATE.slack_mcp_proc = start_slack_mcp_server(CONFIG)
        if not wait_for_slack_mcp_server(STATE.slack_mcp_proc, CONFIG["slack_mcp_server_port"]):
            STATE.exit_code = 1
            return STATE.exit_code

        # Step 3: Start mom
        STATE.mom_proc = start_mom(CONFIG)
        if not wait_for_mom(STATE.mom_proc):
            STATE.exit_code = 1
            return STATE.exit_code

        # Give mom a moment to finish backfill and stabilize
        time.sleep(2)

        # Step 4: Send test message
        send_ts = send_test_message(CONFIG)

        # Step 5: Poll for response
        if poll_for_response(CONFIG, send_ts):
            STATE.exit_code = 0
        else:
            print("")
            print("=== FAIL ===")
            print(f"Timed out after {CONFIG['timeout_seconds']}s waiting for mom's response.", file=sys.stderr)
            print("")
            print("Last conversation history:")
            print_final_history(CONFIG)
            STATE.exit_code = 1

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        STATE.exit_code = 1

    return STATE.exit_code


if __name__ == "__main__":
    # Register cleanup via atexit so it runs on normal exit AND sys.exit()
    atexit.register(cleanup)

    # Signal handlers just set exit code and call sys.exit, which triggers atexit
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    exit_code = main()
    sys.exit(exit_code)
