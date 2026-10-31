#!/usr/bin/env python3
"""Orchestra bridge: send prompts to external agents (claude/agy/pi/opencode).

All CLIs support a non-interactive mode:
  agy:      agy -p "<prompt>"          (Google Antigravity CLI)
  pi:       pi  -p "<prompt>"          (Pi agent harness)
  opencode: opencode run "<prompt>"    (OpenCode)
  claude:   claude -p                  (Claude Code; prompt sent on stdin)

An agent can never delegate to itself: the calling agent, and every agent earlier in a
delegation chain, is left out of `providers` and refused with exit 6.

Usage:
  python orchestra.py providers
  python orchestra.py claude "build feature X" [--model sonnet] [--effort high] [--exec-mode plan] [--output json]
  python orchestra.py agy "build feature X" [--exec-mode auto] [--effort high] [--dangerous]
  python orchestra.py pi  "build feature X" [--model anthropic/sonnet] [--output json] [--approve] [--no-session]
  python orchestra.py opencode "build feature X" [--model provider/model] [--agent build] [--output json] [--auto]
  python orchestra.py pi  --prompt-file task.txt --workdir ./proj --expect-changes --verify "node --check app.js"

After every run a report lists the files the agent created/modified/deleted in --workdir,
so a run that claims success but changed nothing is visible.

Exit codes:
  0  agent ok (and --verify passed, if given)
  N  agent's own non-zero exit code
  3  silent failure: agent exited 0 with no output and no file changes,
     or --expect-changes was given and nothing changed
  4  --verify command failed
  5  agent hit a quota/billing/auth limit with no file changes
  6  refused: the provider is the calling agent or already in the delegation chain
  124 agent timed out
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import sys

BINS = {"claude": "claude", "agy": "agy", "pi": "pi", "opencode": "opencode"}

# Variable each CLI sets for the commands it runs; used to recognise the calling agent
CALLER_MARKERS = {"claude": "CLAUDECODE", "agy": "ANTIGRAVITY_AGENT",
                  "pi": "PI_CODING_AGENT", "opencode": "OPENCODE"}
# Comma-separated agents already in this delegation, passed down to each delegated agent
CHAIN_ENV = "ORCHESTRA_CHAIN"
# Claude's names for --exec-mode. It needs acceptEdits to write files in print mode.
CLAUDE_MODES = {"accept-edits": "acceptEdits", "auto": "auto", "plan": "plan"}

# Directories never worth snapshotting (VCS, deps, caches, agent state)
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
             ".pi", ".pi-lens-probe-home", ".opencode", ".playwright-mcp", ".next", "dist", "build", ".cache",
             ".wrangler", ".turbo", ".vite", ".parcel-cache", ".ruff_cache", ".pytest_cache",
             ".mypy_cache", "coverage"}
# Suffixes never worth snapshotting (TS build info, SQLite WAL/SHM, logs)
SKIP_FILE_SUFFIXES = (".tsbuildinfo", "-wal", "-shm", ".log")
MAX_FILES = 20000
MAX_HASH_BYTES = 50 * 1024 * 1024
KILL_GRACE_SECONDS = 5

# Specific error phrases only: bare words like "quota" or "billing" also appear in
# normal agent summaries (e.g. work on a billing/finance feature) and would
# misreport a successful analysis-only run as a quota failure.
QUOTA_PATTERNS = (
    "usage limit reached",
    "rate limit exceeded",
    "rate limit reached",
    "too many requests",
    "quota exceeded",
    "exceeded your quota",
    "exceeded your current quota",
    "insufficient credits",
    "insufficient_quota",
    "check your billing",
    "billing hard limit",
    "payment required",
    "401 unauthorized",
    "invalid api key",
    "not logged in",
    "please log in",
    "authentication failed",
)

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
LOGS_DIR = os.path.join(STATE_DIR, "logs")
MAX_LOG_FILES = 50


ATTACHED_PROMPT_MESSAGE = "Your full task is in the attached file. Read all of it and follow it exactly."


def build_cmd(provider: str, prompt: str, args: argparse.Namespace,
              prompt_path: str | None = None) -> list[str]:
    """prompt_path: when set (pi/opencode only), the prompt is attached as a file
    instead of passed as an argument."""
    if provider == "claude":  # the prompt goes on stdin
        cmd = ["claude", "-p", "--permission-mode", CLAUDE_MODES[args.exec_mode or "accept-edits"]]
        if args.model:
            cmd += ["--model", args.model]
        if args.effort:
            cmd += ["--effort", args.effort]
        if args.output:
            cmd += ["--output-format", args.output]
    elif provider == "agy":
        cmd = ["agy", "-p", prompt]
        if args.model:
            cmd += ["--model", args.model]
        if args.effort:
            cmd += ["--effort", args.effort]
        if args.exec_mode:
            cmd += ["--mode", args.exec_mode]
        if args.dangerous:
            cmd += ["--dangerously-skip-permissions"]
    elif provider == "opencode":
        cmd = ["opencode", "run"]
        if args.model:
            cmd += ["--model", args.model]
        # "orchestra" agent (~/.config/opencode/agents/orchestra.md): file edits allowed, no shell
        cmd += ["--agent", args.agent or "orchestra"]
        if args.output == "json":
            cmd += ["--format", "json"]
        if args.auto:
            cmd += ["--auto"]
        if prompt_path:
            cmd += ["--file", prompt_path, "--", ATTACHED_PROMPT_MESSAGE]
        else:
            cmd += ["--", prompt]
    else:  # pi
        cmd = ["pi", "-p"]
        if args.model:
            cmd += ["--model", args.model]
        if args.api_provider:
            cmd += ["--provider", args.api_provider]
        if args.output:
            cmd += ["--mode", args.output]
        if args.approve:
            cmd += ["--approve"]
        if args.no_session:
            cmd += ["--no-session"]
        cmd += [f"@{prompt_path}", ATTACHED_PROMPT_MESSAGE] if prompt_path else [prompt]
    return cmd


def find_callers(explicit: str | None) -> list[str]:
    """Agents that must not be delegated to: the delegation chain, plus the calling agent.
    --caller replaces detection from the environment, not the chain."""
    callers = [c for c in os.environ.get(CHAIN_ENV, "").split(",") if c]
    if explicit:
        callers.append(explicit)
    else:
        callers += [name for name, var in CALLER_MARKERS.items() if os.environ.get(var)]
    return list(dict.fromkeys(callers))


def print_providers(callers: list[str]) -> None:
    excluded = [p for p in BINS if p in callers]
    available = [p for p in BINS if p not in callers and shutil.which(BINS[p])]
    missing = [p for p in BINS if p not in callers and not shutil.which(BINS[p])]
    print(f"available: {', '.join(available) or '(none)'}")
    if excluded:
        print(f"excluded (calling agent or delegation chain): {', '.join(excluded)}")
    if missing:
        print(f"not installed: {', '.join(missing)}")


def file_digest(path: str, size: int) -> str:
    if size > MAX_HASH_BYTES:
        return f"size:{size}"
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_quota_error(text: str) -> str | None:
    """Return the (trimmed) line whose text indicates a quota/billing/auth failure, or None."""
    if not text:
        return None
    low = text.lower()
    for pat in QUOTA_PATTERNS:
        idx = low.find(pat)
        if idx != -1:
            start = low.rfind("\n", 0, idx) + 1
            end = low.find("\n", idx)
            if end == -1:
                end = len(text)
            return text[start:end].strip()
    return None

def init_log(provider: str, cmd: list[str], workdir: str,
             prompt_path: str | None, prompt: str) -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # Exclusive create, so runs started in the same second (e.g. in parallel) keep separate logs
    n = 0
    while True:
        suffix = f"-{n}" if n else ""
        path = os.path.join(LOGS_DIR, f"{ts}-{provider}{suffix}.log")
        try:
            f = open(path, "x", encoding="utf-8")
            break
        except FileExistsError:
            n += 1
    with f:
        f.write(f"argv: {json.dumps(cmd)}\n")
        f.write(f"workdir: {workdir}\n")
        f.write(f"prompt_attached: {'yes' if prompt_path else 'no'}\n")
        if prompt_path:
            f.write(f"prompt_path: {prompt_path}\n")
        f.write("--- prompt ---\n")
        f.write(prompt)
        f.write("\n--- end prompt ---\n")
    retain_logs()
    return path

def append_log(path: str, exit_code: int, duration: float,
               changes: dict[str, list[str]] | None, combined: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\nexit_code: {exit_code}\n")
        f.write(f"duration_s: {duration:.1f}\n")
        if changes is not None:
            for kind in ("created", "modified", "deleted"):
                if changes[kind]:
                    f.write(f"{kind}: {', '.join(changes[kind])}\n")
        f.write("\n--- output (last 200 lines) ---\n")
        f.write(tail(combined, 200))
        f.write("\n")

def retain_logs() -> None:
    try:
        logs = sorted((os.path.join(LOGS_DIR, n) for n in os.listdir(LOGS_DIR) if n.endswith(".log")),
                      key=os.path.getmtime)
        for old in logs[:-MAX_LOG_FILES]:
            os.remove(old)
    except OSError:
        pass

def snapshot(root: str, ignores=()) -> dict[str, tuple[int, int, str]] | None:
    """Map relative path -> (size, mtime_ns, sha1). None if the tree is too large."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if any(name.endswith(suf) for suf in SKIP_FILE_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if ignores:
                rel_fwd = rel.replace(os.sep, "/")
                if any(fnmatch.fnmatch(rel_fwd, pat) for pat in ignores):
                    continue
            try:
                st = os.stat(full)
                snap[rel] = (st.st_size, st.st_mtime_ns, file_digest(full, st.st_size))
            except OSError:
                continue
            if len(snap) > MAX_FILES:
                return None
    return snap


def diff_snapshots(before, after) -> dict[str, list[str]]:
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after) if before[p][2] != after[p][2])
    return {"created": created, "modified": modified, "deleted": deleted}


def print_report(changes, workdir: str, log_path: str | None = None) -> None:
    print("\n--- orchestra report ---")
    print(f"workdir: {workdir}")
    if log_path:
        print(f"log: {log_path}")
    if changes is None:
        print("file changes: not tracked (workdir too large)")
        return
    total = sum(len(v) for v in changes.values())
    if not total:
        print("file changes: NONE")
        return
    for kind in ("created", "modified", "deleted"):
        for p in changes[kind]:
            print(f"  {kind:<8} {p}")


def tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run_agent(cmd: list[str], workdir: str, timeout: int, callers: list[str], provider: str,
              stdin_text: str | None = None) -> subprocess.CompletedProcess:
    """Run the agent; on timeout kill its whole process tree (children left running would hold
    the output pipes and hang us). Returns a CompletedProcess with returncode 124 on timeout
    (outputs are best-effort)."""
    # Some agents (opencode) resolve their project dir from $PWD, which is inherited from the caller's shell
    env = {**os.environ, "PWD": workdir, CHAIN_ENV: ",".join([*callers, provider])}
    proc = subprocess.Popen(cmd, cwd=workdir, env=env,
                            stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            # Its own process group on POSIX, so kill_tree can reach its children
                            start_new_session=os.name != "nt")
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired as e:  # a child left the group and still holds the pipes
            out, err = as_text(e.stdout), as_text(e.stderr)
        return subprocess.CompletedProcess(cmd, 124, out, err)
    except BaseException:  # e.g. Ctrl+C, which no longer reaches a separate process group
        kill_tree(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def as_text(data) -> str:
    """TimeoutExpired carries partial output as bytes on POSIX, whatever the Popen text mode."""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def run_verify(command: str, workdir: str, timeout: int) -> bool:
    print(f"\n--- verify: {command} ---")
    try:
        # Decode as UTF-8 explicitly: the Windows default (cp1252) fails on build
        # tool output, which leaves stdout as None
        r = subprocess.run(command, shell=True, cwd=workdir, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"verify: TIMEOUT after {timeout}s")
        return False
    output = tail((r.stdout or "") + "\n" + (r.stderr or ""))
    if output:
        print(output)
    print(f"verify: {'PASS' if r.returncode == 0 else f'FAIL (exit {r.returncode})'}")
    return r.returncode == 0


def main() -> None:
    # Agent output contains non-ASCII (e.g. arrows); the Windows console default
    # (cp1252) would raise UnicodeEncodeError when printing it
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Send a prompt to an external agent.")
    ap.add_argument("provider", choices=[*BINS, "providers"],
                    help="agent to run, or 'providers' to list the agents you can delegate to")
    ap.add_argument("prompt", nargs="?", help="the task/prompt to send (or use --prompt-file)")
    ap.add_argument("--prompt-file", help="read the prompt from this file (avoids shell quoting/length limits)")
    ap.add_argument("--model", help="model id/pattern (all; opencode uses provider/model)")
    ap.add_argument("--provider", dest="api_provider",
                    help="pi only: provider name for --model")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                    help="claude/agy: reasoning effort")
    ap.add_argument("--exec-mode", choices=["accept-edits", "auto", "plan"],
                    help="claude/agy: execution mode (claude default: accept-edits)")
    ap.add_argument("--output", choices=["text", "json"],
                    help="claude/pi/opencode: output format")
    ap.add_argument("--dangerous", action="store_true",
                    help="agy only: auto-approve all tool permissions")
    ap.add_argument("--approve", action="store_true",
                    help="pi only: trust project-local files")
    ap.add_argument("--auto", action="store_true",
                    help="opencode only: auto-approve permissions that are not explicitly denied")
    ap.add_argument("--agent", help="opencode only: agent to use (default: orchestra)")
    ap.add_argument("--no-session", action="store_true",
                    help="pi only: don't save session (ephemeral)")
    ap.add_argument("--caller",
                    help="name of the calling agent, replacing detection from the environment")
    ap.add_argument("--workdir", default=os.getcwd(),
                    help="directory the agent runs in and whose file changes are reported (default: cwd)")
    ap.add_argument("--expect-changes", action="store_true",
                    help="fail with exit 3 if the agent changed no files (use for build/edit tasks)")
    ap.add_argument("--verify", help="shell command run in --workdir after the agent; exit 4 if it fails")
    ap.add_argument("--timeout", type=int, default=600, help="seconds")
    ap.add_argument("--ignore", action="append", default=[],
                    help="extra glob (fnmatch, workdir-relative with forward slashes) to ignore in the change report; repeatable")
    # Intermixed: with plain parse_args an optional positional can't come after the flags
    args = ap.parse_intermixed_args()

    callers = find_callers(args.caller)
    if args.provider == "providers":
        print_providers(callers)
        sys.exit(0)
    if args.provider in callers:
        print(f"error: {args.provider} cannot be delegated to: it is the calling agent or already "
              f"in the delegation chain ({', '.join(callers)}).", file=sys.stderr)
        print_providers(callers)
        sys.exit(6)

    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            args.prompt = f.read()
    if not args.prompt or not args.prompt.strip():
        sys.exit("error: empty prompt (pass it as an argument or via --prompt-file)")

    workdir = os.path.abspath(args.workdir)
    if not os.path.isdir(workdir):
        sys.exit(f"error: workdir not found: {workdir}")

    binary = shutil.which(BINS[args.provider])
    if not binary:
        sys.exit(f"error: {args.provider} not found in PATH")

    # On Windows, pi/opencode are npm shell scripts run via `cmd /c`, and cmd.exe
    # cuts an argument at its first newline - a multi-line prompt would arrive as
    # its first line only. Attach such prompts as a file instead.
    prompt_path = None
    uses_cmd_wrapper = os.name == "nt" and args.provider in ("claude", "pi", "opencode")
    if uses_cmd_wrapper and args.provider != "claude" and "\n" in args.prompt:
        if args.prompt_file:
            prompt_path = os.path.abspath(args.prompt_file)
        else:
            fd, prompt_path = tempfile.mkstemp(prefix="orchestra-prompt-", suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(args.prompt)

    cmd = build_cmd(args.provider, args.prompt, args, prompt_path)
    if uses_cmd_wrapper:
        cmd_wrap = shutil.which(f"{BINS[args.provider]}.cmd") or BINS[args.provider]
        cmd = ["cmd", "/c", cmd_wrap] + cmd[1:]

    start = time.time()
    log_path = init_log(args.provider, cmd, workdir, prompt_path, args.prompt)

    before = snapshot(workdir, args.ignore)
    r = run_agent(cmd, workdir, args.timeout, callers, args.provider,
                  args.prompt if args.provider == "claude" else None)
    after = snapshot(workdir, args.ignore)
    changes = diff_snapshots(before, after) if before is not None and after is not None else None
    changed = changes is None or any(changes.values())

    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    append_log(log_path, r.returncode, time.time() - start, changes, combined)

    quota_err = find_quota_error(combined)
    if quota_err:
        if not changed:
            print(f"error: {args.provider} hit a quota/billing/auth limit: {quota_err}", file=sys.stderr)
            sys.exit(5)
        print(f"warning: {args.provider} reported a possible quota/billing/auth issue but changed files; continuing: {quota_err}",
              file=sys.stderr)

    out = r.stdout.strip()
    err = r.stderr.strip()
    if out:
        print(out)
    # stderr is shown on failure, and also when stdout is empty (otherwise a silent failure leaves no clue)
    if err and (r.returncode != 0 or not out):
        print(tail(err), file=sys.stderr)
    print_report(changes, workdir, log_path)

    if r.returncode == 124:
        print(f"error: {args.provider} timed out after {args.timeout}s", file=sys.stderr)
        sys.exit(124)
    if r.returncode != 0:
        sys.exit(r.returncode)
    if not out and not changed:
        print(f"\nerror: SILENT FAILURE - {args.provider} exited 0 with no output and changed no files. "
              "Likely a permission/mode problem (e.g. agy --exec-mode accept-edits cannot create new files) "
              "or an auth/config issue.", file=sys.stderr)
        sys.exit(3)
    if args.expect_changes and not changed:
        print(f"\nerror: --expect-changes was set but {args.provider} changed no files.", file=sys.stderr)
        sys.exit(3)
    if args.verify and not run_verify(args.verify, workdir, args.timeout):
        sys.exit(4)
    sys.exit(0)


if __name__ == "__main__":
    main()
