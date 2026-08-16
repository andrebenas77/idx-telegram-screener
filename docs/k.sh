#!/usr/bin/env bash
#
# Minimal console bootstrap for a REBUILT VPS.
#
# Served from GitHub Pages purely so the URL is short enough to TYPE by hand:
# the Biznet web console is noVNC and does not accept clipboard paste, so every
# extra character here is a character typed into a browser terminal.
#
#     curl -sL andrebenas77.github.io/idx-telegram-screener/k.sh | bash
#
# It does the ONE thing that cannot be done over SSH — put the owner's public
# key on the box — plus the user/sudo setup that deploy/setup.sh deliberately
# does not do. Everything heavier (apt, pip, Claude Code, the repos) is left
# for a real SSH session where output can be scrolled and errors copied.
#
# Run as root, from the console. Safe to re-run: every step is guarded.

set -euo pipefail

# Andre's public key. Public keys are designed to be published (GitHub serves
# everyone's at github.com/<user>.keys) — the private half never leaves his PC.
# Must stay byte-identical to deploy/setup.sh line 19.
PUB="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICMJtd3jr09Nub42vrhRJRY0A3lUBpZIZXIFj5uiGqGV idx-screener-3"
USER_NAME="screener"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()  { printf '    \033[1;32m[ok]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m[FAIL]\033[0m %s\n\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run this as root from the Biznet console (try: sudo -i)"

say "1/4  User '$USER_NAME'"
# deploy/setup.sh installs the key for whoever runs it and never creates users,
# but every systemd unit hardcodes User=screener and /home/screener/... . Run
# that script as root and the repo lands in /root while the timers look in
# /home/screener. Creating the user here is what prevents that mismatch.
if id -u "$USER_NAME" >/dev/null 2>&1; then
    ok "already exists"
else
    useradd -m -s /bin/bash "$USER_NAME"
    ok "created"
fi

say "2/4  SSH key"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "/home/$USER_NAME/.ssh"
AK="/home/$USER_NAME/.ssh/authorized_keys"
touch "$AK"
if grep -qF "$PUB" "$AK"; then
    ok "key already present"
else
    printf '%s\n' "$PUB" >> "$AK"
    ok "key added"
fi
chown "$USER_NAME:$USER_NAME" "$AK"
chmod 600 "$AK"
ok "$(wc -l < "$AK") key(s) authorized"

say "3/4  sudo"
usermod -aG sudo "$USER_NAME"
echo "$USER_NAME ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$USER_NAME"
chmod 440 "/etc/sudoers.d/$USER_NAME"
# A malformed sudoers drop-in can lock sudo out entirely, so validate and
# discard rather than leave a broken file in place.
if visudo -cf "/etc/sudoers.d/$USER_NAME" >/dev/null 2>&1; then
    ok "passwordless sudo granted"
else
    rm -f "/etc/sudoers.d/$USER_NAME"
    ok "drop-in rejected by visudo and removed; group 'sudo' still applies"
fi

say "4/4  sshd"
# The one failure that strands you: close the console believing SSH works when
# openssh-server was never installed. A minimal image can ship without it, and
# "Connection refused" then looks exactly like an auth problem. Install it here
# while the console is still open, because this is the last moment it can be.
if ! command -v sshd >/dev/null 2>&1 && [[ ! -x /usr/sbin/sshd ]]; then
    ok "openssh-server missing — installing"
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server
fi
systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || true

SSH_OK=0
systemctl is-active --quiet ssh 2>/dev/null && SSH_OK=1
systemctl is-active --quiet sshd 2>/dev/null && SSH_OK=1
if [[ $SSH_OK -eq 1 ]]; then
    ok "sshd active on port $(ss -tlnp 2>/dev/null | grep -oP 'sshd.*:\K\d+' | head -1 || echo 22)"
else
    die "sshd is NOT running. Do not close the console. Try: systemctl status ssh"
fi

# Prove pubkey auth is actually permitted before declaring victory — a hardened
# image with PasswordAuthentication no AND PubkeyAuthentication no locks
# everyone out, and the console is the only place left to notice.
if sshd -T 2>/dev/null | grep -qi '^pubkeyauthentication yes'; then
    ok "pubkey auth enabled"
else
    printf '    [!!] pubkey auth may be disabled — check /etc/ssh/sshd_config\n'
fi

IP="$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo '103.197.190.92')"

cat <<BANNER

============================================================
  CONSOLE WORK IS DONE — close this window.
============================================================

  From PowerShell on your PC:

      ssh -i "\$HOME/.ssh/id_ed25519_idxvps3" $USER_NAME@$IP

  You should NOT be asked for a password.
  Then tell Claude "console done" and it takes over.

============================================================

BANNER
