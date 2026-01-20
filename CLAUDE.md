# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Telegram bot for cryptocurrency token filtering and monitoring. It monitors Solana/BSC/ETH tokens from multiple data sources (GMGN, DexScreener, GeckoTerminal), applies configurable filters, generates price charts, and pushes notifications to configured Telegram chats.

**Key Features:**
- Multi-source token data fetching with automatic fallback
- Configurable filtering by market cap, liquidity, holder distribution, risk scores, etc.
- Task-based monitoring with scheduled execution and time windows
- Dual Telegram integration: Bot API + MTProto (Telethon) for listening to other bots
- Chart generation with matplotlib/mplfinance
- Redis-based deduplication to prevent duplicate notifications

## Development Commands

### Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python -m src.main
```

### Environment Setup

Required environment variables in `.env`:

```bash
# Telegram Bot API
TG_BOT_TOKEN=your_bot_token

# Admin user IDs (comma-separated)
ADMIN_IDS=123456789,987654321

# Telegram MTProto (for client pool)
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash

# GMGN API (optional, for better data access)
GMGN_COOKIE=your_cookie
GMGN_UA=your_user_agent

# Risk scoring APIs (optional)
SOL_SNIFFER_API_KEY=your_key
TOKEN_SNIFFER_API_KEY=your_key

# Logging
LOG_LEVEL=INFO  # or DEBUG
```

### Configuration Files

- `config/tasks.json`: MTProto clients and scheduled tasks configuration
- `state.json`: Bot state (tasks, filters, listen/push chats) - auto-generated
- `.env`: Environment variables (not committed to git)

## Architecture Overview

### Core Components

**1. Data Fetching Layer** (`src/data_fetcher.py`, `src/gmgn_basic.py`)
- Multi-source data fetching with priority: GMGN (basic) → GMGN (full) → DexScreener
- Chart data from GeckoTerminal API (1-minute OHLCV bars)
- Risk scoring from SolSniffer and TokenSniffer APIs
- Automatic retry with exponential backoff
- Browser fingerprint rotation for Cloudflare bypass (curl_cffi)

**2. Bot Layer** (`src/bot.py`)
- Telegram Bot API integration for admin commands
- CA (Contract Address) extraction from messages using regex
- Admin commands: `/start`, `/status`, `/listen`, `/push`, `/filter`, `/task`, etc.
- Inline keyboard for interactive configuration

**3. MTProto Layer** (`src/client_pool.py`, `src/task_scheduler.py`)
- Telethon-based MTProto clients for listening to group messages
- **Critical**: MTProto can see messages from other bots (Bot API cannot)
- Task scheduler with interval-based execution and time windows (UTC+8)
- Automatic task enable/disable based on time windows

**4. State Management** (`src/state.py`, `src/storage.py`)
- JSON-based state persistence in `state.json`
- Multi-task configuration support (each task has its own filters, listen/push chats)
- Redis-based deduplication with 24-hour TTL
- Async file I/O with locking

**5. Filtering System** (`src/filters.py`, `src/models.py`)
- Configurable filters: market_cap, liquidity, open_minutes, top10_ratio, holder_count, max_holder_ratio, trades_5m, risk scores
- Per-task filter configurations
- Range-based filtering (min/max values)

**6. Chart Generation** (`src/chart.py`)
- Price chart visualization using matplotlib and mplfinance
- 1-hour candlestick charts with volume
- Returns BytesIO buffer for Telegram photo upload

### Data Flow

```
1. Message arrives in monitored group
   ↓
2. MTProto client or Bot API extracts CA (Contract Address)
   ↓
3. DataFetcher fetches token metrics from GMGN/DexScreener
   ↓
4. GeckoTerminal provides 1-hour OHLCV chart data
   ↓
5. Risk scores fetched from SolSniffer/TokenSniffer
   ↓
6. Filters applied based on task configuration
   ↓
7. Chart generated if data available
   ↓
8. Notification pushed to configured chats (if passed filters)
```

### Task System Architecture

The bot supports multiple independent monitoring tasks, each with:
- **Unique ID**: Task identifier
- **Listen chats**: Groups/channels to monitor for CAs
- **Push chats**: Targets to send notifications (groups, channels, or bots)
- **Filters**: Independent filter configuration per task
- **Time windows**: Optional start_time/end_time (HH:MM format, UTC+8)
- **Scheduled execution**: For periodic CA checks (via task_scheduler)

**State synchronization:**
- `state.json` stores task configurations (managed by Bot API commands)
- `config/tasks.json` stores MTProto clients and scheduled tasks
- TaskScheduler watches `state.json` for changes and syncs time windows/enabled status

### MTProto vs Bot API

**MTProto (Telethon) - `client_pool.py`:**
- Can listen to messages from other bots in groups
- Used for monitoring groups where other bots post CAs
- Requires session files or session strings
- Configured in `config/tasks.json`

**Bot API - `bot.py`:**
- Admin interface for configuration
- Cannot see messages from other bots
- Used for sending notifications to groups/channels
- Configured via `TG_BOT_TOKEN` environment variable

**When to send to bots vs groups:**
- Bots (targets starting with `@`): Send only CA text via MTProto
- Groups/channels (numeric IDs): Send photo + caption via Bot API

## Key Implementation Details

### CA Pattern Matching

```python
CA_PATTERN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40}")
```
Matches Solana addresses (base58, 32-44 chars) and Ethereum addresses (0x + 40 hex chars).

### Chain Detection

```python
def chain_hint(ca: str) -> str:
    if ca.startswith("0x"):
        return "ethereum"  # or "bsc" based on context
    return "solana"
```

### Deduplication Strategy

- Key format: `{task_id}:{chain}:{ca}`
- TTL: 86400 seconds (24 hours)
- Prevents duplicate notifications for the same token within 24 hours per task

### Time Window Logic

- Time windows use UTC+8 (China timezone)
- Format: "HH:MM" (e.g., "09:00", "18:30")
- Supports cross-day windows (e.g., start="22:00", end="06:00")
- Tasks auto-enable/disable based on current time
- Scheduler checks time windows every 3 seconds

### Data Source Priority

1. **GMGN Basic API** (fast, tls_client with retry)
2. **GMGN Full API** (curl_cffi with browser fingerprints)
3. **DexScreener API** (fallback)

Chart data always from **GeckoTerminal** (1-minute OHLCV, last 60 bars).

### Filter Application

Filters are applied in `src/filters.py:apply_filters()`:
- Numeric ranges: market_cap, liquidity, holder_count, trades_5m, risk scores
- Ratio ranges: top10_ratio, max_holder_ratio (stored as decimals, e.g., 0.082 = 8.2%)
- Time-based: open_minutes (calculated from first_trade_at or pool_created_at)

**Important**: `first_trade_at` (from K-line data) is preferred over `pool_created_at` for accurate open time.

## Common Development Patterns

### Adding a New Filter

1. Add field to `FilterConfig` in `src/models.py`
2. Add field to `TokenMetrics` if needed
3. Update `_filters_to_dict()` and `_filters_from_dict()` in `src/state.py`
4. Add filter check in `src/filters.py:apply_filters()`
5. Update bot command handlers in `src/bot.py` to support new filter

### Adding a New Data Source

1. Create fetcher method in `src/data_fetcher.py`
2. Add to fallback chain in `fetch_all()`
3. Map response to `TokenMetrics` model
4. Handle errors and return None on failure

### Adding a New Bot Command

1. Add command handler in `src/bot.py:BotApp`
2. Register handler in `__init__()` with `self.app.add_handler()`
3. Add admin check if needed: `if update.effective_user.id not in self.admin_ids`
4. Update bot commands list in `cmd_start()`

### Working with MTProto Clients

```python
# Add client via bot command
/add_client <session_string_or_file_path>

# Client auto-named by username or user_id
# Stored in config/tasks.json
```

Session types:
- **File**: Path to `.session` file (e.g., `sessions/my_account.session`)
- **String**: Telethon StringSession (long base64 string)

## Testing and Debugging

### Enable Debug Logging

```bash
LOG_LEVEL=DEBUG python -m src.main
```

### Test CA Processing

Use `/ca <chain> <address>` command in bot to manually test token processing without filters.

### Check Task Status

Use `/tasks` command to see all tasks and their configurations.

### Monitor Scheduler

Scheduler logs task execution with timestamps in UTC+8:
```
⏰ Task task_id next run: 2026-01-18 14:30:00 CST+0800
▶️ Task task_id running at 2026-01-18 14:30:00 CST+0800
```

### Common Issues

**GMGN 403/429 errors:**
- Ensure `GMGN_COOKIE` and `GMGN_UA` are set
- Fetcher automatically rotates browser fingerprints
- Falls back to DexScreener if all attempts fail

**MTProto client not seeing messages:**
- Verify client is connected: check logs for "✅ Client started"
- Ensure chat is in task's `listen_chats` list
- Check if chat ID matches (Bot API uses -100 prefix for supergroups)

**Charts not generating:**
- GeckoTerminal API may not have data for very new tokens
- Check logs for "GeckoTerminal: no pools found"
- Ensure token has at least 1 hour of trading history

**Time windows not working:**
- Verify format is "HH:MM" (24-hour format)
- Check timezone is UTC+8 (China time)
- Look for auto-enable/disable logs in scheduler

## File Structure

```
src/
├── main.py              # Entry point, orchestrates all components
├── bot.py               # Telegram Bot API, admin commands
├── data_fetcher.py      # Multi-source data fetching
├── gmgn_basic.py        # GMGN basic API client
├── client_pool.py       # MTProto client management
├── task_scheduler.py    # Scheduled task execution
├── state.py             # State management and persistence
├── storage.py           # Redis deduplication
├── filters.py           # Filter application logic
├── models.py            # Pydantic data models
├── chart.py             # Chart generation
├── utils.py             # Utility functions
└── solana_analyzer.py   # Solana-specific analysis (if used)

config/
└── tasks.json           # MTProto clients and scheduled tasks

state.json               # Bot state (auto-generated)
requirements.txt         # Python dependencies
```

## Important Notes

- **Never commit** `.env`, `state.json`, or session files to git
- **Admin IDs** must be configured for bot commands to work
- **MTProto clients** are required to listen to messages from other bots
- **Time windows** use UTC+8 timezone (China Standard Time)
- **Deduplication** is per-task with 24-hour TTL
- **Chart data** requires at least 1 hour of trading history
- **Risk scores** are optional (APIs may not be configured)
