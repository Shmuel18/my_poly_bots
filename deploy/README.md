# VPS Deployment

## TL;DR — one command on a fresh Ubuntu VPS

```bash
curl -fsSL https://raw.githubusercontent.com/Shmuel18/my_poly_bots/main/deploy/vps_setup.sh | bash
```

(Or clone and run locally: `git clone … && bash my_poly_bots/deploy/vps_setup.sh`.)

After setup completes, put your keys in `config/.env`, then install the service:

```bash
sudo bash ~/my_poly_bots/deploy/install_service.sh
```

The bot now auto-starts on reboot and restarts on failure.

## Files

| File | Purpose |
|---|---|
| [vps_setup.sh](vps_setup.sh) | Install system deps, clone/pull repo, venv, pip install, validate `.env` |
| [polybot.service](polybot.service) | systemd unit template |
| [install_service.sh](install_service.sh) | Installs `polybot.service` into `/etc/systemd/system` with the correct user/home |

## Required env vars in `config/.env`

- `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`
- `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER_ADDRESS`
- `GEMINI_API_KEY` — **required for pair discovery** (free from [aistudio.google.com/apikey](https://aistudio.google.com/apikey))
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional; without them the bot runs with probe-only sizing and never scales up

## Operating the bot

```bash
journalctl -u polybot -f          # live logs
sudo systemctl restart polybot     # after a code pull
sudo systemctl stop polybot        # stop
```

## Updating

```bash
cd ~/my_poly_bots
git pull origin main
sudo systemctl restart polybot
```

## Important log lines to watch on first run

- `💰 Balance: $XX.XX USDC` → wallet connected
- `🤖 LLM Agent enabled: gemini-2.0-flash` → Gemini key valid
- `📦 Discovery: Markets 0-100 / 5000` → scanning started
- `✨ New pair (conf=0.95)` → Gemini found a candidate
- `🧪 Probe opened for pair_key=…` → first live trade
