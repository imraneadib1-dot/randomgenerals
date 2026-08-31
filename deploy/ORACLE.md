# Deploying to Oracle Cloud Always Free

The goal: `randomgenerals.com` stays up with your laptop switched off,
for £0/month, forever.

Oracle's Always Free tier gives you a VM that is genuinely free with no
expiry — **2 ARM cores and 12GB RAM**. Everything in this app runs on it:
accounts, threads, memory, credits, web search, the sandboxed code
runner, and the video bay. The one thing it cannot do is run a good
model, because there is no GPU — see **Where the answers come from**.

> **The allowance was halved on 15 June 2026**, from 4 OCPUs / 24GB to
> 2 / 12, with no announcement — the docs were simply edited. From
> 18 August 2026 Oracle began terminating Always Free instances above
> the new limit.
>
> Take this seriously when you pick a shape: Oracle's own wording is that
> if an over-limit resource is terminated, *it may not be possible to
> recreate resources above the updated limit*. Overshooting can strand
> you below where you started. **Create at 2/12.**
>
> Pay-As-You-Go accounts reportedly keep 4/24 at no charge, but Oracle
> has never confirmed that publicly and the documentation says the limits
> apply to all tenancies. Do not plan around it.

2 cores and 12GB is still comfortably more than this app needs — it runs
Flask, gunicorn and ffmpeg with room to spare. It only pinches if you
also want a local model, which was already the weakest part of the plan.

Budget about an hour, most of it waiting on Oracle.

---

## What you have to do yourself

I can't do these for you — they need your Oracle account, and the
console is the only way in.

Everything after step 4 is one script.

---

## 1. Account

Sign up at <https://signup.cloud.oracle.com>.

- A card is required **for identity verification only** — a small
  authorisation hold, then released. Always Free resources are not
  charged unless you deliberately upgrade.
- The signup flow pushes the **30-day trial with $300 of credits**.
  That is a separate thing from Always Free. When the trial lapses, the
  Always Free resources keep running; anything else stops. Do not build
  on trial credits and assume they persist.
- Oracle only reclaims idle instances on never-upgraded accounts, which
  is a real argument for upgrading to Pay As You Go later — Always Free
  shapes stay free on a PAYG account.
- Pick your home region carefully. **It cannot be changed afterwards**,
  and Always Free capacity lives in it. For Morocco, Frankfurt or
  Amsterdam are the closest.

## 2. Create the VM

Compute → Instances → **Create instance**.

| field | value |
|---|---|
| Image | Canonical Ubuntu 22.04 or 24.04 |
| Shape | **Ampere A1 Compute** (ARM) |
| OCPUs | **2** — the Always Free limit since June 2026 |
| Memory | **12 GB** |
| SSH keys | Generate a key pair and **download the private key** |

> **The one that wastes everyone's afternoon:** `Out of host capacity`
> on Ampere. It is extremely common in popular regions. Options, in
> order: try a different Availability Domain in the same region; retry
> at a quieter hour; or start at 1 OCPU / 6GB and resize up to 2/12
> later. Retrying the same AD immediately will keep failing.

Note the **public IP** when it finishes.

## 3. SSH in

```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<PUBLIC_IP>
```

If it hangs rather than refusing, that is the firewall, not the key —
see the note in step 5.

## 4. Get the code onto the VM

```bash
sudo mkdir -p /opt/randomgenerals
sudo chown $USER:$USER /opt/randomgenerals
git clone https://github.com/imraneadib1-dot/randomgenerals.git /opt/randomgenerals
```

## 5. Run the setup script

```bash
cd /opt/randomgenerals
bash deploy/oracle-setup.sh
```

It installs Python, ffmpeg and cloudflared, creates the virtualenv from
`requirements-cloud.txt`, writes `/opt/randomgenerals/.env` with a fresh
`SECRET_KEY`, and installs a systemd service running gunicorn.

Add `--with-ollama` if you want a local model too — read the note at the
top of the script first.

> **On firewalls.** The script locks inbound down to SSH and leaves it
> there, because traffic arrives through a Cloudflare tunnel, which is an
> outbound connection. Nothing needs to listen on the public internet.
>
> This matters because Oracle has **two** firewalls and both must agree:
> the VCN Security List in the console, and iptables on the instance
> itself. Forgetting the second is the classic Oracle symptom of "the
> port is open in the console and still nothing connects". Using a tunnel
> means you never touch either.

## 6. Add a model key

```bash
nano /opt/randomgenerals/.env      # set GROQ_API_KEY
```

Get one free at <https://console.groq.com/keys> — no card. Without it the
app serves every page and every prompt fails at the point of asking.

## 7. Start it

```bash
sudo systemctl start randomgenerals
curl -s localhost:5001/api/health
journalctl -u randomgenerals -f        # if it does not come up
```

## 8. Move the tunnel off the laptop

The tunnel credentials live on whichever machine currently serves the
site. On the **laptop**:

```bash
scp -i <key> ~/.cloudflared/*.json ~/.cloudflared/cert.pem \
    ubuntu@<PUBLIC_IP>:~/.cloudflared/
```

Then on the **VM**:

```bash
sudo cloudflared service install
sudo systemctl start cloudflared
```

> Only one machine may serve a tunnel at a time. **Stop the laptop's
> tunnel first**, or requests land unpredictably on either machine and
> you will chase a bug that is really two servers.

Confirm from anywhere:

```bash
curl -s https://randomgenerals.com/api/health
```

Now switch the laptop off.

---

## Where the answers come from

There is no GPU on any free tier anywhere, including this one. A 7B model
on two Ampere cores answers at well under a word a second, which reads as
broken rather than slow.

Measured on this VM, not estimated: `llama3.2:3b` took **99.6 seconds**
to reach the first token of "name three primary colours", and
`qwen2.5-coder:7b` never finished "say hi" inside a 120-second timeout.

So replies come from **Groq**, free and without a card. Its ceiling is
8,000 tokens per minute per key, shared across everyone using the site at
once — the app reads the remaining budget from every response and routes
to the local model *before* the limit rather than after, so running out
degrades a reply instead of failing it. Pro accounts get first claim on
what is left.

`--with-ollama` pulls `llama3.2:3b` and serves it locally as well. Read
the timings above first. It exists so "runs on hardware you control" can
stay literally true if that matters more than speed — not because it is
the better experience.

**Note this changes what the app may honestly claim.** The provider label
is computed from `OLLAMA_URL`: point it at a remote endpoint and the
channel reads "Ollama Cloud", with a note that prompts leave the machine,
because on this page they otherwise do not.

## What is degraded, and what is not

| | on the VM |
|---|---|
| Chat, code, memory, accounts, credits, web search | works |
| Sandboxed code runner | works |
| Video bay | works — the script installs ffmpeg |
| `app.db` across restarts | works — a real disk, unlike Render free |
| Image generation | hosted API only; no local Stable Diffusion |
| Local 7B models | no GPU |

## Keeping it alive

- Oracle reclaims Always Free compute that looks idle, **on accounts
  that have never upgraded**. A service handling real traffic is
  normally enough. Upgrading to Pay As You Go removes the risk entirely
  and still does not charge for Always Free shapes.
- Updating: `cd /opt/randomgenerals && git pull && sudo systemctl restart randomgenerals`
- Logs: `journalctl -u randomgenerals -f`
