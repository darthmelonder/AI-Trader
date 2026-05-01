# AI-Trader Personal Agent

Scans NASDAQ top-50 stocks hourly using the [ai4trade.ai](https://ai4trade.ai) platform's market intelligence (macro regime, technical signals, news sentiment) and publishes virtual trades when all entry criteria pass. Sends push notifications via ntfy.sh so you can execute manually on your real broker.

**Strategy 1 — Momentum + Macro Alignment:** enters when macro is bullish, stock signal is buy/bullish trend, both 5d and 20d returns are positive, price is within 30% of support, and no bearish news. Exits on stop-loss (8%), signal flip, macro flip, or 90-day age limit.

---

## Quick start (local Mac)

**1. Install dependencies**

```bash
cd agent/
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**2. Configure**

```bash
cp .env.example .env
```

Open `.env` and set at minimum:
- `AGENT_PASSWORD` — choose any strong password (used to register your agent account)
- `NTFY_TOPIC` — a unique name like `jatin-nasdaq-bot` (install the [ntfy app](https://ntfy.sh) on your phone and subscribe to this topic)
- `DRY_RUN=false` — flip this when you're ready to go live (leave as `true` to test first)

**3. Run**

```bash
# Test first — reads real market data, logs what would happen, posts nothing
python main.py --dry-run --scan-now --verbose

# Go live — registers your agent account, starts scanning every hour
python main.py
```

On first live run the agent registers itself on ai4trade.ai and saves credentials to `credentials.json`. **Do not delete that file** — it's your agent's identity and simulated $100k account.

To keep it running after closing the terminal:

```bash
nohup python main.py > agent.log 2>&1 &
tail -f agent.log
```

---

## VM deployment (one command)

On any Linux VM after cloning the repo:

```bash
cd AI-Trader/agent/
./setup.sh
```

`setup.sh` installs Docker if missing, walks you through `.env` configuration, and starts the agent as a background container that auto-restarts on crash or reboot.

After that:

```bash
docker compose logs -f           # watch live logs
docker compose restart agent     # apply .env changes
docker compose down              # stop
```

### credentials.json and your agent account

`credentials.json` holds your agent's token and ID on ai4trade.ai. There are two scenarios:

**Fresh deploy — new agent account (default)**

Do nothing. `setup.sh` creates an empty placeholder. On first start the agent registers a new account, saves the real credentials, and the Docker volume keeps them across container restarts.

**Migrating an existing account to the VM**

If you already ran the agent locally and want to continue with the same account and trading history, copy your local `credentials.json` to the VM before running `setup.sh`:

```bash
# From your local machine
scp agent/credentials.json user@your-vm:~/AI-Trader/agent/credentials.json

# Then on the VM
cd AI-Trader/agent/
./setup.sh
```

The agent will read the existing token and pick up where it left off.

> **Never commit `credentials.json`** — it's in `.gitignore`. Treat it like a password file.

---

## CLI flags

| Flag | Effect |
|---|---|
| `--dry-run` | Suppress all write API calls. GET reads still happen so you see real data. |
| `--scan-now` | Run one scan immediately then exit (useful for testing). |
| `--verbose` | Show per-symbol gate debug logs — see exactly why each stock passes or fails. |

---

## Notifications

When a signal fires you get an ntfy push notification:

- **Entry** — `BUY NVDA: 15 shares @ ~$875 | macro=bullish, signal=buy/bullish, 5d=+3.2%`
- **Exit (stop-loss)** — urgent priority, bypasses Do Not Disturb
- **Exit (signal/macro flip)** — high priority

Subscribe to your topic in the [ntfy app](https://ntfy.sh) (iOS / Android / desktop). No account needed.

---

## Configuration reference (`.env`)

| Variable | Default | Description |
|---|---|---|
| `AGENT_NAME` | `JatinMomentumBot` | Display name on ai4trade.ai |
| `AGENT_EMAIL` | — | Email for account registration |
| `AGENT_PASSWORD` | — | Password for account registration |
| `BASE_URL` | `https://ai4trade.ai/api` | Platform API base URL |
| `NTFY_TOPIC` | — | Your ntfy.sh topic name |
| `DISCORD_WEBHOOK_URL` | *(blank)* | Optional Discord webhook |
| `STOP_LOSS_PCT` | `8.0` | Exit if position drops this % from entry |
| `MAX_POSITIONS` | `6` | Max open positions at once |
| `POSITION_SIZE_PCT` | `20.0` | % of remaining cash per trade |
| `MAX_POSITION_AGE_DAYS` | `90` | Force-exit after this many days |
| `SCAN_INTERVAL_SECONDS` | `3600` | How often to run the full scan |
| `HEARTBEAT_INTERVAL_SECONDS` | `60` | Platform heartbeat poll interval |
| `DRY_RUN` | `true` | Set to `false` to go live |
| `ACTIVE_STRATEGIES` | `momentum_macro` | Comma-separated list of active strategies |

---

## Adding a second strategy

1. Create `strategies/your_strategy.py` implementing the `Strategy` ABC from `strategies/base.py`
2. Register it in `strategies/__init__.py`
3. Add its name to `ACTIVE_STRATEGIES` in `.env` (e.g. `momentum_macro,your_strategy`)

Each trade includes the strategy name in its content string so you can filter and compare performance on the platform via the signal feed.

---

## Files

```
agent/
├── main.py                     # Entry point — bootstrap, main loop
├── client.py                   # All ai4trade.ai API calls
├── scanner.py                  # Scan orchestration, position book, market hours gate
├── notifier.py                 # ntfy.sh push + optional Discord alerts
├── strategies/
│   ├── base.py                 # Strategy ABC + EntrySignal / ExitSignal dataclasses
│   ├── momentum_macro.py       # Strategy 1
│   └── __init__.py             # Strategy registry
├── Dockerfile
├── docker-compose.yml
├── setup.sh                    # One-command VM setup
├── requirements.txt
└── .env.example
```
