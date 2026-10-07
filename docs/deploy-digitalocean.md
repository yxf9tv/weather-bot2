# Moving the bot to a DigitalOcean droplet

Goal: run from an IP that both Polymarket (non-restricted country) and Kalshi accept, 24/7, with the
database carried over. Stop the Mac copy before starting the droplet copy; two copies would double-quote.

## 1. Create the droplet
- Region: **Amsterdam (AMS3)** or **Frankfurt (FRA1)**. Not London, not any US region.
- Image: Ubuntu 24.04 LTS. Size: Basic, regular CPU, 1 GB / 1 vCPU (the $6 one).
- Authentication: SSH key. No password login.

## 2. Install (as root on the droplet)
```
bash <(curl -fsSL https://raw.githubusercontent.com/yxf9tv/weather-bot2/main/deploy/install.sh)
```
Creates user `bot`, installs `uv`, clones the repo, installs the systemd unit (enabled, not started).

## 3. Copy secrets and the database (from the Mac)
```
cd ~/projects/weather-bot-2
uv run python -m weather_bot disable-trading              # stop new quotes on the Mac
launchctl bootout gui/$(id -u)/com.bzcruz.weather-bot      # stop the Mac loop for good
scp .env kalshi.pk root@<ip>:/home/bot/weather-bot2/
scp data/weather_bot.sqlite data/stations_cache.json root@<ip>:/home/bot/weather-bot2/data/
```
Then on the droplet:
```
chown -R bot:bot /home/bot/weather-bot2 && chmod 600 /home/bot/weather-bot2/.env /home/bot/weather-bot2/kalshi.pk
```
`.env` and `kalshi.pk` are gitignored; they travel by scp only.

## 4. Verify the location before trading
```
su - bot -c 'cd weather-bot2 && ~/.local/bin/uv run python -m weather_bot probe'
```
Rests one minimum-size bid far below the market on each venue, cancels it, prints PASS/FAIL. A Polymarket
`FAIL ... Trading restricted in your region` means the region is blocked: destroy the droplet and pick the
other EU region. Both must PASS.

## 5. Start
```
su - bot -c 'cd weather-bot2 && ~/.local/bin/uv run python -m weather_bot enable-trading'
systemctl start weather-bot
tail -f /home/bot/weather-bot2/data/run.log
```

## Day-to-day
```
systemctl status weather-bot            # alive?
journalctl -u weather-bot -n 50         # service-level messages
su - bot -c 'cd weather-bot2 && ~/.local/bin/uv run python -m weather_bot status'
su - bot -c 'cd weather-bot2 && ~/.local/bin/uv run python -m weather_bot disable-trading'   # kill switch
cd /home/bot/weather-bot2 && sudo -u bot git pull && systemctl restart weather-bot             # deploy a fix
```
The droplet clock is UTC; scoring runs at 14:00 UTC as before.
