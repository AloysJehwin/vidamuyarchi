#!/usr/bin/env python3
"""Add explicit Bash permission rules for ssh/scp to ~/.claude/settings.json so
Claude Code runs Razer commands without the auto-mode classifier. Backs up first."""
import json, shutil, sys, time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
RULES = ["Bash(ssh:*)", "Bash(scp:*)"]

def main():
    if not SETTINGS.exists():
        sys.exit(f"not found: {SETTINGS}")
    backup = SETTINGS.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(SETTINGS, backup)
    data = json.loads(SETTINGS.read_text())
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    added = [r for r in RULES if r not in allow]
    allow.extend(added)
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")
    print(f"backup: {backup}")
    print(f"added: {added or 'nothing (already present)'}")
    print(f"permissions.allow now: {allow}")
    print(f"defaultMode: {perms.get('defaultMode')}")

if __name__ == "__main__":
    main()
