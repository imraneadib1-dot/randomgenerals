#!/usr/bin/env bash
# Deploy the current code to the VM, and prove it landed.
#
# Run ON THE SERVER:
#     bash /opt/randomgenerals/deploy.sh
#
# Or, the first time, straight from the checkout you already have:
#     cd /opt/randomgenerals && git fetch && git checkout main && bash deploy.sh
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
# This names the branch explicitly, shows what actually moved, checks
# the running server is on the new build, then empties Cloudflare's
# cache and checks the public site is on it too - the site people see
# is Cloudflare's copy, and it can lag the server by hours.

set -u

APP_DIR="${APP_DIR:-/opt/randomgenerals}"
# main, since 2026-09-12. The deploy/oracle-and-video-bay branch was
# being fast-forwarded to main by hand after every change, which is
# one more step that could be forgotten - and was.
BRANCH="${BRANCH:-main}"
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
    MODULES="app.py db.py videogen.py higgsfield_api.py paddle_billing.py features.py ratelimit.py providers.py agents brain/intent.py"
    if ! python3 -m compileall -q $MODULES >/dev/null 2>&1; then
        echo "  a module does not compile - NOT restarting."
        python3 -m compileall $MODULES 2>&1 | tail -5 | sed 's/^/  /'
        exit 1
    fi
    echo "  ok"
    # Result checking (videogen.probe) reads the real resolution and
    # duration of every clip with ffprobe. Without it the check stops at
    # the container signature - the site works, it just checks less.
    if ! command -v ffprobe >/dev/null; then
        echo "  note: ffprobe is not installed (sudo apt install -y ffmpeg) -"
        echo "        generated clips are checked for format only, not size."
    fi
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
# The page carries the commit it was built from (<meta name="rg-build">,
# see build_id() in app.py). Checking the RUNNING site rather than the
# checkout is the point: it catches a service that restarted from a
# stale copy, a wrong working directory, or a cached template. This
# used to grep for phrases that happened to be new in one build, which
# meant every later change either updated the list or went unverified.
BODY="$(curl -s "$URL")"
fail=0
WANT="$(git rev-parse --short HEAD)"
GOT="$(printf '%s' "$BODY" \
       | grep -o 'name="rg-build" content="[^"]*"' \
       | sed 's/.*content="//; s/"$//')"
if [ -z "$GOT" ]; then
    echo "  MISSING  : no rg-build marker in the page - this is pre-marker code"
    fail=$((fail + 1))
elif [ "$GOT" != "$WANT" ]; then
    echo "  STALE    : page says build $GOT, checkout is $WANT"
    fail=$((fail + 1))
else
    echo "  serving  : build $GOT (matches HEAD)"
fi

# The moon was removed long ago. If it is still being served, this is
# a very old process indeed.
if printf '%s' "$BODY" | grep -q "moon-face"; then
    echo "  STILL OLD: moon-face is in the page, so this is pre-update code"
    fail=$((fail + 1))
fi

if [ "$fail" -ne 0 ]; then
    echo ""
    echo "The service is up but is serving old markup ($fail checks failed)."
    echo "Check that the service runs from $APP_DIR:"
    echo "  systemctl cat $SERVICE | grep -E 'WorkingDirectory|ExecStart'"
    exit 1
fi

# ----------------------------------------------------------------------
# Everything above talks to gunicorn on 127.0.0.1. Nobody else does:
# visitors reach the site through Cloudflare, which keeps its own
# copies, so "the server is serving the new build" and "people are
# getting the new build" are two different claims. This used to prove
# the first and print DEPLOYED, while the site kept answering with the
# build before.

# Read one value from .env without sourcing it: .env is the service's
# EnvironmentFile, not a shell script, and a value with a $ or a space
# in it would be run rather than read.
env_value() {
    grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- \
        | sed -e 's/\r$//' -e 's/^["'"'"']//' -e 's/["'"'"']$//'
}

CF_ZONE_ID="${CF_ZONE_ID:-$(env_value CF_ZONE_ID)}"
CF_PURGE_TOKEN="${CF_PURGE_TOKEN:-$(env_value CF_PURGE_TOKEN)}"
PUBLIC_URL="${PUBLIC_URL:-$(env_value PUBLIC_SITE_URL)}"
PUBLIC_URL="${PUBLIC_URL:-https://randomgenerals.com}"
PUBLIC_PAGE="${PUBLIC_URL%/}/app"

echo ""
echo "== Cloudflare =="
if [ -n "$CF_ZONE_ID" ] && [ -n "$CF_PURGE_TOKEN" ]; then
    RESP="$(curl -s --max-time 20 -X POST \
        "https://api.cloudflare.com/client/v4/zones/$CF_ZONE_ID/purge_cache" \
        -H "Authorization: Bearer $CF_PURGE_TOKEN" \
        -H "Content-Type: application/json" \
        --data '{"purge_everything":true}')"
    if printf '%s' "$RESP" | grep -q '"success":[[:space:]]*true'; then
        echo "  purged   : everything Cloudflare had cached for this zone"
    else
        echo "  PURGE FAILED - Cloudflare said:"
        printf '%s\n' "${RESP:-(nothing - api.cloudflare.com did not answer)}" \
            | head -c 400 | sed 's/^/    /'
        echo ""
        echo "  (the token needs Zone / Cache Purge / Purge on this zone)"
    fi
else
    echo "  not purging: CF_ZONE_ID and CF_PURGE_TOKEN are not in .env"
    echo "  (see .env.example - without them Cloudflare empties its"
    echo "   cache on its own schedule, not on a deploy)"
fi

# A purge takes a few seconds to reach every data centre, so the public
# page is given a little while to catch up before it is called stale.
PUBLIC_GOT=""
for i in $(seq 1 10); do
    PUBLIC_BODY="$(curl -s --max-time 15 "$PUBLIC_PAGE" 2>/dev/null)"
    PUBLIC_GOT="$(printf '%s' "$PUBLIC_BODY" \
                  | grep -o 'name="rg-build" content="[^"]*"' \
                  | sed 's/.*content="//; s/"$//')"
    [ "$PUBLIC_GOT" = "$WANT" ] && break
    sleep 3
done

# What each layer is doing with the page and with the app's entry
# script, so a stale site can be diagnosed from this output instead of
# guessed at. cf-cache-status HIT on the page means a Cloudflare rule
# is caching HTML; the fix is in the dashboard (Caching > Cache Rules),
# not here.
show_headers() {
    curl -s -o /dev/null -D - --max-time 15 "$1" 2>/dev/null \
        | grep -iE '^(cf-cache-status|cache-control|age):' \
        | tr -d '\r' | sed 's/^/      /'
}
ENTRY="$(printf '%s' "$PUBLIC_BODY" \
         | grep -o 'type="module" src="[^"]*"' | head -1 \
         | sed 's/.*src="//; s/"$//; s/&amp;/\&/g')"
echo "  page     : $PUBLIC_PAGE"
show_headers "$PUBLIC_PAGE"
if [ -n "$ENTRY" ]; then
    echo "  script   : $ENTRY"
    show_headers "${PUBLIC_URL%/}$ENTRY"
fi

public_fail=0
if [ -z "$PUBLIC_BODY" ]; then
    echo "  could not fetch $PUBLIC_PAGE from this machine - public"
    echo "  build not checked (set PUBLIC_URL if the address is different)"
elif [ "$PUBLIC_GOT" = "$WANT" ]; then
    echo "  serving  : build $PUBLIC_GOT to the public (matches HEAD)"
else
    echo "  STALE    : the public page says build ${PUBLIC_GOT:-none}, the server is on $WANT"
    echo "             Cloudflare is answering with a copy it kept."
    public_fail=1
fi

echo ""
if [ "$public_fail" -eq 0 ]; then
    echo "DEPLOYED. Now at $(git log --oneline -1)"
    echo ""
    echo "Worth running next:"
    echo "  python3 check_mail.py your@email.com    # can it send codes?"
    echo "  python3 check_paddle.py                 # can it take money?"
    exit 0
fi

echo "The server is on the new build, but the public site is not."
echo "  - set CF_ZONE_ID and CF_PURGE_TOKEN in .env and deploy again, or"
echo "    purge by hand: Cloudflare dashboard > Caching > Purge Everything"
echo "  - if it happens again after a purge, a Cache Rule is caching HTML:"
echo "    Caching > Cache Rules, and take /app and / out of it"
exit 1
