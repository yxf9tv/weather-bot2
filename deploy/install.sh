#!/usr/bin/env bash
# One-time droplet setup. Run as root on a fresh Ubuntu 24.04 droplet:
#   bash <(curl -fsSL https://raw.githubusercontent.com/yxf9tv/weather-bot2/main/deploy/install.sh)
# Then copy .env and kalshi.pk into /home/bot/weather-bot2/ (scp), and run: systemctl start weather-bot
set -euo pipefail
REPO=https://github.com/yxf9tv/weather-bot2.git
apt-get update -q && apt-get install -y -q git sqlite3 curl ca-certificates
id -u bot >/dev/null 2>&1 || useradd -m -s /bin/bash bot
timedatectl set-timezone UTC
su - bot -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
su - bot -c "[ -d weather-bot2 ] || git clone $REPO weather-bot2"
su - bot -c "cd weather-bot2 && ~/.local/bin/uv sync && mkdir -p data && cp scripts/pre-push-secret-scan.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push"
install -m 644 /home/bot/weather-bot2/deploy/weather-bot.service /etc/systemd/system/weather-bot.service
systemctl daemon-reload && systemctl enable weather-bot
echo
echo "Next: scp .env kalshi.pk root@<ip>:/home/bot/weather-bot2/  then on the droplet:"
echo "  chown bot:bot /home/bot/weather-bot2/.env /home/bot/weather-bot2/kalshi.pk && chmod 600 /home/bot/weather-bot2/.env /home/bot/weather-bot2/kalshi.pk"
echo "  su - bot -c 'cd weather-bot2 && ~/.local/bin/uv run python -m weather_bot probe'"
echo "  systemctl start weather-bot && tail -f /home/bot/weather-bot2/data/run.log"
