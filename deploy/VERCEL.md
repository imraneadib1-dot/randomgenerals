# Vercel in front of the VM (replacing the Cloudflare tunnel)

The app does not move. It stays on the Oracle VM with its SQLite file,
its background jobs and gunicorn on `127.0.0.1:5001`. What changes is
the front door:

```
before:  visitor -> Cloudflare -> tunnel (outbound from VM) -> gunicorn
after:   visitor -> Vercel -> https://origin.randomgenerals.com (Caddy on VM) -> gunicorn
```

Vercel's part is one file, `vercel/vercel.json`: forward everything to
`ORIGIN_URL` and stamp every request with `ORIGIN_SECRET`. Caddy's part is
`deploy/Caddyfile`: refuse anything without that secret, and hand the app
the visitor's real address. Both files explain why each line is there.

The cost of leaving the tunnel is that the VM now listens on the public
internet (ports 80 and 443), which the tunnel never needed. The secret is
what keeps that safe - without it anyone who found the VM's address could
skip Vercel and set their own "real IP" header, walking round the rate
limits and the Paddle webhook allowlist.

## What was measured, not assumed

Checked on 28 Sep 2026 through a real deployment of `vercel/vercel.json`
pointed at an echo server, with forged headers:

| | result |
| --- | --- |
| forged `X-Origin-Secret` | replaced with the real one |
| forged `X-Forwarded-Host` | replaced with the public host |
| forged `X-Vercel-Proxied-For`, `Forwarded` | replaced with the real IP |
| forged `CF-Connecting-IP`, `X-Forwarded-For`, `X-Vercel-Forwarded-For` | **passed through** - never trust these |
| `X-Real-IP` | not sent at all |
| query strings, POST bodies, cookies | intact |
| 6 MB upload | accepted |
| streaming | first byte 0.08 s, rest as it was written |
| 10 s response | fine |
| caching | off (MISS every time, even for `Cache-Control: public`) |

And the Caddyfile was run locally in front of the app: 404 without the
secret, the forged `CF-Connecting-IP` replaced, the secret stripped before
gunicorn, streaming unbuffered, and Google's `redirect_uri` built as
`https://randomgenerals.com/api/auth/google/callback`.

**Not yet measured:** replies longer than 10 seconds (Deep mode, video
polling) and uploads above 6 MB. Try both right after switching; if one
fails, that is a Vercel proxy limit and the fix is to send that path
straight to the origin host.

## Switching over

Do the steps in order. The live site keeps working on the tunnel until
step 6, and step 6 can be undone.

### 1. A secret

On the VM:

```bash
openssl rand -hex 32
```

Keep it somewhere private. It goes in two places: Caddy (step 3) and
Vercel (step 5).

### 2. A name for the VM

Wherever the DNS for randomgenerals.com is managed, add:

| type | name | value |
| --- | --- | --- |
| A | `origin` | the VM's public IP |

If that is Cloudflare, set the record to **DNS only** (grey cloud). An
orange-cloud record would put Cloudflare back in the middle.

### 3. Open 80 and 443, install Caddy

Oracle has two firewalls and both must agree (see ORACLE.md). In the
console, add ingress rules to the VCN Security List for TCP 80 and 443
from `0.0.0.0/0`. Then on the VM:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save

sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

sudo cp /opt/randomgenerals/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl edit caddy
```

In the editor that opens, add (with your secret):

```ini
[Service]
Environment=ORIGIN_HOST=origin.randomgenerals.com
Environment=ORIGIN_SECRET=<the secret from step 1>
Environment=APP_PORT=5001
```

```bash
sudo systemctl restart caddy
```

Check it - the first must be 404, the second 200:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://origin.randomgenerals.com/api/health
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Origin-Secret: <secret>" \
  https://origin.randomgenerals.com/api/health
```

A certificate error here means Caddy could not get one: usually port 80
still closed in one of the two firewalls, or the DNS record from step 2
not live yet (`dig +short origin.randomgenerals.com`).

### 4. The app trusts the proxy

`/opt/randomgenerals/.env` should already have `TRUST_PROXY=1` (the
Oracle setup writes it). Nothing else in the app changes.

### 5. Point Vercel at the VM

In the Vercel project `randomgenerals`, Settings > Environment
Variables, for Production and Preview:

- `ORIGIN_URL` = `https://origin.randomgenerals.com`
- `ORIGIN_SECRET` = the secret from step 1 (mark it Sensitive)

Then redeploy. If the project is connected to GitHub, its Root Directory
must be `vercel`, so only `vercel.json` is deployed and not the repo.

Check `https://randomgenerals.vercel.app/app` loads the site, you can sign
in, and a chat reply streams in.

### 6. Move the domain

In the Vercel project, Settings > Domains, add `randomgenerals.com` and
`www.randomgenerals.com`. Vercel shows the exact DNS records it wants
(normally an `A` record for the apex and a `CNAME` for www). At the DNS
provider, replace the records that point at the tunnel with those - DNS
only, if the provider is Cloudflare. Leave the `origin` record alone.

Then check, from any machine:

```bash
curl -sI https://randomgenerals.com/app | grep -i '^server'   # Vercel
bash deploy.sh                                                 # on the VM, as before
```

Sign in with Google once: its redirect address has not changed, so
nothing in Cloud Console needs editing. Paddle's webhook address has not
changed either.

### 7. Retire the tunnel

Once the site has run on Vercel for a day:

```bash
sudo systemctl disable --now cloudflared
```

## Going back

Put the tunnel's DNS records back and `sudo systemctl enable --now
cloudflared`. Caddy can stay running; with no traffic sent to it, it
does nothing.
