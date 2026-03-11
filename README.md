# Claude Code Sub-Agent Wrapper

Use **non-Anthropic models** (GLM-5 via z.ai) as sub-agents in Claude Code, with the **exact same capabilities** as native Task tool sub-agents.

**v2.2.1**: Added `--show-prompt` flag to display full prompt before execution (like native Task tool UI).

**v2.2.0**: Now uses **inactivity-based timeout** (heartbeat pattern) — long tasks run indefinitely if active; stalled tasks are detected and killed quickly.

## Why?

Claude Code's native Task tool spawns sub-agents, but:
- Sub-agents are **Anthropic models only** (expensive per-token pricing)
- No way to use cheaper alternatives like GLM-5

This wrapper solves both problems by spawning `claude -p` (headless mode) with z.ai environment variables, giving you:
- **ALL native Claude Code tools** (Read, Write, Edit, Glob, Grep, Bash, WebFetch, etc.)
- **GLM-5** as the model ($30/mo unlimited via z.ai)
- **Same behavior** as native sub-agents

## Quick Start

### 1. Install Claude Code CLI
```bash
npm install -g @anthropic-ai/claude-code
```

### 2.(OPTIONAL) Get z.ai API Key (or equivalent)
Subscribe to [GLM Coding Plan](https://z.ai/subscribe) ($30/mo unlimited)

### 3. Set Environment Variable
```bash
export ZAI_API_KEY="your-api-key-here"
```

### 4. Run
```bash
python subagent_template.py --task "Fix the bug in auth.py" --cwd /path/to/project --stream
```

## Files

| File | Description |
|------|-------------|
| `subagent_template.py` | **Use this** - Template |
| `README.md` | Documentation |
| `LICENSE` | MIT License |

## Usage

### Basic
```bash
python subagent_template.py --task "Your task description" --cwd /path/to/project
```

### With Progress Streaming (Recommended)
```bash
python subagent_template.py --task "Implement feature X" --cwd /path/to/project --stream
```

### All Options
```
--task               Task description (required)
--cwd                Working directory (default: current)
--inactivity-timeout Kill if no tool use for N seconds (default: 90)
--max-timeout        Optional hard ceiling in seconds (default: unlimited)
--stream             Show tool names as they execute
--show-prompt        Display full prompt before execution (like native Task UI)
--max-budget         Max cost in USD
--allowed-tools      Comma-separated list of allowed tools
--debug              Write debug logs to /tmp/glm-subagent-debug.log
```

### Timeout Behavior (v2.2.0+)

Uses **inactivity-based timeout** instead of global timeout:

| Scenario | Behavior |
|----------|----------|
| Agent actively using tools | Timer resets on each tool use → runs indefinitely |
| Agent stalls (no tool use) | Killed after `--inactivity-timeout` seconds |
| Very long task | Use `--max-timeout` as safety ceiling |

This follows the [heartbeat pattern](https://docs.temporal.io/encyclopedia/detecting-activity-failures) recommended by Temporal and AWS for agentic workflows.

## Output Format

The wrapper returns clean JSON that your orchestrator can parse:

```json
{"success": true, "result": "Task completed successfully...", "error": null}
```

### With `--stream` flag, you'll see:
```
[subagent] a1b2c3d4e5 starting cwd=/path/to/project
[subagent] task: Implement feature X...
[subagent] a1b2c3d4e5 🔧 Glob
[subagent] a1b2c3d4e5 🔧 Read
[subagent] a1b2c3d4e5 🔧 Edit
[subagent] a1b2c3d4e5 ✅ complete
[subagent] a1b2c3d4e5 ✅ success
[subagent] result: Implemented feature X by...
[subagent] logs: /tmp/glm-native-subagent/run_a1b2c3d4e5.stream.jsonl
{"success": true, "result": "Implemented feature X by...", "error": null}
```

### With `--show-prompt` flag (like native Task tool UI):
```
[subagent] a1b2c3d4e5 starting cwd=/path/to/project
└ Prompt:                              ← Gray colored label
    TASK: Update roleProvisioning.service.ts

    You need to update the role provisioning configuration
    so that Sales Managers automatically get proper permissions.

    File Location:
    /Users/Gabe/Dev/project/src/services/roleProvisioning.service.ts

[subagent] a1b2c3d4e5 🔧 Read
[subagent] a1b2c3d4e5 🔧 Edit
...
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Your Claude Code Session (Orchestrator)                        │
│                                                                 │
│  "Implement the EmailInbox component"                          │
│                                                                 │
│  [Bash] python subagent_template.py --task "..." --stream    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  subagent_template.py                                         │
│                                                                 │
│  1. Sets ANTHROPIC_BASE_URL to z.ai endpoint                   │
│  2. Spawns: claude -p --output-format stream-json              │
│  3. Closes stdin immediately (prevents hang)                    │
│  4. Streams tool names to stdout (token-efficient)              │
│  5. Writes full stream to log file (debuggability)              │
│  6. Returns JSON result                                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Claude Code CLI (GLM-5 via z.ai)                             │
│                                                                 │
│  - Fresh 200k token context window                              │
│  - ALL native tools available                                   │
│  - Permissions inherited (--dangerously-skip-permissions)       │
│  - Session not persisted (isolation)                            │
└─────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### Problem: Bash Tool Truncation

When Claude Code calls a Bash command, output is captured and has limits:
- UI collapses long output
- Hard size limit (~30k chars) causes silent truncation

**Old approach (broken):**
```
stream-json events → stdout → Bash captures → TRUNCATED!
```

**New approach (fixed):**
```
stream-json events → log FILE (full fidelity)
tool names only    → stdout (tiny, ~10 lines)
final JSON         → stdout (always present)
```

### Problem: Spinner Spam

In-place spinner updates (`\r`) don't work when output is captured by Bash tool. Each update becomes a new line → hundreds of lines.

**Fix:** Detect if stdout is a tty. Skip spinner if being captured:
```python
if not sys.stdout.isatty():
    return  # Skip spinner
```

### Problem: Token Inefficiency

Streaming all events to stdout bloats the orchestrator's context.

**Fix:** Only print high-signal information:
- Task start/end
- Tool names (deduplicated)
- Result preview (truncated to 200 chars)
- Pointer to full logs

## Log Files

Full output is always saved to `/tmp/glm-native-subagent/`:

```
run_{id}.stream.jsonl   # All stream-json events (when --stream)
run_{id}.stdout.txt     # Raw stdout from subprocess
run_{id}.stderr.txt     # Raw stderr from subprocess
```

## Use from Claude Code

Add this to your `CLAUDE.md`:

```markdown
# Use GLM Sub-Agents (Not Native Task Tool)

For sub-agent work, use GLM wrapper via Bash:
\`\`\`bash
python /path/to/subagent_template.py --task "task" --cwd "$(pwd)" --stream
\`\`\`
Returns JSON: `{"success": bool, "result": "...", "error": null}`. Logs at `/tmp/glm-native-subagent/`.
```

## Comparison: This vs Native Task Tool

| Feature | Native Task Tool | This Wrapper |
|---------|-----------------|--------------|
| Model | Anthropic only | Any (GLM-5 via z.ai) |
| Cost | Per-token ($$$) | Flat rate ($30/mo) |
| Tools | All native | All native |
| Context isolation | ✅ | ✅ |
| Progress UI | Native panel | Terminal output |
| Truncation risk | None | None (fixed in v2.1) |
| Timeout | Global only | Inactivity-based (v2.2) |

## Comparison: This vs MCP Approach

| Feature | MCP Plugin | This Wrapper |
|---------|-----------|--------------|
| Context cost | Schema in every prompt | Zero |
| Hot refresh | Requires restart | Instant |
| Build step | npm build | None |
| Complexity | TypeScript, plugin install | Single Python file |

## Troubleshooting

### "Missing API token"
Set `ZAI_API_KEY` or `ANTHROPIC_AUTH_TOKEN` environment variable.

### Process hangs
The wrapper closes stdin immediately. If you're modifying the code, ensure:
```python
proc.stdin.close()  # CRITICAL
```

### Timeout
Default is 120s. Increase with `--timeout 300`.

### "Claude CLI not found"
Install: `npm install -g @anthropic-ai/claude-code`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ZAI_API_KEY` | Yes* | z.ai API key |
| `ANTHROPIC_AUTH_TOKEN` | Yes* | Alternative to ZAI_API_KEY |
| `ZAI_BASE_URL` | No | Custom API endpoint |
| `ANTHROPIC_BASE_URL` | No | Alternative to ZAI_BASE_URL |

*One of `ZAI_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is required.

## z.ai Model Mapping

| Requested | Actual |
|-----------|--------|
| claude-opus | GLM-5 |
| claude-sonnet | GLM-5 |
| claude-haiku | GLM-5-Air |

## License

MIT
