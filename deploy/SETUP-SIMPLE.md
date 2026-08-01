# Setting up your server — the plain-English version

This is the same setup as `VPS-SETUP.md`, written without assuming you know Linux.

**What we're building:** your server wakes up at 07:00 every weekday, reads your Telegram channels,
builds the screener board, publishes it to your website, and sends the summary to your phone.

**How long:** about 90 minutes total, split into 7 short sessions. You can stop after any session and
come back later — nothing breaks in between.

---

## Before you start — three things to know

**1. You'll be typing into a black window.** That's normal. On Windows, press the Start button, type
`PowerShell`, and open it. That's your control panel for the server.

**2. When you type a password, nothing appears on screen.** No dots, no stars, nothing. It looks
broken. It isn't — Linux hides passwords completely. Type it and press Enter.

**3. Copy-paste works, but differently.** Copy normally (Ctrl+C). To paste into PowerShell,
**right-click** or press Ctrl+V. Paste the commands rather than retyping them — a single typo in a
long command is the most common way this goes wrong.

Throughout this guide: `server` means the computer you rented from Biznet. `your PC` means the
Windows machine in front of you.

---

## Session 1 — Get connected (~10 min)

**Goal:** type a command on your PC and see the server answer.

### 1.1 Find two pieces of information

Log into the Biznet Gio portal and open your NEO Lite server. You need:

- **The IP address** — four numbers with dots, like `103.xxx.xxx.xxx`. Usually labelled "Public IP".
- **The root password** — shown when the server was created, or emailed to you.

If you can't find the password, look for a "Reset Password" or "Console" option on the server's page
in the portal. Resetting it is safe and takes a moment. *(Biznet's menus change over time, so I'm
describing what to look for rather than exact button names.)*

> "root" is the Linux word for the main administrator account — full control over the machine.

### 1.2 Connect

In PowerShell on your PC, type this, replacing the IP with yours:

```bash
ssh root@103.xxx.xxx.xxx
```

**The first time only**, it will ask something like *"The authenticity of host ... can't be
established. Are you sure you want to continue connecting?"* — type `yes` and press Enter. This is
your PC noting down the server's identity so it can recognise it next time.

Then it asks for the password. Type it (remember: nothing appears) and press Enter.

### 1.3 Check it worked

You should now see something like `root@your-server:~#`. That means you're *on the server* — every
command you type now runs there, not on your PC.

Try it:

```bash
echo "hello from the server"
```

If it prints back, you're in. **Session 1 done.**

To leave at any time, type `exit`. To come back, run the same `ssh` command.

---

## Session 2 — Make it secure (~20 min)

**Goal:** stop using the password, and lock the front door.

Right now anyone on the internet can try to guess your password, and this server is about to hold a
logged-in copy of your Telegram account. So we replace the password with a **key** — a very long
file that can't realistically be guessed — and then switch passwords off entirely.

### 2.1 Create your key (on your PC, not the server)

Open a **second** PowerShell window, leaving the server one alone. Then:

```bash
ssh-keygen -t ed25519 -C "idx-screener" -f "$HOME/.ssh/id_ed25519_idxvps"
```

It asks for a passphrase. **Set one** and don't lose it — it protects the key file itself, so that
someone who steals your laptop still can't reach the server.

This creates two files. One is secret, one is shareable:

| File | What it is |
|---|---|
| `id_ed25519_idxvps` | **Private.** Never send this anywhere, to anyone, ever. |
| `id_ed25519_idxvps.pub` | **Public.** Safe to paste into the server. |

Now show the public one so you can copy it:

```bash
cat "$HOME/.ssh/id_ed25519_idxvps.pub"
```

Copy the whole line it prints — it starts with `ssh-ed25519` and ends with `idx-screener`.

### 2.2 Create your everyday account (on the server)

Back in the first window. Working as `root` all the time is like doing your daily work while logged
in as the bank's administrator — one wrong command has no safety net. So we make a normal account:

```bash
adduser screener
```

It asks for a password (choose one, remember it) and then some optional details — name, phone, etc.
Press Enter through those, then `Y` to confirm.

Give it permission to do admin tasks when needed:

```bash
usermod -aG sudo screener
```

### 2.3 Install your key into that account

```bash
mkdir -p /home/screener/.ssh
nano /home/screener/.ssh/authorized_keys
```

`nano` is a simple text editor inside the terminal. Paste your public key line (right-click), then:

- **Ctrl+O** then **Enter** to save
- **Ctrl+X** to exit

Then set the permissions — Linux refuses to use a key file that others could read:

```bash
chown -R screener:screener /home/screener/.ssh
chmod 700 /home/screener/.ssh
chmod 600 /home/screener/.ssh/authorized_keys
```

### 2.4 Test the key BEFORE switching passwords off

This order matters. If you disable passwords and the key doesn't work, you're locked out.

**Keep your root window open.** In the second window on your PC:

```bash
ssh -i "$HOME/.ssh/id_ed25519_idxvps" screener@103.xxx.xxx.xxx
```

If you land at `screener@your-server:~$` — the key works. If it asks for a password instead,
something's off; stop and tell me rather than continuing.

### 2.5 Now close the front door (in the root window)

```bash
nano /etc/ssh/sshd_config
```

Find these two lines and set them (they may say `yes`, or start with a `#` which means "ignored" —
delete the `#`):

```
PasswordAuthentication no
PermitRootLogin no
```

Save with Ctrl+O, Enter, Ctrl+X. Apply it:

```bash
systemctl restart ssh
```

Turn on the firewall so only the login port is reachable:

```bash
ufw allow OpenSSH
ufw --force enable
```

### 2.6 Make logging in easy

On your PC, create a shortcut file so you don't retype the long command. In PowerShell:

```bash
notepad "$HOME\.ssh\config"
```

Say yes to creating it, then paste (replace the IP):

```
Host idxvps
    HostName 103.xxx.xxx.xxx
    User screener
    IdentityFile ~/.ssh/id_ed25519_idxvps
```

Save and close. From now on, connecting is just:

```bash
ssh idxvps
```

**Session 2 done.** This was the hardest one.

---

## Session 3 — Set the clock and add breathing room (~5 min)

Connect with `ssh idxvps`, then:

```bash
sudo timedatectl set-timezone Asia/Jakarta
```

The screener works in Jakarta time, so the server must agree about what "today" means.

Now add **swap** — emergency overflow space. Your server has 2 GB of memory and Claude Code wants
more, so this lets it borrow from disk when it runs short. Slower, but it means the run finishes
instead of being killed:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Check it:

```bash
free -h
```

You should see a `Swap:` row showing `4.0Gi`. **Session 3 done.**

---

## Session 4 — Install the software (~10 min)

```bash
sudo apt update
sudo apt install -y python3 python3-pip git jq
pip3 install --user telethon
```

Then Claude Code:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Close and reopen your connection (`exit`, then `ssh idxvps`) so the new program is found, then
check both:

```bash
claude --version
python3 --version
```

Both should print version numbers. **Session 4 done.**

---

## Session 5 — Copy the screener across (~15 min)

### 5.1 Download the public part

```bash
git clone https://github.com/andrebenas77/idx-telegram-screener.git ~/idx-telegram-screener
```

### 5.2 Copy the private part from your PC

Your Telegram keys and channel list are deliberately **not** on GitHub, so they have to be copied
by hand. In a PowerShell window **on your PC**:

```bash
cd "$HOME\.claude\skills\idx-telegram-screener"
scp secrets/.env idxvps:~/idx-telegram-screener/secrets/.env
scp reference/channels.txt idxvps:~/idx-telegram-screener/reference/channels.txt
```

Back on the server, lock those files down and make the runner executable:

```bash
chmod 600 ~/idx-telegram-screener/secrets/.env
chmod +x ~/idx-telegram-screener/scripts/run_daily.sh
```

**Session 5 done.**

---

## Session 6 — Log into Telegram and set up the bot (~20 min)

### 6.1 Log the server into your Telegram

```bash
cd ~/idx-telegram-screener
python3 scripts/tg_login.py
```

Telegram sends a code to your phone — type it in. If you have two-factor enabled, it asks for that
password too.

> **You will get a Telegram security alert about a new login. That one is you.** It's worth knowing
> in advance so it doesn't alarm you.

```bash
chmod 600 ~/idx-telegram-screener/secrets/screener.session
```

### 6.2 Create your bot

On your phone, open Telegram and message **@BotFather**. Send `/newbot`, pick a name and a username
ending in `bot`. It replies with a **token** — a long string like `12345:AAH...`. Keep it.

> This bot is separate from your own Telegram account. It's what sends you the morning summary and
> listens for your `/run` command.

### 6.3 Find your chat ID

Send your new bot any message (just `hi`). Then on the server, replacing the token:

```bash
cd ~/idx-telegram-screener
TELEGRAM_BOT_TOKEN=12345:AAH... python3 scripts/notify_telegram.py --whoami
```

It prints a number — that's your chat ID.

### 6.4 Store the four secrets

```bash
sudo nano /etc/idx-screener.env
```

Paste this, filling in your real values:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
SECTORS_API_KEY=...
TELEGRAM_BOT_TOKEN=12345:AAH...
TELEGRAM_CHAT_ID=123456789
```

- **CLAUDE_CODE_OAUTH_TOKEN** — get it by running `claude setup-token` on **your PC** (it opens a
  browser). Valid about a year.
- **SECTORS_API_KEY** — the one you already use on your PC.

Save (Ctrl+O, Enter, Ctrl+X), then protect it:

```bash
sudo chmod 600 /etc/idx-screener.env
```

**Session 6 done.**

---

## Session 7 — Switch on the automation (~10 min)

```bash
sudo cp ~/idx-telegram-screener/deploy/idx-screener.service /etc/systemd/system/
sudo cp ~/idx-telegram-screener/deploy/idx-screener.timer   /etc/systemd/system/
sudo cp ~/idx-telegram-screener/deploy/idx-bot.service      /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now idx-screener.timer idx-bot.service
```

Confirm the schedule is armed:

```bash
systemctl list-timers idx-screener.timer
```

It should show the next 07:00 run. **Session 7 done — the automation is live.**

---

## Final check — does it actually work?

### Test 1: run it once by hand

```bash
cd ~/idx-telegram-screener
./scripts/run_daily.sh --trigger manual
```

Takes 2–3 minutes. You should get a Telegram message at the end.

### Test 2: watch the memory (do this during Test 1)

**This is the important one** — it tells you whether the Rp109k server is big enough. Open a second
connection and run:

```bash
watch -n2 free -m
```

- **Good:** the `Swap` used column stays low or rises a little then settles.
- **Bad:** swap climbs steadily and keeps climbing, or the run takes over 10 minutes, or it dies.

If it's bad, the fix is to resize to the 4 GB plan (~Rp179k) in the Biznet panel. **Better to find
this out now than in three months.**

### Test 3: the rest

- Your site shows today's date: `https://andrebenas77.github.io/idx-telegram-screener/`
- The Foreign column shows numbers, not dashes. Dashes mean `SECTORS_API_KEY` didn't get through.
- Send `/run` to your bot from your phone — it should reply immediately, then send a summary.
- Send `/status` — it reports the last run.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| `Permission denied (publickey)` | The key isn't installed right. Use the Biznet web console to get in and re-check Session 2.3. |
| `command not found: claude` | Reopen the connection (`exit` then `ssh idxvps`). |
| Foreign column all dashes | `SECTORS_API_KEY` missing or misspelt in `/etc/idx-screener.env`. |
| Bot doesn't answer | `sudo systemctl status idx-bot` — check the chat ID matches. |
| Telegram logged the server out | Re-run `python3 scripts/tg_login.py`. |
| No morning message | `journalctl -u idx-screener -n 50` shows what happened. |

To read the log of the most recent run:

```bash
ls -t ~/logs/ | head -1
```

Nothing here is irreversible — if a session goes wrong, you can rebuild the server from the Biznet
panel and start again. You'd only lose the setup time, never your data, because the screener's
history lives in GitHub.
