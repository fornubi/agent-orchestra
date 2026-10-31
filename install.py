#!/usr/bin/env python3
"""Install the agent-orchestra skill and the OpenCode agent for this user.

Installs:
  ~/.claude/skills/agent-orchestra/SKILL.md    read by Claude Code and OpenCode
  ~/.agents/skills/agent-orchestra/SKILL.md    read by Codex, Gemini CLI and OpenCode
  ~/.config/opencode/agents/orchestra.md       only when opencode is on PATH

Both skill copies have the path to this clone's orchestra.py filled in. OpenCode reads
both locations; it logs a duplicate-name warning and uses one, which is harmless because
the copies are identical.

orchestra.py itself stays in this clone, so `git pull` updates it. Re-run this script
with --force after moving the clone or when the skill changes.

Exit codes:
  0  everything is installed and up to date
  1  a file differs from the installed copy and was left alone (re-run with --force)
"""
import argparse
import pathlib
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parent


def install(target: pathlib.Path, content: str, force: bool) -> bool:
    """Write content to target. Return False if an existing, different file was left alone."""
    if target.exists():
        if target.read_text(encoding="utf-8") == content:
            print(f"up to date  {target}")
            return True
        if not force:
            print(f"skipped     {target} (differs from this repo; use --force to overwrite)")
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    print(f"installed   {target}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Install the agent-orchestra skill and OpenCode agent.")
    ap.add_argument("--force", action="store_true", help="overwrite installed files that differ")
    args = ap.parse_args()

    home = pathlib.Path.home()
    skill = (REPO / "skills" / "agent-orchestra" / "SKILL.md").read_text(encoding="utf-8")
    skill = (skill.replace("{{ORCHESTRA_PY}}", str(REPO / "orchestra.py"))
                  .replace("{{LOGS_DIR}}", str(REPO / "state" / "logs")))
    ok = True
    for root in (".claude", ".agents"):
        ok = install(home / root / "skills" / "agent-orchestra" / "SKILL.md", skill, args.force) and ok

    if shutil.which("opencode"):
        agent = (REPO / "opencode" / "agents" / "orchestra.md").read_text(encoding="utf-8")
        ok = install(home / ".config" / "opencode" / "agents" / "orchestra.md", agent, args.force) and ok
    else:
        print("not found   opencode is not on PATH, skipping its agent (re-run after installing it)")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
