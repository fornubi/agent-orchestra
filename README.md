# agent-orchestra

A single-file, stdlib-only Python 3 CLI that lets one agent (or a person) send prompts to other coding-agent CLIs non-interactively, then reports what actually happened.

## Why It Exists

Do not trust an agent's claim of success.

Autonomous coding agents regularly produce convincing explanations: *"I have investigated the issue, updated the module, and resolved the problem."* In practice, an agent run can fail in several silent ways:

- The process exits with code `0` while modifying zero files due to tool permission restrictions, execution mode constraints, or sandbox limitations.
- The agent hits an API quota, rate limit, or billing cutoff midway through and terminates without writing changes.
- The agent hallucinates that edits were saved without issuing any file operations.
- The agent leaves the repository in a broken state that fails compilation, syntax checks, or unit tests.

`orchestra` supervises the run as an impartial auditor. Before the agent runs, `orchestra` creates an in-memory SHA-1 snapshot of your working directory. After the agent finishes, it takes a second snapshot, computes the exact filesystem diff (created, modified, deleted), runs optional verification commands, writes an audit log, and maps silent failures or broken builds to explicit, scriptable exit codes.

## Requirements

- **Python 3.10+**: `orchestra.py` uses modern union type syntax (`str | None`) and relies exclusively on the Python standard library. No third-party packages or virtual environments are needed.
- **Provider CLIs**: The target coding agent CLI must be installed, authenticated, and available in your `PATH`:
  - `claude` (Claude Code)
  - `agy` (Google Antigravity CLI)
  - `pi` (Pi agent harness)
  - `opencode` (OpenCode)

> Note: `freebuff` is **not** supported because it operates solely as an interactive TUI.

## Install

Clone the repository directly. There are no dependencies to install:

```bash
git clone https://github.com/fornubi/agent-orchestra.git
cd agent-orchestra
```

Examples use `python`. Where that command is missing, as on many macOS and Linux systems, use `python3`.

Run `orchestra.py` directly using Python 3:

```bash
python orchestra.py --help
```

On Unix systems, you can also make it executable:

```bash
chmod +x orchestra.py
./orchestra.py --help
```

### Agent Skill

To let your coding agent delegate through `orchestra`, install the included skill from your clone. It uses the open [Agent Skills](https://agentskills.io) format:

```bash
python install.py
```

| Installed file | Used by | When |
|---|---|---|
| `~/.claude/skills/agent-orchestra/SKILL.md` | Claude Code, OpenCode | Always |
| `~/.agents/skills/agent-orchestra/SKILL.md` | Codex, Gemini CLI, OpenCode | Always |
| `~/.config/opencode/agents/orchestra.md` | `orchestra.py` when delegating to `opencode` | Only if `opencode` is on `PATH` |

Both skill copies are generated from `skills/agent-orchestra/SKILL.md` with the path to your clone and the right Python command filled in. If `XDG_CONFIG_HOME` is set, the OpenCode agent goes there instead of `~/.config`. OpenCode sees both, logs a duplicate-name warning, and uses one; the copies are identical, so this is harmless.

Then ask your agent to delegate, for example: *"Use agent-orchestra to have pi build this, then review it."* The calling agent only needs to support skills and run shell commands. Delegation targets are `claude`, `agy`, `pi`, and `opencode`, minus the [calling agent](#no-self-delegation).

`orchestra.py` stays in your clone, so `git pull` updates it. An installed file that differs from the repository is left alone and the script exits `1`. Re-run with `--force` to overwrite it, which you also need after moving the clone or when the skill changes.

## Quick Start

### Basic Prompt Delegation

Send a prompt directly to `agy`:

```bash
python orchestra.py agy "Fix the typo in the error response in src/server.py"
```

### Build and Verify with Change Enforcement

Run `pi` against a target repository, enforcing that files must change and that verification tests pass:

```bash
python orchestra.py pi "Add unit tests for token validation" \
  --workdir ../my-api \
  --expect-changes \
  --verify "npm test"
```

### Multi-Line Tasks via Prompt File

Pass structured tasks from a file to avoid shell quoting and newline truncation:

```bash
python orchestra.py opencode --prompt-file tasks/refactor-auth.md \
  --workdir ../my-service \
  --expect-changes \
  --verify "pytest tests/test_auth.py"
```

## Provider Flags

### Common Flags

These flags are available across all providers:

| Flag | Type | Description |
|---|---|---|
| `provider` | Positional | Target agent CLI: `claude`, `agy`, `pi`, or `opencode`; or `providers` to list the agents you can delegate to. |
| `prompt` | Positional | The task prompt (optional if `--prompt-file` is specified). |
| `--prompt-file <path>` | String | Read the prompt from this file. Avoids shell quoting and newline limits. |
| `--workdir <path>` | String | Directory where the agent runs and whose file changes are tracked (default: current directory). |
| `--expect-changes` | Flag | Fail with exit code `3` if the agent exits without changing any files. Recommended for build and edit tasks. |
| `--verify "<cmd>"` | String | Shell command executed in `--workdir` after the agent finishes. Exits with code `4` if this command returns non-zero. |
| `--timeout <sec>` | Integer | Maximum execution time in seconds (default: `600`). Kills the agent and its child processes on timeout. |
| `--ignore <glob>` | String | Extra glob pattern (`fnmatch`, relative to `--workdir` with forward slashes) to exclude from change tracking. Repeatable. |
| `--caller <name>` | String | Name of the calling agent, replacing detection from the environment. See [No Self-Delegation](#no-self-delegation). |
| `--model <id>` | String | Model identifier passed to the underlying CLI (for `opencode`, uses `provider/model`). |

### Provider-Specific Flags

#### Claude Code (`claude`)

| Flag | Values | Description |
|---|---|---|
| `--effort <level>` | `low`, `medium`, `high`, `xhigh`, `max` | Reasoning effort level. |
| `--exec-mode <mode>` | `accept-edits`, `auto`, `plan` | Permission mode (default: `accept-edits`, which edits files but runs no shell commands, so use `--verify` for tests). |
| `--output <format>` | `text`, `json` | Output formatting (passed to `claude --output-format`). |

#### Google Antigravity (`agy`)

| Flag | Values | Description |
|---|---|---|
| `--effort <level>` | `low`, `medium`, `high`, `xhigh`, `max` | Reasoning effort level. |
| `--exec-mode <mode>` | `accept-edits`, `auto`, `plan` | Execution mode (passed to `agy --mode`). |
| `--dangerous` | Flag | Auto-approves all tool permissions (passes `--dangerously-skip-permissions` to `agy`). |

#### Pi (`pi`)

| Flag | Values | Description |
|---|---|---|
| `--provider <name>` | String | Provider name for `--model`. |
| `--output <format>` | `text`, `json` | Output formatting mode (passed to `pi --mode`). |
| `--approve` | Flag | Automatically trusts project-local files. |
| `--no-session` | Flag | Runs ephemerally without saving session state to disk. |

#### OpenCode (`opencode`)

| Flag | Values | Description |
|---|---|---|
| `--agent <name>` | String | Agent to invoke (default: `orchestra`). |
| `--output <format>` | `text`, `json` | Output formatting (passes `--format json` when `json` is chosen). |
| `--auto` | Flag | Automatically approves permissions not explicitly denied. |

## Exit Codes

`orchestra` turns ambiguous agent states into deterministic, scriptable exit codes:

| Code | Status | Trigger Condition |
|:---:|---|---|
| `0` | Success | Agent completed successfully (and `--verify` passed, if provided). |
| `N` | Agent Error | The agent CLI returned its own non-zero exit code `N`. |
| `3` | Silent Failure | Agent exited `0` with no console output and no file changes, OR `--expect-changes` was set and nothing changed. |
| `4` | Verification Failed | The post-run `--verify "<cmd>"` command failed (non-zero exit code or verification timeout). |
| `5` | Quota / Auth Limit | Output matched a known quota, billing, rate-limit, or authentication error phrase with no file changes. |
| `6` | Refused | The provider is the calling agent or already in the delegation chain. The agent was not started. |
| `124` | Timeout | Execution exceeded `--timeout` seconds. The agent and its child processes were killed. |

> **Note on Quota Detection**: If a quota or billing phrase appears in the agent's output but files *did* change on disk, `orchestra` prints a warning to `stderr` and allows normal exit code evaluation to continue.

## No Self-Delegation

An agent can never delegate to itself. From Claude Code, `claude` is not an option; from `agy`, `agy` is not an option; and so on:

```console
$ python orchestra.py providers
available: agy, pi, opencode
excluded (calling agent or delegation chain): claude
```

Running an excluded provider exits `6` without starting it. Two sources decide what is excluded:

| Source | How it works |
|---|---|
| Calling agent | Detected from the variable each CLI sets for its commands: `CLAUDECODE`, `ANTIGRAVITY_AGENT`, `PI_CODING_AGENT`, or `OPENCODE`. |
| Delegation chain | `orchestra` passes every agent in the delegation down in `ORCHESTRA_CHAIN`. If `claude` delegates to `pi`, that `pi` cannot delegate to `claude` or `pi`, which also rules out loops. |

If detection is wrong, for example because a variable leaked from a parent terminal, pass `--caller <name>` to replace it. The chain still applies. This guards against mistakes, not against someone deliberately bypassing it.

## Change Report and Logs

### Filesystem Snapshots

`orchestra` creates an in-memory SHA-1 snapshot of the target `--workdir` immediately before and after agent execution.

- **Tracked Categories**: Reports files `created`, `modified`, and `deleted`.
- **Large Files**: Files larger than 50 MB are tracked by file size rather than SHA-1 hashing.
- **Large Trees**: If `--workdir` contains more than 20,000 files, snapshotting is skipped, and the report displays `file changes: not tracked (workdir too large)`.
- **Excluded Directories**: VCS, dependencies, caches, and agent state directories are automatically skipped: `.git`, `.hg`, `.svn`, `node_modules`, `__pycache__`, `.venv`, `venv`, `.pi`, `.pi-lens-probe-home`, `.opencode`, `.playwright-mcp`, `.next`, `dist`, `build`, `.cache`, `.wrangler`, `.turbo`, `.vite`, `.parcel-cache`, `.ruff_cache`, `.pytest_cache`, `.mypy_cache`, and `coverage`.
- **Excluded Suffixes**: Files ending in `.tsbuildinfo`, `-wal`, `-shm`, and `.log` are excluded from snapshots.

### Report Output

At the end of every run, `orchestra` outputs a change summary:

```
--- orchestra report ---
workdir: /path/to/project
log: /path/to/agent-orchestra/state/logs/20261030-140330-pi.log
  created  src/app.js
  modified README.md
```

If no changes occurred:

```
--- orchestra report ---
workdir: /path/to/project
log: /path/to/agent-orchestra/state/logs/20261030-140330-pi.log
file changes: NONE
```

### Run Logs

Each run writes a persistent log to `state/logs/<YYYYmmdd-HHMMSS>-<provider>.log` relative to `orchestra.py`. Runs of the same provider started in the same second get a numeric suffix (`-1`, `-2`, ...), so parallel runs keep separate logs. The log records:

- Full command line invocation (`argv`)
- Target working directory (`workdir`)
- Whether the prompt was attached as a file
- The complete prompt text
- Final exit code and duration in seconds
- File change lists (`created`, `modified`, `deleted`)
- The trailing 200 lines of combined console output

`orchestra` retains the newest 50 log files and automatically purges older logs.

## Build and Review Workflow

When integrating coding agents into automated workflows, shell scripts, or supervisory agents, adopt the delegate-verify-review pattern:

1. **Delegate with Constraints**: Always pass `--expect-changes` and `--verify "<test-command>"` for code modification tasks:
   ```bash
   python orchestra.py pi --prompt-file task.txt \
     --workdir ./project \
     --expect-changes \
     --verify "npm test"
   ```
2. **Trust the Report and Exit Code**:
   - Check the exit code: if `0`, the agent exited cleanly, files were modified, and the verification command succeeded.
   - If non-zero (`3`, `4`, `5`, or `124`), inspect `stderr` and the log file indicated in the report.
3. **Review Changed Files**:
   - Inspect the file list printed under `--- orchestra report ---`.
   - Run `git diff` against those specific paths to inspect code quality and correctness before committing.

## Caveats

- **OpenCode Agent Requirement**: When using `opencode`, `orchestra` defaults to `--agent orchestra`. This agent must exist at `~/.config/opencode/agents/orchestra.md`. `python install.py` puts it there from `opencode/agents/orchestra.md`. It allows reading and editing files inside `--workdir` but no shell, so use `--verify` to run tests.
- **Antigravity (`agy`) `accept-edits` Mode**: `agy --exec-mode accept-edits` has been observed to do nothing when a task needs new files created.
- **Python Version**: Python 3.10+ is required due to modern union type annotations (`str | None`).
- **Windows Multi-Line Prompts**: On Windows, `cmd.exe` truncates command arguments at the first newline character. When invoking `pi` or `opencode` with multi-line prompts on Windows (`claude` receives its prompt on stdin instead), `orchestra` automatically writes the prompt to a temporary file and attaches it via `--file` or `@file`.
- **Process Tree Cleanup**: On Windows, child processes spawned by agent wrappers can keep standard I/O pipes open after a timeout. On timeout, `orchestra` executes `taskkill /PID <pid> /T /F` to terminate the entire process tree. On macOS and Linux, the agent runs in its own process group and that whole group is killed.

## License

MIT License. Copyright (c) 2026 A Kurniawan. See [LICENSE](LICENSE) for the full text.
Repository: [https://github.com/fornubi/agent-orchestra](https://github.com/fornubi/agent-orchestra)
