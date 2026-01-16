#!/usr/bin/env python3
# =============================================================================
# Claude Code Sub-Agent Wrapper Template
# Version: v2.1.1
#
# Use non-Anthropic models as sub-agents in Claude Code with ALL native tools.
# This template uses environment variables - set your API key before use.
#
# Setup:
#   export ZAI_API_KEY="your-api-key-here"  # Get from https://z.ai/subscribe
#
# Usage:
#   python subagent_template.py --task "Your task" --cwd /path --stream
# =============================================================================
"""
Claude Code Sub-Agent Wrapper

Spawns Claude Code CLI with custom API backend (z.ai, OpenRouter, etc.),
giving ALL native Claude Code tools but using your preferred model.

Key features:
- Native-ish UI: spinner (in terminal) + tool names
- Token efficient: only tool names + result preview to stdout
- No truncation: full stream saved to log files
- Full debuggability: logs at /tmp/glm-native-subagent/

Environment variables (set before running):
- ANTHROPIC_AUTH_TOKEN or ZAI_API_KEY (required) - Your API key
- ANTHROPIC_BASE_URL or ZAI_BASE_URL (optional) - API endpoint
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Optional, Dict, Any, List


# =============================================================================
# CONFIGURATION - Modify these for your API provider
# =============================================================================
DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"  # z.ai endpoint
DEFAULT_API_TIMEOUT_MS = "300000"  # 5 min timeout
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _safe_truncate(s: str, n: int = 250) -> str:
    if not s:
        return ""
    return s[:n] + ("..." if len(s) > n else "")


def _build_env() -> Dict[str, str]:
    """Build environment with API credentials from env vars."""
    env = os.environ.copy()

    # Check for API token (required)
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ZAI_API_KEY")
    if not token:
        raise ValueError(
            "Missing API token!\n"
            "Set one of these environment variables:\n"
            "  export ZAI_API_KEY='your-key-here'\n"
            "  export ANTHROPIC_AUTH_TOKEN='your-key-here'\n\n"
            "Get your API key from: https://z.ai/subscribe"
        )

    env["ANTHROPIC_AUTH_TOKEN"] = token
    env["ANTHROPIC_BASE_URL"] = (
        env.get("ANTHROPIC_BASE_URL") or
        env.get("ZAI_BASE_URL") or
        DEFAULT_BASE_URL
    )
    env.setdefault("API_TIMEOUT_MS", DEFAULT_API_TIMEOUT_MS)
    return env


def _kill_process_group(proc: subprocess.Popen, grace_seconds: int = 5) -> None:
    """Kill process group: SIGTERM first, then SIGKILL."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_subagent(
    task: str,
    working_dir: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    timeout: int = 600,
    skip_permissions: bool = True,
    stream_progress: bool = False,
    max_budget_usd: Optional[float] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """
    Run Claude Code CLI as a sub-agent with custom API backend.

    Args:
        task: Task description
        working_dir: Working directory (default: current)
        allowed_tools: Comma-separated allowed tools
        timeout: Timeout in seconds (default: 600)
        skip_permissions: Skip permission prompts (default: True)
        stream_progress: Show tool names as they execute
        max_budget_usd: Max cost ceiling
        debug: Write debug logs

    Returns:
        Dict with: success, result, session_id, error, artifacts
    """
    env = _build_env()
    cwd = working_dir or os.getcwd()
    run_id = uuid.uuid4().hex[:10]

    # Log file paths
    artifacts_dir = os.path.join("/tmp", "glm-native-subagent")
    os.makedirs(artifacts_dir, exist_ok=True)

    stream_log = os.path.join(artifacts_dir, f"run_{run_id}.stream.jsonl") if stream_progress else None
    stdout_log = os.path.join(artifacts_dir, f"run_{run_id}.stdout.txt")
    stderr_log = os.path.join(artifacts_dir, f"run_{run_id}.stderr.txt")

    # Build command
    output_format = "stream-json" if stream_progress else "json"
    cmd: List[str] = ["claude", "-p", task, "--output-format", output_format]

    if stream_progress:
        cmd.append("--verbose")

    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    elif allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])

    # Sub-agent behavior prompt
    subagent_prompt = """You are a coding sub-agent. Complete the given task efficiently.
Guidelines:
- Read existing files before modifying them
- Use Edit tool for surgical changes to existing files
- Use Write tool only for new files
- Follow existing project conventions
- When done, provide a clear summary of what you accomplished"""
    cmd.extend(["--append-system-prompt", subagent_prompt])
    cmd.append("--no-session-persistence")

    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])

    # Debug logging
    debug_log = None
    if debug:
        debug_log = open("/tmp/glm-subagent-debug.log", "a", encoding="utf-8")
        debug_log.write(f"\n{'='*80}\nrun_id={run_id}\ncwd={cwd}\ncmd={' '.join(cmd)}\n")
        debug_log.flush()

    # Print header (small)
    print(f"[subagent] {run_id} starting cwd={cwd}", flush=True)
    print(f"[subagent] task: {_safe_truncate(task, 120)}", flush=True)

    result: Dict[str, Any] = {
        "success": False,
        "result": "",
        "session_id": None,
        "error": None,
        "artifacts": {
            "run_id": run_id,
            "stream_log": stream_log,
            "stdout_log": stdout_log,
            "stderr_log": stderr_log
        },
    }

    final_result_event: Optional[Dict[str, Any]] = None
    stop_spinner = threading.Event()
    last_tool_name_printed = None

    def spinner_loop():
        # Skip spinner if stdout is not a real terminal
        if not sys.stdout.isatty():
            return
        i = 0
        while not stop_spinner.is_set():
            frame = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
            i += 1
            sys.stdout.write(f"\r[subagent] {run_id} running {frame}")
            sys.stdout.flush()
            time.sleep(0.2)
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            bufsize=1,
        )

        # CRITICAL: Close stdin immediately to prevent hang
        if proc.stdin:
            proc.stdin.close()

        # Open log files
        stdout_f = open(stdout_log, "w", encoding="utf-8")
        stderr_f = open(stderr_log, "w", encoding="utf-8")
        stream_f = open(stream_log, "w", encoding="utf-8") if stream_log else None

        spinner_thread = threading.Thread(target=spinner_loop, daemon=True)
        spinner_thread.start()

        def read_stdout():
            nonlocal final_result_event, last_tool_name_printed
            assert proc.stdout is not None
            for line in proc.stdout:
                # Write to log files (full fidelity)
                stdout_f.write(line)
                stdout_f.flush()
                if stream_progress and stream_f:
                    stream_f.write(line)
                    stream_f.flush()

                # Parse stream events for progress display
                if stream_progress:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    try:
                        event = json.loads(line_stripped)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type", "")
                    if etype == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_name = block.get("name") or "tool"
                                # Only print unique tool names (deduplication)
                                if tool_name != last_tool_name_printed:
                                    last_tool_name_printed = tool_name
                                    sys.stdout.write(f"\n[subagent] {run_id} 🔧 {tool_name}\n")
                                    sys.stdout.flush()
                    elif etype == "result":
                        final_result_event = event
                        sys.stdout.write(f"\n[subagent] {run_id} ✅ complete\n")
                        sys.stdout.flush()

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()

        # Wait with timeout
        start = time.time()
        while proc.poll() is None:
            if (time.time() - start) > timeout:
                _kill_process_group(proc)
                result["error"] = f"Timeout after {timeout}s"
                break
            time.sleep(0.1)

        # Capture stderr
        if proc.stderr:
            stderr_text = proc.stderr.read()
            if stderr_text:
                stderr_f.write(stderr_text)
                stderr_f.flush()

        # Cleanup
        stop_spinner.set()
        reader.join(timeout=2)
        spinner_thread.join(timeout=1)
        stdout_f.close()
        stderr_f.close()
        if stream_f:
            stream_f.close()

        if result["error"]:
            return result

        # Parse result
        if proc.returncode == 0:
            if stream_progress:
                if final_result_event:
                    result["success"] = True
                    result["result"] = final_result_event.get("result", "") or ""
                    result["session_id"] = final_result_event.get("session_id")
                else:
                    # Fallback: re-parse log file
                    last_result = None
                    if stream_log and os.path.exists(stream_log):
                        with open(stream_log, "r", encoding="utf-8") as f:
                            for ln in f:
                                try:
                                    ev = json.loads(ln.strip())
                                    if ev.get("type") == "result":
                                        last_result = ev
                                except:
                                    continue
                    if last_result:
                        result["success"] = True
                        result["result"] = last_result.get("result", "") or ""
                        result["session_id"] = last_result.get("session_id")
                    else:
                        result["success"] = True
                        result["result"] = f"(no result event; see {stream_log})"
            else:
                # Non-streaming: parse stdout as JSON
                with open(stdout_log, "r", encoding="utf-8") as f:
                    stdout_text = f.read()
                try:
                    output = json.loads(stdout_text)
                    result["success"] = True
                    result["result"] = output.get("result", "") or ""
                    result["session_id"] = output.get("session_id")
                except json.JSONDecodeError:
                    result["success"] = True
                    result["result"] = stdout_text
        else:
            err_text = ""
            if os.path.exists(stderr_log):
                with open(stderr_log, "r", encoding="utf-8") as f:
                    err_text = f.read()
            result["error"] = (err_text.strip() or f"Exit code: {proc.returncode}").strip()

    except FileNotFoundError:
        result["error"] = "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
    except Exception as e:
        result["error"] = str(e)
    finally:
        if debug_log:
            debug_log.write(f"success={result.get('success')} error={result.get('error')}\n")
            debug_log.close()

    # Print final summary (small)
    if result["success"]:
        print(f"[subagent] {run_id} ✅ success", flush=True)
        print(f"[subagent] result: {_safe_truncate(result.get('result',''), 200)}", flush=True)
    else:
        print(f"[subagent] {run_id} ❌ error={result.get('error')}", flush=True)

    if stream_log:
        print(f"[subagent] logs: {stream_log}", flush=True)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Sub-Agent Wrapper",
        epilog="Set ZAI_API_KEY environment variable before running."
    )
    parser.add_argument("--task", required=True, help="Task description")
    parser.add_argument("--cwd", help="Working directory")
    parser.add_argument("--allowed-tools", help="Comma-separated allowed tools")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout seconds (default: 120)")
    parser.add_argument("--require-permissions", action="store_true", help="Require permission prompts")
    parser.add_argument("--stream", action="store_true", help="Show tool names as they execute")
    parser.add_argument("--max-budget", type=float, help="Max cost USD")
    parser.add_argument("--debug", action="store_true", help="Debug logs to /tmp/glm-subagent-debug.log")
    args = parser.parse_args()

    result = run_subagent(
        task=args.task,
        working_dir=args.cwd,
        allowed_tools=args.allowed_tools,
        timeout=args.timeout,
        skip_permissions=not args.require_permissions,
        stream_progress=args.stream,
        max_budget_usd=args.max_budget,
        debug=args.debug,
    )

    # Print JSON for orchestrator
    print(json.dumps({"success": result["success"], "result": result["result"], "error": result["error"]}))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
