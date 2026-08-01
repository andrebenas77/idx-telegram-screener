#!/usr/bin/env bash
#
# One-shot VPS setup for the IDX Telegram Screener.
#
# Run this ONCE on a fresh Ubuntu 24.04 server, from the Biznet web console:
#
#   curl -sL https://raw.githubusercontent.com/andrebenas77/idx-telegram-screener/main/deploy/setup.sh | bash
#
# It installs the owner's SSH public key (so normal SSH logins work afterwards),
# then sets up Python, Telethon, Claude Code, swap, the clock, and the repo.
#
# Safe to re-run: every step checks before acting.

set -euo pipefail

# Andre's SSH public key. Public keys are not secret — they are designed to be
# published (GitHub serves everyone's at github.com/<user>.keys). The matching
# PRIVATE key never leaves his PC.
PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIpFuXM3fCU8db+pMDOl3ThIHq9vb/Dg9djiYWOP5s9D idx-screener"

REPO="https://github.com/andrebenas77/idx-telegram-screener.git"
DEST="$HOME/idx-telegram-screener"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[1;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m[!!]\033[0m %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
    warn "Running as root. That works, but SSH will be set up for root only."
    warn "If you meant to use a normal account, press Ctrl+C and log in as that user."
    sleep 3
fi

say "IDX Screener — one-shot setup"
echo "    user: $(whoami)   host: $(hostname)   time: $(date)"

# --- 1. SSH key -----------------------------------------------------------
# Done first and separately: if anything later fails, SSH access still works
# and the rest can be finished comfortably from a real terminal.
say "1/7  Installing SSH key for $(whoami)"
mkdir -p "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
if grep -qF "$PUBKEY" "$HOME/.ssh/authorized_keys"; then
    ok "key already present"
else
    echo "$PUBKEY" >> "$HOME/.ssh/authorized_keys"
    ok "key added"
fi
chmod 700 "$HOME/.ssh"
chmod 600 "$HOME/.ssh/authorized_keys"
ok "permissions set"

say "Checking sudo (you may be asked for your password once)"
sudo -v

# --- 2. Clock -------------------------------------------------------------
say "2/7  Setting timezone to Asia/Jakarta"
sudo timedatectl set-timezone Asia/Jakarta
ok "$(date)"

# --- 3. Packages ----------------------------------------------------------
say "3/7  Installing system packages (this is the slow part, ~2 min)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-pip git jq curl ca-certificates >/dev/null
ok "python3, git, jq, curl installed"

# --- 4. Telethon ----------------------------------------------------------
say "4/7  Installing Telethon (reads your Telegram channels)"
# Ubuntu 24.04 marks the system Python as externally managed; --break-system-packages
# is the supported way to install a user-level package on top of it.
pip3 install --user --quiet --break-system-packages telethon 2>/dev/null \
    || pip3 install --user --quiet telethon
ok "telethon installed"

# --- 5. Swap --------------------------------------------------------------
# This box has 2 GB and Claude Code wants more; swap is what makes it viable.
say "5/7  Adding 4 GB swap"
if [[ -f /swapfile ]]; then
    ok "swapfile already exists"
else
    sudo fallocate -l 4G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    ok "4 GB swap active"
fi
free -h | sed 's/^/    /'

# --- 6. Claude Code -------------------------------------------------------
say "6/7  Installing Claude Code"
if command -v claude >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/claude" ]]; then
    ok "already installed"
else
    curl -fsSL https://claude.ai/install.sh | bash
fi
export PATH="$HOME/.local/bin:$PATH"
grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null \
    || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
if command -v claude >/dev/null 2>&1; then
    ok "$(claude --version 2>/dev/null || echo installed)"
else
    warn "claude not on PATH yet — log out and back in, then run: claude --version"
fi

# --- 7. The screener ------------------------------------------------------
say "7/7  Downloading the screener"
if [[ -d "$DEST/.git" ]]; then
    git -C "$DEST" pull --rebase --quiet || warn "pull failed; keeping existing copy"
    ok "updated $DEST"
else
    git clone --quiet "$REPO" "$DEST"
    ok "cloned to $DEST"
fi
chmod +x "$DEST/scripts/run_daily.sh" 2>/dev/null || true
mkdir -p "$DEST/secrets" "$DEST/build"

IP="$(curl -s --max-time 5 https://api.ipify.org || echo 'your-server-ip')"

cat <<BANNER

============================================================
  SETUP COMPLETE
============================================================

  SSH now works. From PowerShell on your PC:

      ssh -i "\$HOME/.ssh/id_ed25519_idxvps" $(whoami)@${IP}

  You should NOT be asked for a server password — only for
  your key passphrase.

  Still to do (these need you, they can't be automated):
    1. Copy secrets/.env + reference/channels.txt from your PC
    2. python3 scripts/tg_login.py     (type the Telegram code)
    3. Create the bot with @BotFather
    4. Fill in /etc/idx-screener.env
    5. Install the systemd timer

  Tell Claude "setup.sh finished" and it will walk you
  through the rest from a normal terminal.

============================================================

BANNER
