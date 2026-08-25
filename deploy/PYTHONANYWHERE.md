# Deploying to PythonAnywhere (free, no card)

The free "Beginner" plan runs Flask natively, keeps a real 512MB disk so
`app.db` survives restarts, and needs no payment details.

What it does **not** give you is a custom domain. The site lives at
`https://<username>.pythonanywhere.com`; `randomgenerals.com` cannot
point at it on this plan.

There is no GPU and no Ollama here, and this app answers only from
Ollama. So the site will serve every page, sign-ins and billing will
work, and a prompt will report that the local AI isn't running. Adding
a cloud provider back is what would make it answer - see app.py.

Outbound access on the free tier is allowlisted; DuckDuckGo, Stripe,
Paddle and Google's OAuth endpoints are all on it, checked against
<https://www.pythonanywhere.com/whitelist/>.

---

## 1. Account

Sign up at <https://www.pythonanywhere.com/registration/register/beginner/>.
Note the username you choose - it becomes your URL.

## 2. Get the code onto the server

**Consoles** tab -> **Bash**, then:

```bash
git clone https://github.com/imraneadib1-dot/randomgenerals.git
cd randomgenerals
```

## 3. Virtualenv and dependencies

```bash
mkvirtualenv --python=/usr/bin/python3.11 randomgenerals
pip install -r requirements-cloud.txt
```

`mkvirtualenv` also activates it. If you come back to a fresh console
later, reactivate with `workon randomgenerals`.

## 4. Secrets

Never commit these - `.env` is gitignored.

```bash
cd ~/randomgenerals
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" > .env
chmod 600 .env
nano .env      # replace PASTE_YOUR_KEY_HERE with the real gsk_... value
```

`Ctrl+O`, `Enter`, `Ctrl+X` saves and exits nano.

Optional, only if you use them: `STRIPE_SECRET_KEY`,
`STRIPE_PRICE_ID_PRO`, `STRIPE_WEBHOOK_SECRET`, `GEMINI_API_KEY`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.

Leave `ALLOW_MOCK_UPGRADE` out entirely. It hands out Pro for free.

## 5. Create the web app

**Web** tab -> **Add a new web app**.

- Domain: accept the default
- Framework: **Manual configuration** (*not* the Flask option - that
  scaffolds a new app over yours)
- Python: **3.11**

Then on the Web tab set:

- **Source code**: `/home/<username>/randomgenerals`
- **Virtualenv**: `/home/<username>/.virtualenvs/randomgenerals`
- **WSGI configuration file**: click it. Delete everything in the editor,
  paste the contents of `deploy/pythonanywhere_wsgi.py`, and change
  `USERNAME` on line 16 to your username. Save.

## 6. Static files

Flask can serve these itself, but letting nginx do it is faster and
costs no CPU quota. Still on the Web tab, under **Static files**:

| URL | Directory |
|---|---|
| `/static/` | `/home/<username>/randomgenerals/static/` |

## 7. Go

Hit the green **Reload** button, then open
`https://<username>.pythonanywhere.com`.

Check it came up cleanly:

```bash
curl -s https://<username>.pythonanywhere.com/api/health
```

`"mode"` will read `unavailable`, which is correct here - there is no
Ollama on this host.

---

## When something breaks

**Error log** on the Web tab is the first place to look; it has the
actual traceback.

| Symptom | Cause |
|---|---|
| `ModuleNotFoundError: No module named 'app'` | Source code path wrong, or `USERNAME` not changed in the WSGI file |
| `ModuleNotFoundError` for flask/requests | Virtualenv path wrong on the Web tab |
| Reply appears all at once, not word by word | nginx buffering; `streaming_response()` in `app.py` sets `X-Accel-Buffering: no` to prevent this |
| Site 500s after a `git pull` | You have to press **Reload**; code changes are not picked up automatically |

## Keeping it alive

Free web apps expire after **three months**. PythonAnywhere emails you a
link to press to extend it, and the app goes offline if you ignore it.

CPU is capped at 100 seconds/day. That meters *your* processor time, not
time spent waiting on the network, so serving pages uses very little
of it. Going over
throttles the app rather than stopping it.

## Updating later

```bash
workon randomgenerals
cd ~/randomgenerals
git pull
pip install -r requirements-cloud.txt   # only if dependencies changed
```

Then **Reload** on the Web tab.
