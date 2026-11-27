#!/usr/bin/env bash
# One-shot deploy from the Mac when Tailscale is up: email engine + n8n workflows to ~/outreach, LinkedIn half to ~/vidamuyarchi.
# Usage: bash scripts/deploy.sh [host] [--install-cron]
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST=razer-gpu
INSTALL_CRON=0
for arg in "$@"; do
  case "$arg" in
    --install-cron) INSTALL_CRON=1 ;;
    -*) echo "usage: $0 [host] [--install-cron]"; exit 2 ;;
    *) HOST="$arg" ;;
  esac
done

ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOST" true 2>/dev/null || { echo "Razer not reachable over Tailscale"; exit 1; }
python3 "$ROOT/src/n8n/build_workflows.py" || exit 1
ssh -o BatchMode=yes "$HOST" 'mkdir -p ~/outreach/workflows ~/outreach/inbox ~/outreach/osint ~/outreach/bin ~/outreach/tools ~/vidamuyarchi/src/linkedin ~/vidamuyarchi/scripts ~/vidamuyarchi/config && chmod 700 ~/outreach && find ~/outreach/workflows -maxdepth 1 -name "*.json" -delete' || exit 1
scp -q -o BatchMode=yes "$ROOT"/src/engine/*.py "$ROOT"/src/engine/install_tools.sh "$ROOT"/config/private/*.pdf "$ROOT"/config/companies.csv "$HOST":~/outreach/ || { echo "engine copy failed"; exit 1; }
scp -q -o BatchMode=yes "$ROOT"/src/n8n/workflows/*.json "$HOST":~/outreach/workflows/ || { echo "workflow copy failed"; exit 1; }
RSYNC=(rsync -a -e "ssh -o BatchMode=yes" --exclude '__pycache__/')
{ "${RSYNC[@]}" "$ROOT/src/linkedin/" "$HOST":vidamuyarchi/src/linkedin/ \
  && "${RSYNC[@]}" "$ROOT/scripts/" "$HOST":vidamuyarchi/scripts/ \
  && "${RSYNC[@]}" --exclude 'private/' "$ROOT/config/" "$HOST":vidamuyarchi/config/; } || { echo "LinkedIn rsync failed"; exit 1; }
if ssh -o BatchMode=yes "$HOST" '/usr/bin/python3 -m py_compile ~/vidamuyarchi/src/linkedin/linkedin_outreach.py && bash -n ~/vidamuyarchi/scripts/linkedin_daily.sh'; then
  echo "linkedin compile ok"; LINKEDIN_OK=1
else
  echo "!! LinkedIn half failed to compile on the Razer"; LINKEDIN_OK=0
fi
ssh -o BatchMode=yes "$HOST" '
  export PATH=$HOME/.npm-global/bin:$HOME/.local/bin:$PATH
  cd ~/outreach && chmod 600 *.py *.pdf companies.csv && chmod 700 pipeline.py install_tools.sh
  [ -f .env ] && chmod 600 .env
  /usr/bin/python3 -m py_compile *.py && echo "compile ok"
  bash install_tools.sh || echo "!! tool install had errors (see above); engine falls back to built-in SMTP probe"
  /usr/bin/python3 pipeline.py seed
  for f in workflows/error_handler.json $(ls workflows/*.json | grep -v error_handler); do echo "$f: $(n8n import:workflow --input=$f 2>&1 | grep -i "imported" || echo "!! import failed")"; done
  echo "== preflight =="; /usr/bin/python3 pipeline.py preflight | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); print(\"ok=\",d[\"ok\"],\"failed=\",d[\"failed\"]); [print(\"  \",c[\"name\"],c[\"ok\"],c[\"detail\"][:80]) for c in d[\"checks\"]]"
  echo "== status =="; /usr/bin/python3 pipeline.py status
'

echo
CRON_MODE=print
if [ "$INSTALL_CRON" -eq 1 ]; then
  if [ "$LINKEDIN_OK" -eq 1 ]; then CRON_MODE=install; else echo "!! --install-cron skipped: the LinkedIn half did not compile"; fi
fi
ssh -o BatchMode=yes "$HOST" /usr/bin/python3 - "$CRON_MODE" <<'PYEOF'
import os, subprocess, sys
run = "/usr/bin/xvfb-run -a --server-args='-screen 0 1280x900x24' /bin/bash scripts/linkedin_daily.sh >> $HOME/linkedin_daily.log 2>&1"
repo = os.path.expanduser("~/vidamuyarchi")
want = [f"3 9 * * * cd {repo} && LINKEDIN_SHEETS_SYNC=1 HEADED=1 {run}", f"40 15 * * * cd {repo} && HARVEST_ONLY=1 LINKEDIN_SHEETS_SYNC=1 HEADED=1 {run}"]
if sys.argv[1] != "install":
    print("== LinkedIn cron lines (IST), NOT installed; re-run with --install-cron to add them ==")
    print("\n".join(want))
    sys.exit(0)
cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
if cur.returncode and "no crontab" not in cur.stderr:
    sys.exit("!! crontab -l failed, crontab left unchanged: " + cur.stderr.strip())
before = cur.stdout if cur.returncode == 0 else ""
print("== crontab before ==\n" + (before.rstrip("\n") or "(empty)"))
def runs_daily(line):
    s = line.strip()
    return "linkedin_daily.sh" in s and not s.startswith("#")
lines = before.splitlines()
active = [l.strip() for l in lines if runs_daily(l)]
stray = [l for l in active if l not in want or active.count(l) > 1]
if stray:
    sys.exit("!! crontab left unchanged: these linkedin_daily.sh lines would double the LinkedIn runs:\n" + "\n".join(stray))
lines += [w for w in want if w not in active]
after = "\n".join(lines) + "\n"
if after != before and subprocess.run(["crontab", "-"], input=after, text=True).returncode:
    sys.exit("!! crontab install failed")
now = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
print("== crontab after" + ("" if after != before else " (unchanged)") + " ==\n" + now.rstrip("\n"))
PYEOF

echo
echo "n8n: workflows are imported INACTIVE and this script never activates them. After review, activate them by hand"
echo "in the n8n editor; re-check after every deploy, since a re-import replaces them:"
for f in "$ROOT"/src/n8n/workflows/*.json; do python3 -c 'import json,sys; print("   -", json.load(open(sys.argv[1]))["name"])' "$f"; done
echo "   (the Error Handler needs no activation)"
echo "Optional first backlog build:  ssh $HOST 'cd ~/outreach && python3 pipeline.py intake --time-budget 480 --probe-budget 60'"
