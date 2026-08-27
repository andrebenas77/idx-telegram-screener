# VPS setup — unattended daily screener

Runbook for moving the screener onto a Jakarta VPS so it runs itself at 07:00 WIB on weekdays,
publishes to GitHub Pages, and messages your phone. Your PC keeps working as an ad-hoc second driver.

**Target box:** Biznet Gio NEO Lite SS 2.2 — 2 vCPU / 2 GB, Jakarta, ~Rp109k/mo, Ubuntu 24.04 LTS.

> **Note on RAM.** Claude Code documents a **4 GB minimum** and this box has 2 GB. That's a
> deliberate cost trade backed by a 4 GB swapfile. **Step 9.2 measures it under real load** — if it
> thrashes, resize in place to NEO Lite MS 4.4 (4 GB, ~Rp179k). Find that out in week 1, not month 6.

---

## 0. On your PC first

| What | How |
|---|---|
| Claude Pro/Max token | `claude setup-token` → copy the `sk-ant-oat01-…` value (valid ~1 year) |
| Telegram bot | Message **@BotFather** → `/newbot` → copy the HTTP API token |
| Sectors key | Copy your existing `SECTORS_API_KEY` value |
| SSH key | `ssh-keygen -t ed25519 -C "andrebenas77@idx-screener-vps" -f "$HOME/.ssh/id_ed25519_idxvps"` |

Create the SSH key **before ordering** — Biznet's panel asks for the public key during instance
creation. Only the `.pub` half ever leaves your PC. Set a passphrase: this key guards a server that
will hold a logged-in Telegram *user* session, which is full account access.

## 1. Provision and harden

Order the instance with Ubuntu 24.04 and your public key. Then:

```bash
adduser screener && usermod -aG sudo screener
install -d -m 700 -o screener -g screener /home/screener/.ssh
# put your id_ed25519_idxvps.pub into /home/screener/.ssh/authorized_keys (mode 600)
```

Lock down SSH in `/etc/ssh/sshd_config` — `PasswordAuthentication no`, `PermitRootLogin no` — then
`systemctl restart ssh`. Firewall to SSH only:

```bash
ufw allow OpenSSH && ufw --force enable
```

Clock (the scripts derive the session date in WIB):

```bash
timedatectl set-timezone Asia/Jakarta
```

Swap — this is what makes 2 GB viable:

```bash
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

## 2. Runtime

```bash
sudo apt update && sudo apt install -y python3 python3-pip git jq
pip3 install --user telethon
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

## 3. The repo

```bash
git clone https://github.com/andrebenas77/idx-telegram-screener.git ~/idx-telegram-screener
```

A clone alone is **not runnable** — `.gitignore` deliberately excludes secrets and your channel list.
From your PC:

```bash
scp secrets/.env             idxvps:~/idx-telegram-screener/secrets/.env
scp reference/channels.txt   idxvps:~/idx-telegram-screener/reference/channels.txt
```

Do **not** copy `screener.session` — step 4 makes a fresh one. Then:

```bash
chmod 600 ~/idx-telegram-screener/secrets/.env
chmod +x  ~/idx-telegram-screener/scripts/run_daily.sh
```

Set the git identity and a push credential (a GitHub PAT with `repo` scope, or a deploy key
generated *on the server* — keep it separate from your login key so revoking one never locks out the
other). The `--ipv4` flag your Windows machine needs is a local quirk and is not required here.

## 4. Telegram login (interactive — you must type the code)

```bash
cd ~/idx-telegram-screener && python3 scripts/tg_login.py
```

You'll get a login code in Telegram, plus a 2FA prompt if you have one. **You will also get a
security notification about a new login — that one is you.** Then:

```bash
chmod 600 ~/idx-telegram-screener/secrets/screener.session
```

We log in fresh rather than copying the session file: a session that suddenly appears on a new IP is
likelier to trip Telegram's checks than a clean login, and Jakarta keeps the geographic jump small.

## 5. Find your chat id

Open Telegram, send your new bot any message (e.g. `hi`), then on the VPS:

```bash
TELEGRAM_BOT_TOKEN=<your-bot-token> python3 scripts/notify_telegram.py --whoami
```

It prints the numeric `TELEGRAM_CHAT_ID` for every chat that has messaged the bot.

## 6. Secrets file

`/etc/idx-screener.env`, root-owned, `chmod 600`:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…
SECTORS_API_KEY=…
TELEGRAM_BOT_TOKEN=…
TELEGRAM_CHAT_ID=…
```

`SECTORS_API_KEY` **must** be here rather than in `secrets/.env`: `scripts/sectors_client.py` reads
it from the process environment, and a scheduled job starts with a near-empty one. Miss this and the
Foreign column silently shows "–" with no error.

## 7. Install the units

```bash
sudo cp ~/idx-telegram-screener/deploy/idx-screener.service /etc/systemd/system/
sudo cp ~/idx-telegram-screener/deploy/idx-screener.timer   /etc/systemd/system/
sudo cp ~/idx-telegram-screener/deploy/idx-bot.service      /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now idx-screener.timer idx-bot.service
systemctl list-timers idx-screener.timer
```

The timer fires **Mon–Fri 07:00 Asia/Jakarta**. It also fires on Indonesian public holidays when IDX
is closed — the board just repeats the previous close on thin chatter, which the recency weighting
absorbs.

## 8. Your PC afterwards

The VPS is the only *scheduled* writer, but your PC can still run the skill any time. Run
`git pull --rebase` in the skill folder before you do — both machines only append to
`data/history.csv` and regenerate `docs/`, so that one guard removes every conflict case.

## 9. Verification

Do these in order. **9.2 is the one that decides whether 2 GB was the right call.**

1. **Telethon path** — `python3 scripts/fetch_mentions.py`. Confirms session, channels, and matching.
2. **Memory under load** — run it for real while watching RAM in a second SSH session:
   ```bash
   ./scripts/run_daily.sh --trigger manual
   ```
   ```bash
   watch -n2 free -m
   ```
   Healthy: peak usage leaves headroom and swap stays near-idle. Unhealthy: swap climbing steadily,
   run past ~10 min, or an OOM kill in `journalctl -k`. If unhealthy → resize to MS 4.4.
3. **Foreign column populated** (not "–") — proves `SECTORS_API_KEY` reached the job environment.
4. **Site rebuilt** — `https://andrebenas77.github.io/idx-telegram-screener/` shows today's date and
   a new build timestamp. Pages takes a minute or two after the push.
5. **Phone trigger** — send `/run`, expect an immediate ack then a summary ~2–3 min later.
   Also try `/status`.
6. **Let the timer fire once on its own** before trusting it.
7. **Secrets still untracked** — `git status` in the repo must not list `secrets/`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Foreign column all "–" | `SECTORS_API_KEY` missing from `/etc/idx-screener.env` |
| Auth errors in the log | `CLAUDE_CODE_OAUTH_TOKEN` expired (~1 year) — re-run `claude setup-token` |
| `claude: not found` in service log | `Environment=PATH=` line in the unit doesn't match the install path |
| Bot silent | `systemctl status idx-bot`; check the chat id whitelist matches your actual chat |
| Bot replies "run in progress" forever | Stale `/tmp/idx-screener.lock` holder — `systemctl status idx-screener` |
| Telegram logged the session out | Re-run `python3 scripts/tg_login.py` |
| Two commits fighting | Your PC pushed without `git pull --rebase` first |

Logs: `~/logs/screener-<date>.log`, plus `journalctl -u idx-screener` and `journalctl -u idx-bot`.

---

## Trade layer: bot commands, private portfolio page, Excel export

Added 2026-08-27. The screener half above is unchanged.

### Systemd units

| unit | when | what |
|---|---|---|
| `idx-trade-plan.timer` | Mon–Fri 07:15 WIB | `trade_plan.py --summary --notify` |
| `idx-trade-preclose.timer` | Mon–Fri 15:40 WIB | `trade_manage.py --notify`, `Persistent=false` |
| `idx-portfolio.timer` | Mon–Fri every 15 min + 16:25 | private portfolio page, `Persistent=false` |

All run as `andrebenas77` from `/home/andrebenas77/idx-telegram-screener`. **The units in
`deploy/` shipped with `User=screener` and `/home/screener/...`, which is not this box** —
rewrite the user and paths before installing or every fire fails silently.

Install: `sudo install -m 644 -o root -g root deploy/idx-*.{service,timer}
/etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now <timer>`

Test with `sudo systemctl start <unit>` — **never by invoking the script over ssh**, which
has no systemd environment, so `/etc/idx-screener.env` is absent and the run dies in
seconds reporting missing credentials. And never run a build script directly in the repo:
it dirties `docs/` and the next `git pull --rebase` fails, stranding the box on old code.

### The ledger lives HERE

`data/book/ledger.jsonl` is owned by this machine. The laptop's copy is
`ledger.jsonl.retired-<date>`, and `position_book.assert_owner()` refuses both reads and
writes there so it can never silently report a flat book. Do not restore it.

### Excel export

`apt install python3-openpyxl` — **not pip**, which PEP 668 blocks on Ubuntu 24.04.
`/export` in Telegram builds the workbook and uploads it.

### Private portfolio page

Repo `andrebenas77/idx-book`, private, Pages from `/docs`. Deploy key `~/.ssh/id_idxbook`
with **write** access; `~/.ssh/config` aliases `github-idxbook` with `IdentitiesOnly yes`
— without it ssh offers the login key first, GitHub accepts it as the USER, and the push
carries full account permissions, defeating the per-repo scoping entirely.

Config in `secrets/portfolio.env` (gitignored, 0600); template is
`secrets/portfolio.env.example`.

**Pages on a private repo requires a paid GitHub plan.** On the free tier
`POST repos/<owner>/idx-book/pages` returns 422. After upgrading:

```
gh api -X POST repos/andrebenas77/idx-book/pages -f "source[branch]=main" -f "source[path]=/docs"
sudo systemctl enable --now idx-portfolio.timer
```

Until then the timer stays disabled: the page still generates and commits, but nothing
serves it, and 28 commits a day to a page nobody can open is noise.
