#!/usr/bin/env bash
# Deploy the current code to the VM, and prove it landed.
#
# Run ON THE SERVER:
#     bash /opt/randomgenerals/deploy.sh
#
# Or, the first time, straight from the checkout you already have:
#     cd /opt/randomgenerals && git fetch && git checkout deploy/oracle-and-video-bay && bash deploy.sh
#
# WHY THIS EXISTS RATHER THAN "git pull && systemctl restart"
#
# Two failures look identical to a pull that worked:
#
#   1. The checkout is on a different branch from the one being pushed
#      to. `git pull` then reports "Already up to date" and nothing
#      changes, which is indistinguishable from success.
#   2. The service restarts, fails to boot, and systemd leaves the old
#      process dead. The site is then down rather than stale, and
#      nothing said so.
#
# This names the branch explicitly, shows what actually moved, and then
# checks the running site for a string that only exists in the new code.

set -u

APP_DIR="${APP_DIR:-/opt/randomgenerals}"
BRANCH="${BRANCH:-deploy/oracle-and-video-bay}"
SERVICE="${SERVICE:-randomgenerals}"
# 5001, from PORT in deploy/oracle-setup.sh - gunicorn binds to
# 127.0.0.1 and the Cloudflare tunnel is what faces the world.
URL="${URL:-http://127.0.0.1:5001/app}"

cd "$APP_DIR" || { echo "No such directory: $APP_DIR"; exit 1; }

echo "== before =="
echo "  branch : $(git rev-parse --abbrev-ref HEAD)"
echo "  commit : $(git log --oneline -1)"
echo ""

echo "== fetching =="
git fetch --all --prune || { echo "fetch failed"; exit 1; }

# Explicit, because being on the wrong branch is the failure that hides.
CURRENT="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT" != "$BRANCH" ]; then
    echo "  switching from '$CURRENT' to '$BRANCH'"
    git checkout "$BRANCH" || { echo "checkout failed"; exit 1; }
fi

OLD="$(git rev-parse HEAD)"
git pull --ff-only origin "$BRANCH" || {
    echo ""
    echo "Pull was refused. Usually that means the VM has local edits."
    echo "See them with:  git status"
    echo "Throw them away with:  git reset --hard origin/$BRANCH"
    exit 1
}
NEW="$(git rev-parse HEAD)"

echo ""
if [ "$OLD" = "$NEW" ]; then
    echo "== nothing new =="
    echo "  already at $(git log --oneline -1)"
else
    echo "== what changed =="
    git log --oneline "$OLD..$NEW" | sed 's/^/  /'
    echo ""
    git diff --stat "$OLD..$NEW" | tail -12 | sed 's/^/  /'
fi

echo ""
echo "== python syntax, before restarting anything =="
# A syntax error here has taken this site down before. Better to find it
# with the old process still serving than after it has been killed.
if command -v python3 >/dev/null; then
    if ! python3 -m compileall -q app.py db.py >/dev/null 2>&1; then
        echo "  app.py or db.py does not compile - NOT restarting."
        python3 -m compileall app.py db.py 2>&1 | tail -5 | sed 's/^/  /'
        exit 1
    fi
    echo "  ok"
fi

echo ""
echo "== restarting =="
sudo systemctl restart "$SERVICE" || { echo "restart failed"; exit 1; }

for i in $(seq 1 40); do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null)"
    if [ "$CODE" = "200" ]; then
        echo "  up after ${i}s"
        break
    fi
    sleep 1
done

if [ "${CODE:-}" != "200" ]; then
    echo "  THE SITE IS NOT ANSWERING (last status: ${CODE:-none})"
    echo ""
    echo "  Last 20 log lines:"
    sudo journalctl -u "$SERVICE" -n 20 --no-pager | sed 's/^/    /'
    exit 1
fi

echo ""
echo "== is the new code actually serving? =="
# Strings that exist only in the new build. Checking the running site
# rather than the checkout is the point: it catches a service that
# restarted from a stale copy, a wrong working directory, or a cached
# template.
BODY="$(curl -s "$URL")"
ok=0
fail=0
for marker in "Connected apps" "connectorUrl" "accountManage" "2,000 credits"; do
    if printf '%s' "$BODY" | grep -q "$marker"; then
        echo "  found    : $marker"
        ok=$((ok + 1))
    else
        echo "  MISSING  : $marker"
        fail=$((fail + 1))
    fi
done

# The moon was removed. If it is still being served, this is old code.
if printf '%s' "$BODY" | grep -q "moon-face"; then
    echo "  STILL OLD: moon-face is in the page, so this is pre-update code"
    fail=$((fail + 1))
fi

echo ""
if [ "$fail" -eq 0 ]; then
    echo "DEPLOYED. Now at $(git log --oneline -1)"
    echo ""
    echo "Worth running next:"
    echo "  python3 check_mail.py your@email.com    # can it send codes?"
    echo "  python3 check_paddle.py                 # can it take money?"
    exit 0
fi

echo "The service is up but is serving old markup ($fail checks failed)."
echo "Check that the service runs from $APP_DIR:"
echo "  systemctl cat $SERVICE | grep -E 'WorkingDirectory|ExecStart'"
exit 1
