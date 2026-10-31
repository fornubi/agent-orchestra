---
name: agent-orchestra
description: Bridge to external coding agents claude (Claude Code), agy (Google Antigravity CLI), pi (Pi agent harness) and opencode (OpenCode). Use when the user asks you to delegate work to other agents/providers, or to do a build + review loop where another agent builds and you review. Invokes orchestra.py. freebuff is interactive-only and cannot be delegated to.
---

# Agent Orchestra — claude, agy, pi & opencode bridge

Send prompts to external CLI agents from any agent that can run shell commands. All run non-interactively and return stdout as the result.

**Never delegate to yourself.** Before delegating, run `python orchestra.py providers` and choose only from `available`. Never offer the user an agent it leaves out. The calling agent and every agent earlier in the delegation chain are excluded, and running one exits 6. If none is available, do the work yourself and say why. Don't use `--caller` to get around an exclusion; it exists for when detection names the wrong agent.

## Script

`{{ORCHESTRA_PY}}` (Python 3.10+, stdlib only). The commands below shorten this to `orchestra.py`; use the full path unless the current directory is the repository.

## Usage

```
python orchestra.py providers
python orchestra.py claude "<prompt>" [--model <name>] [--effort high] [--exec-mode plan] [--output json]
python orchestra.py agy "<prompt>" [--exec-mode auto] [--effort high] [--dangerous]
python orchestra.py pi  "<prompt>" [--model <pattern>] [--output json] [--approve] [--no-session]
python orchestra.py opencode "<prompt>" [--model provider/model] [--agent <name>] [--output json]

# common to all
  --prompt-file <path>   read the prompt from a file (prefer this for long prompts)
  --workdir <dir>        where the agent runs and whose file changes are reported (default: cwd)
  --expect-changes       exit 3 if the agent changed no files (use for every build/edit task)
  --verify "<cmd>"       run a check in --workdir after the agent; exit 4 if it fails
  --timeout <sec>        default 600
  --ignore <glob>        extra glob (fnmatch, workdir-relative with forward slashes) to
                         exclude from the change report; repeatable
```

- **claude**: Claude Code, via `claude -p` with the prompt on stdin. Runs with `--permission-mode acceptEdits` unless `--exec-mode` says otherwise, so it can edit files but can't run shell commands itself; use `--verify`.
- **agy**: Google Antigravity CLI. `-p` print mode. Default mode can already create and edit files, so **don't pass `--dangerous`**. `--effort` low|medium|high|xhigh|max. Observed: `--exec-mode accept-edits` can edit existing files but silently does nothing when the task needs new files/directories.
- **pi**: Pi agent harness. Uses its default dynamic model; only pass `--model`/`--provider` when a specific model is required. `--no-session` keeps runs ephemeral. File writes work with `--approve`. Observed: may ignore structural requirements in the spec and still report success, so always verify.
- **opencode**: OpenCode, via `opencode run`. Uses its configured default model; `--model` takes `provider/model`. The bridge runs the `orchestra` agent by default (`~/.config/opencode/agents/orchestra.md`, installed by `install.py`): deny-by-default, allows read/search and file edits inside `--workdir` only, **no shell** (so it can't run tests itself; use `--verify`). Don't pass `--auto`, and don't pick another `--agent` for build tasks: an agent whose permissions resolve to "ask" has every write rejected in a non-interactive run. `--output json` maps to `--format json`.
- **freebuff**: **not supported** — it is an interactive TUI only, with no prompt argument or headless mode. Don't try to delegate to it; tell the user to run it themselves in a terminal if they want it.

Claude Code only: its auto-mode classifier blocks `--dangerous` (as "Create Unsafe Agents", even with a Bash allow rule) and opencode `--auto`, but allows pi `--approve`.

## Output & exit codes

Agent stdout is printed, followed by a `--- orchestra report ---` block listing files `created` / `modified` / `deleted` in `--workdir` (or `file changes: NONE`), the log file path (`log: <path>`), then the `--verify` result if given.

| exit | meaning |
|------|---------|
| 0 | agent ok (and verify passed) |
| N | agent's own error code |
| 3 | silent failure (exit 0, no output, no changes) or `--expect-changes` with no changes |
| 4 | `--verify` failed |
| 5 | agent hit a quota/billing/auth limit (usage/rate/quota, insufficient credits/billing, auth/401/invalid key/not logged in) with **no file changes** — switch agent/provider or top up |
| 6 | refused: the provider is the calling agent or already in the delegation chain — choose from `providers` |
| 124 | timeout |

If a quota/billing/auth pattern is detected **and** files did change, a warning is printed but normal exit logic runs (the work may be partially done). State/agent dirs `.wrangler`, `.turbo`, `.vite`, `.parcel-cache`, `.ruff_cache`, `.pytest_cache`, `.mypy_cache`, `coverage` and file suffixes `.tsbuildinfo`, `-wal`, `-shm`, `.log` are never counted as changes; use `--ignore` for extra project-specific excludes.

Each run writes a log under `{{LOGS_DIR}}` as `<YYYYmmdd-HHMMSS>-<provider>.log` (final argv, workdir, prompt-attached flag/path, full prompt, exit code, duration, change report, last 200 lines of output). The 50 newest logs are kept.

## Workflow: build + QC

1. Write the task to a file and delegate: `python orchestra.py <agent> --prompt-file task.txt --workdir <project> --expect-changes --verify "<test/lint/syntax check>"`.
2. Trust the report and exit code, not the agent's own claims of success. Exit 3 → check mode/permissions or try the other agent; exit 4 → read the verify output.
3. Read the changed files listed in the report yourself as QC, against the spec.
4. Present the best result to the user.

## Notes

- Delegated agents run as child processes and inherit this shell's environment.
- Don't echo or extract credentials; pi's auth is the user's to manage (`pi auth`).
- If an agent errors, check the agent's own auth/config before assuming the bridge is at fault.
