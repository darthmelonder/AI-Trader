# AI-Trader Personal Agent

Scans NASDAQ top-50 stocks hourly using the [ai4trade.ai](https://ai4trade.ai) platform's market intelligence (macro regime, technical signals, news sentiment) and publishes virtual trades when all entry criteria pass. Sends push notifications via ntfy.sh so you can execute manually on your real broker.

**Strategy 1 — Momentum + Macro Alignment:** enters when macro is bullish, stock signal is buy/bullish trend, both 5d and 20d returns are positive, price is within 30% of support, and no bearish news. Exits on stop-loss (8%), signal flip, macro flip, or 90-day age limit.

**Strategy 2 — LLM-Guided Swing:** targets 1–4 week holds. Pre-gates on RSI (35–65), above 50d MA, and earnings blackout (5d). Passes qualifying symbols to Gemini with platform signals, yfinance fundamentals, RSI/MACD (computed locally), and FRED macro data. Only enters if Gemini returns `buy` with confidence ≥ 0.70. Exits on stop-loss, profit target, 30-day age limit, or daily LLM re-evaluation turning negative.

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

`setup.sh` installs Docker if missing, walks you through env configuration interactively, and starts the agent(s) as background containers that auto-restart on crash or reboot.

During setup you'll be asked whether you also want to run the **LLM Swing agent**. Answer `y` to configure and launch both agents as separate leaderboard entries, or `n` to run only Strategy 1.

### Docker commands

```bash
# Logs
docker compose logs -f                  # all agents, live
docker compose logs -f agent            # Strategy 1 only
docker compose logs -f agent-swing      # Strategy 2 only

# Restart after editing an env file
docker compose restart agent
docker compose restart agent-swing

# Stop
docker compose down                     # stop Strategy 1 only
docker compose --profile swing down     # stop both

# Start both agents (after initial setup)
docker compose --profile swing up -d
```

### Starting the swing agent later

If you ran `setup.sh` and only set up Strategy 1, you can add Strategy 2 any time:

```bash
cp .env.swing.example .env.swing
nano .env.swing          # fill in AGENT_NAME, AGENT_PASSWORD, GEMINI_API_KEY, FRED_API_KEY
echo '{"token":null,"agent_id":null}' > credentials.swing.json
docker compose --profile swing up -d --build
```

### Credentials files and your agent accounts

Each agent has its own credentials file that holds its platform token and ID:

| File | Agent |
|---|---|
| `credentials.json` | Strategy 1 (`agent`) |
| `credentials.swing.json` | Strategy 2 (`agent-swing`) |

Both files are created as empty placeholders by `setup.sh`. On first start each agent registers a new account on ai4trade.ai, saves the real token, and the Docker volume keeps it across restarts.

**Migrating an existing local account to the VM:**

```bash
# From your local machine
scp agent/credentials.json user@your-vm:~/AI-Trader/agent/credentials.json
scp agent/credentials.swing.json user@your-vm:~/AI-Trader/agent/credentials.swing.json  # if applicable

# Then on the VM
cd AI-Trader/agent/
./setup.sh
```

> **Never commit credentials files** — they're in `.gitignore`. Treat them like password files.

---

## CLI flags

| Flag | Effect |
|---|---|
| `--env <file>` | Load a specific env file instead of `.env` (enables multiple independent agents). |
| `--dry-run` | Suppress all write API calls. GET reads still happen so you see real data. |
| `--scan-now` | Run one scan immediately then exit (useful for testing). |
| `--force-scan` | Run the entry scan even when the market is closed. Use with `--scan-now` for testing. |
| `--verbose` | Show per-symbol gate debug logs — see exactly why each stock passes or fails. |

---

## Notifications

When a signal fires you get an ntfy push notification:

- **Entry** — `BUY NVDA: 15 shares @ ~$875 | macro=bullish, signal=buy/bullish, 5d=+3.2%`
- **Exit (stop-loss)** — urgent priority, bypasses Do Not Disturb
- **Exit (signal/macro flip)** — high priority

Subscribe to your topic in the [ntfy app](https://ntfy.sh) (iOS / Android / desktop). No account needed.

---

## Configuration reference

### Base (all agents — `.env` / `.env.swing`)

| Variable | Default | Description |
|---|---|---|
| `AGENT_NAME` | — | Display name on ai4trade.ai leaderboard |
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

### Strategy 2 — LLM Swing (`.env.swing` only)

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | — | Gemini API key (free at aistudio.google.com) |
| `FRED_API_KEY` | — | FRED API key for macro data (free) |
| `S2_MIN_LLM_CONFIDENCE` | `0.70` | Minimum Gemini confidence score to open a position |
| `S2_MAX_POSITIONS` | `3` | Max open positions for this strategy |
| `S2_POSITION_SIZE_PCT` | `15.0` | % of cash per trade |
| `S2_MAX_HOLD_DAYS` | `30` | Force-exit after this many days |
| `S2_LLM_CACHE_TTL_HOURS` | `24` | Hours before Gemini re-evaluates the same symbol |
| `S2_EARNINGS_BLACKOUT_DAYS` | `5` | Skip entry if earnings within this many days |
| `S2_STOP_LOSS_PCT` | `9.0` | Stop-loss % |
| `S2_TARGET_RETURN_PCT` | `12.0` | Default profit target % (overridden by Gemini's suggestion) |

---

## Running a separate agent per strategy

Running two strategies inside the same agent shares a single cash pool, position slots, and leaderboard entry — you can't cleanly attribute P&L to a strategy. The recommended approach is to run each strategy as its own independent agent process, each with its own platform identity and virtual $100k account.

### How it works

The `--env <file>` flag loads a different env file, which controls the agent's identity (`AGENT_NAME`, `AGENT_EMAIL`, `AGENT_PASSWORD`) and which strategies it runs (`ACTIVE_STRATEGIES`). Each env file gets its own credentials file derived automatically:

| Env file | Credentials file | Agent identity |
|---|---|---|
| `.env` | `credentials.json` | `JatinMomentumBot` — Strategy 1 |
| `.env.swing` | `credentials.swing.json` | `JatinSwingBot` — Strategy 2 |

### Setting up the LLM Swing agent

**1. Create the env file**

```bash
cp .env.swing.example .env.swing
```

Open `.env.swing` and fill in:
- `AGENT_NAME` / `AGENT_EMAIL` / `AGENT_PASSWORD` — a different identity from your Strategy 1 agent
- `GEMINI_API_KEY` — free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
- `FRED_API_KEY` — free at [fredaccount.stlouisfed.org/apikeys](https://fredaccount.stlouisfed.org/apikeys)

**2. Install Strategy 2 dependencies** (once)

```bash
pip install google-genai yfinance fredapi pandas
```

**3. Test with a dry run**

```bash
# Runs the full scan right now regardless of market hours, logs decisions, posts nothing
python main.py --env .env.swing --scan-now --force-scan --verbose
```

You should see lines like:
```
NVDA: Gemini -> decision=buy confidence=0.75 horizon=17d target=+12.0% stop=-8.0%
TSLA SKIP [llm_swing] LLM decision=skip confidence=0.65
AMD  SKIP [llm_swing] gate=rsi-overbought RSI=79.9>65.0
```

**4. Go live**

```bash
# Terminal 1 — Momentum Macro agent
python main.py

# Terminal 2 — LLM Swing agent (separate identity, separate $100k account)
python main.py --env .env.swing
```

Both agents appear independently on the ai4trade.ai leaderboard. After several weeks you can compare their virtual P&L directly.

**On a VM**, run both in the background:

```bash
nohup python main.py > agent_momentum.log 2>&1 &
nohup python main.py --env .env.swing > agent_swing.log 2>&1 &
```

### Adding your own strategy

1. Create `strategies/your_strategy.py` implementing the `Strategy` ABC from `strategies/base.py`
   - If your strategy needs config/API keys at construction, add `NEEDS_CONFIG = True` as a class attribute and accept `config: dict` in `__init__`
2. Register it in `strategies/__init__.py`
3. Create a dedicated `.env.yourstrategy` with `ACTIVE_STRATEGIES=your_strategy` and a unique `AGENT_NAME`
4. Run: `python main.py --env .env.yourstrategy`

---

## Files

```
agent/
├── main.py                         # Entry point — bootstrap, main loop, --env dispatch
├── client.py                       # All ai4trade.ai API calls
├── scanner.py                      # Scan orchestration, position book, market hours gate
├── notifier.py                     # ntfy.sh push + optional Discord alerts
├── llm_analyst.py                  # Gemini wrapper with 24h cache + 429 retry logic
├── strategies/
│   ├── base.py                     # Strategy ABC + EntrySignal / ExitSignal dataclasses
│   ├── momentum_macro.py           # Strategy 1 — rule-based momentum
│   ├── llm_swing.py                # Strategy 2 — LLM-guided swing
│   └── __init__.py                 # Strategy registry
├── data_sources/
│   ├── yfinance_source.py          # Fundamentals + RSI/MACD computed from price history
│   └── fred_source.py              # FRED macro data (rates, CPI) — cached 24h
├── Dockerfile
├── docker-compose.yml
├── setup.sh                        # One-command VM setup
├── requirements.txt
├── .env.example                    # Template for Strategy 1 agent
└── .env.swing.example              # Template for Strategy 2 (LLM Swing) agent
```
