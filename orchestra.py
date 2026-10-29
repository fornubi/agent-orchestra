#!/usr/bin/env python3
"""Orchestra bridge: send prompts to external agents (agy/pi) from Claude.

Both CLIs support non-interactive print mode:
  agy: agy -p "<prompt>"  (Google Antigravity CLI)
  pi:  pi  -p "<prompt>"  (Pi agent harness)

Usage:
  python orchestra.py agy "build feature X" [--exec-mode auto] [--effort high] [--dangerous]
  python orchestra.py pi  "build feature X" [--model anthropic/sonnet] [--output json] [--approve] [--no-session]
  python orchestra.py pi  --prompt-file task.txt --workdir ./proj --expect-changes --verify "node --check app.js"

After every run a report lists the files the agent created/modified/deleted in --workdir,
so a run that claims success but changed nothing is visible.

Exit codes:
  0  agent ok (and --verify passed, if given)
  N  agent's own non-zero exit code
  3  silent failure: agent exited 0 with no output and no file changes,
     or --expect-changes was given and nothing changed
  4  --verify command failed
  124 agent timed out
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys

BINS = {"agy": "agy", "pi": "pi"}

# Directories never worth snapshotting (VCS, deps, caches, agent state)
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
             ".pi", ".playwright-mcp", ".next", "dist", "build", ".cache"}
MAX_FILES = 20000
MAX_HASH_BYTES = 50 * 1024 * 1024


def build_cmd(provider: str, prompt: str, args: argparse.Namespace) -> list[str]:
    if provider == "agy":
        cmd = ["agy", "-p", prompt]
        if args.model:
            cmd += ["--model", args.model]
        if args.effort:
            cmd += ["--effort", args.effort]
        if args.exec_mode:
            cmd += ["--mode", args.exec_mode]
        if args.dangerous:
            cmd += ["--dangerously-skip-permissions"]
    else:  # pi
        cmd = ["pi", "-p", prompt]
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
    return cmd


def file_digest(path: str, size: int) -> str:
    if size > MAX_HASH_BYTES:
        return f"size:{size}"
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: str) -> dict[str, tuple[int, int, str]] | None:
    """Map relative path -> (size, mtime_ns, sha1). None if the tree is too large."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
                snap[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns, file_digest(full, st.st_size))
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


def print_report(changes, workdir: str) -> None:
    print("\n--- orchestra report ---")
    print(f"workdir: {workdir}")
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


def run_verify(command: str, workdir: str, timeout: int) -> bool:
    print(f"\n--- verify: {command} ---")
    try:
        r = subprocess.run(command, shell=True, cwd=workdir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"verify: TIMEOUT after {timeout}s")
        return False
    output = tail(r.stdout + "\n" + r.stderr)
    if output:
        print(output)
    print(f"verify: {'PASS' if r.returncode == 0 else f'FAIL (exit {r.returncode})'}")
    return r.returncode == 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Send a prompt to an external agent.")
    ap.add_argument("provider", choices=["agy", "pi"])
    ap.add_argument("prompt", nargs="?", help="the task/prompt to send (or use --prompt-file)")
    ap.add_argument("--prompt-file", help="read the prompt from this file (avoids shell quoting/length limits)")
    ap.add_argument("--model", help="model id/pattern (both)")
    ap.add_argument("--provider", dest="api_provider",
                    help="pi only: provider name for --model")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                    help="agy only: reasoning effort")
    ap.add_argument("--exec-mode", choices=["accept-edits", "auto", "plan"],
                    help="agy only: execution mode")
    ap.add_argument("--output", choices=["text", "json"],
                    help="pi only: output format")
    ap.add_argument("--dangerous", action="store_true",
                    help="agy only: auto-approve all tool permissions")
    ap.add_argument("--approve", action="store_true",
                    help="pi only: trust project-local files")
    ap.add_argument("--no-session", action="store_true",
                    help="pi only: don't save session (ephemeral)")
    ap.add_argument("--workdir", default=os.getcwd(),
                    help="directory the agent runs in and whose file changes are reported (default: cwd)")
    ap.add_argument("--expect-changes", action="store_true",
                    help="fail with exit 3 if the agent changed no files (use for build/edit tasks)")
    ap.add_argument("--verify", help="shell command run in --workdir after the agent; exit 4 if it fails")
    ap.add_argument("--timeout", type=int, default=600, help="seconds")
    args = ap.parse_args()

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

    # On Windows, pi is a POSIX shell script; run its .cmd wrapper via cmd /c.
    if os.name == "nt" and args.provider == "pi":
        cmd_wrap = shutil.which("pi.cmd")
        if cmd_wrap:
            cmd = ["cmd", "/c", cmd_wrap] + build_cmd("pi", args.prompt, args)[1:]
        else:
            cmd = ["cmd", "/c", "pi"] + build_cmd("pi", args.prompt, args)[1:]
    else:
        cmd = build_cmd(args.provider, args.prompt, args)

    before = snapshot(workdir)
    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print(f"error: {args.provider} timed out after {args.timeout}s", file=sys.stderr)
        after = snapshot(workdir)
        print_report(diff_snapshots(before, after) if before is not None and after is not None else None, workdir)
        sys.exit(124)
    after = snapshot(workdir)
    changes = diff_snapshots(before, after) if before is not None and after is not None else None
    changed = changes is None or any(changes.values())

    out = r.stdout.strip()
    err = r.stderr.strip()
    if out:
        print(out)
    # stderr is shown on failure, and also when stdout is empty (otherwise a silent failure leaves no clue)
    if err and (r.returncode != 0 or not out):
        print(tail(err), file=sys.stderr)
    print_report(changes, workdir)

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
