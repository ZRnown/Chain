## CA Filter Telegram Bot (Solana + BSC)

End-to-end Telegram CA filter bot with group listening, on-chain/K-line data aggregation, filtering, and push notifications (image + metrics). Supports Solana and BSC chains. Uses pure Bot API (no UserBot). **Disable privacy mode in BotFather** so the bot can read group messages (Group Privacy -> Turn off).

### Features

- Listen to multiple TG groups for contract addresses (CA) via Telethon UserBot.
- Aggregate data from DexScreener (price, mcap, liquidity), Birdeye (1m OHLCV K-line charts), RPC (holders/top holders), GoPlus (security), GMGN (rat/bundle ratios – requires cookie/headers), and custom RPCs.
- Configurable filters: market cap, liquidity, TGE/open time, top10 share, holder count, max holder share, 5m trades, rat-trader ratio, bundled ratio. Unset fields = no filter.
- Push to multiple destinations (groups/users/bots) with 1m K-line image and compact metrics, plus quick links (GMGN/DexScreener/scan).
- Inline settings panel to adjust thresholds and manage listen/push groups.

### Quick Start

1. Install deps:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `.env` file:
   ```bash
   cp .env.example .env
   ```
3. Fill `.env` file with your configuration (see below).
4. Run:
   ```bash
   python -m src.main
   ```

### Config & Secrets

所有配置都通过 `.env` 文件管理，无需其他配置文件。

1. **复制示例文件**：
   ```bash
   cp .env.example .env
   ```

2. **编辑 `.env` 文件**，填写以下配置：
   - `TG_BOT_TOKEN` - Telegram Bot Token（必需）
   - `ADMIN_IDS` - 管理员ID列表，逗号分隔（必需）
     - 如何获取你的Telegram用户ID：
       - 发送消息给 [@userinfobot](https://t.me/userinfobot) 获取你的用户ID
       - 或者使用 [@getidsbot](https://t.me/getidsbot) 获取
   - `GMGN_COOKIE`, `GMGN_UA` - GMGN Cookie和User-Agent（可选，用于获取老鼠仓和捆绑占比）
   - `LOG_LEVEL` - 日志级别（可选，默认：INFO）

3. **运行时配置**：监听群组、推送群组、筛选条件等通过机器人命令或按钮菜单配置，保存在 `state.json` 中。

### Files

- `main.py` — entrypoint wiring monitor + bot.
- `src/bot.py` — PTB bot for commands/settings and group listening (non-command text).
- `src/state.py` — persistent state (`state.json`) for chats/filters set via bot commands.
- `src/data_fetcher.py` — GMGN API aggregation (rat/bundle ratios, token metrics).
- `src/filters.py` — apply thresholds.
- `src/chart.py` — 1m K-line generation with mplfinance.
- `src/models.py` — data schemas.
- `src/storage.py` — In-memory dedupe store.
- `src/utils.py` — helpers.

### Notes

- GMGN endpoints are unofficial; you must supply current cookies/headers and may need a proxy. The code isolates this so you can swap in a paid source easily.
- Systemd unit example is in `deploy/systemd.service` (fill paths/user).
# Chain
