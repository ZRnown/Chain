from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import List, Optional, Tuple

from .bot import BotApp, chain_hint
from .chart import render_chart
from .client_pool import ClientPool
from .data_fetcher import DataFetcher
from .filters import apply_filters
from .models import TokenMetrics
from .state import StateStore
from .storage import DedupeStore
from .task_scheduler import TaskScheduler
from .utils import short_num, format_time_ago


def build_caption(m: TokenMetrics, filtered: Optional[List[str]] = None) -> str:
    # 辅助函数
    def fmt_num(n): 
        return short_num(n) if n is not None else "N/A"
    
    def fmt_pct(n): 
        if n is None:
            return "N/A"
        # 使用向下取整的方式保留两位小数，避免四舍五入
        try:
            val = (Decimal(str(n)) * Decimal("100")).quantize(Decimal("0.00"), rounding=ROUND_DOWN)
            return f"{val}%"
        except Exception:
            return "N/A"
    
    def fmt_int(n): 
        return str(int(n)) if n is not None else "N/A"
    
    # 1. 市值 & 池子
    mc = fmt_num(m.market_cap)
    liq = fmt_num(m.liquidity_usd)
    
    # 2. 时间（优先使用第一个K线时间，即真正的开盘时间）
    open_time = m.first_trade_at or m.pool_created_at
    age = format_time_ago(open_time) if open_time else "N/A"
    
    # 3. 交易次数
    tx_5m = fmt_int(m.trades_5m)
    
    # 4. 构建 GMGN 链接
    # 根据链类型自动生成
    chain_path = "sol" if m.chain.lower() == "solana" else m.chain.lower()
    gmgn_url = f"https://gmgn.ai/{chain_path}/token/{m.address}"
    
    # 布局构建
    # 标题行：名称 + 链接
    title_line = f"💊 <b>{m.symbol}</b> ({m.name or 'Unknown'})"
    
    # 数据矩阵 (横排密集显示)
    # 第一行：市值 | 池子 | 开盘
    line1 = f"💰市值: ${mc} | 💧池子: ${liq} | ⏰开盘: {age}"
    
    # 第二行：CA (单行方便复制)
    line2 = f"<code>{m.address}</code>"
    
    # 第三行：持有 | 前10 | 5分交易 | 最大持仓
    line3 = f"👥持有: {fmt_int(m.holders)} | 🔟Top10: {fmt_pct(m.top10_ratio)} | 📉5m交易: {tx_5m} | 🐳最大: {fmt_pct(m.max_holder_ratio)}"
    
    # 底部：链接
    line4 = f"🔗 <a href='{gmgn_url}'>点击前往 GMGN 查看详情 ↗️</a>"
    
    content = [title_line, line1, line2, line3, "-"*20, line4]
    
    if filtered:
        content.append(f"\n🚫 <b>已过滤原因:</b> {', '.join(filtered)}")
        
    return "\n".join(content)


async def main():
    # Configure detailed logging
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Keep httpx and telegram logs at WARNING to reduce noise
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)
    
    # Create logger for our application
    logger = logging.getLogger("ca_filter_bot")
    logger.setLevel(logging.DEBUG if log_level == "DEBUG" else logging.INFO)
    
    logger.info("=" * 60)
    logger.info("🚀 CA Filter Bot Starting...")
    logger.info("=" * 60)
    
    # Load environment variables
    from dotenv import load_dotenv
    load_dotenv()
    
    # Required env vars
    tg_bot_token = os.getenv("TG_BOT_TOKEN")
    if not tg_bot_token:
        raise RuntimeError("TG_BOT_TOKEN is required")
    
    # Optional env vars
    gmgn_headers = {}
    if os.getenv("GMGN_COOKIE"):
        gmgn_headers["cookie"] = os.getenv("GMGN_COOKIE")
        logger.info("✅ GMGN Cookie configured")
    if os.getenv("GMGN_UA"):
        gmgn_headers["user-agent"] = os.getenv("GMGN_UA")
        logger.info("✅ GMGN User-Agent configured")
    if not gmgn_headers:
        logger.warning("⚠️  GMGN headers not configured, may have limited access")
    
    # Birdeye API Key (required for chart data)
    birdeye_api_key = os.getenv("BIRDEYE_API_KEY")
    if not birdeye_api_key:
        logger.warning("⚠️  BIRDEYE_API_KEY not configured, chart generation will fail")
    else:
        logger.info("✅ Birdeye API Key configured")
    
    # Admin IDs
    admin_ids_str = os.getenv("ADMIN_IDS", "")
    admin_ids = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip()] if admin_ids_str else []
    if admin_ids:
        logger.info(f"✅ Admin IDs configured: {len(admin_ids)} admin(s)")
    else:
        logger.warning("⚠️  No admin IDs configured, admin features will be disabled")
    
    # Tasks config path (for MTProto clients + tasks)
    tasks_config_path = os.getenv("TASK_CONFIG_PATH", "config/tasks.json")
    
    state = StateStore("state.json", admin_ids)
    logger.info("💾 State store initialized")

    fetcher = DataFetcher(
        gmgn_headers=gmgn_headers,
        birdeye_api_key=birdeye_api_key,
    )
    logger.info("📡 DataFetcher initialized")
    
    dedupe = DedupeStore()
    logger.info("🔄 Dedupe store initialized (in-memory)")

    bot_app = BotApp(admin_ids, state, process_ca=None, scheduler=None)
    client_pool = ClientPool(tasks_config_path)
    try:
        await client_pool.load()
    except Exception as e:
        logger.warning(f"⚠️ Failed to load clients: {e}")

    async def process_ca(chain: str, ca: str, force_push: bool = False, task_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Process CA and return (photo_path, caption, error_message).
        If successful, returns (photo_path, caption, None).
        If failed, returns (None, None, error_message).
        task_id: 用于按任务独立的过滤与推送配置；若为 None 则使用当前任务或默认空配置。
        """
        # 选择任务配置
        task_id_in_use = task_id or await state.current_task()
        tasks_snap = await state.all_tasks()
        task_cfg = tasks_snap.get(task_id_in_use) if tasks_snap else None

        key = f"{task_id_in_use or 'global'}:{chain}:{ca}"
        logger.info(f"🔍 Processing CA: {chain} - {ca[:8]}... (task={task_id_in_use})")
        
        if not force_push:
            logger.debug(f"🔍 Checking dedupe for: {key[:64]}...")
            is_seen = await dedupe.seen(key)
            logger.debug(f"🔍 Dedupe check result: {is_seen}")
            if is_seen:
                logger.info(f"⏭️  CA already processed for task={task_id_in_use}, skipping: {ca[:8]}...")
                return None, None, None  # Already processed, skip silently
        
        try:
            logger.info(f"📥 Fetching data for {chain} - {ca[:8]}...")
            start_time = asyncio.get_event_loop().time()
            
            # 并行执行：获取GMGN数据 + 获取图表数据 + 获取代币信息（使用地址，不依赖metrics）
            # 注意：图表数据和代币信息获取需要address，可以在获取metrics之前就开始
            # 获取更长时间范围的K线（30天）以找到真正的开盘时间，但允许失败（使用 return_exceptions=True）
            metrics_task = fetcher.fetch_all(chain, ca)
            chart_task = fetcher.fetch_chart_by_address(chain, ca, minutes=60)  # 图表显示用60分钟
            chart_all_task = fetcher.fetch_chart_by_address(chain, ca, minutes=30*24*60)  # 获取30天的K线用于找开盘时间
            token_info_task = fetcher.fetch_token_info_from_birdeye(chain, ca)
            
            # 等待四个任务完成，允许 chart_all_task 和 token_info_task 失败（使用 return_exceptions=True）
            results = await asyncio.gather(
                metrics_task, 
                chart_task, 
                chart_all_task, 
                token_info_task,
                return_exceptions=True
            )
            metrics, bars, bars_all, token_info = results
            
            # 检查是否有异常
            if isinstance(metrics, Exception):
                raise metrics
            # bars 允许失败，如果失败则设置为 None，后续会生成 fallback 图表
            if isinstance(bars, Exception):
                logger.warning(f"⚠️ Failed to fetch chart data (60min): {bars}")
                bars = None
            # bars_all 和 token_info 允许失败，设置为 None
            if isinstance(bars_all, Exception):
                logger.warning(f"⚠️ Failed to fetch 30-day K-line data: {bars_all}")
                bars_all = None
            if isinstance(token_info, Exception):
                logger.warning(f"⚠️ Failed to fetch Birdeye token info: {token_info}")
                token_info = None
            logger.info(f"✅ Data fetched: {metrics.symbol} | Price: ${metrics.price_usd} | MCap: ${metrics.market_cap}")
            
            # 处理图表数据结果（允许为空，后续会生成 fallback 图表）
            if not bars:
                logger.warning(f"⚠️ Chart data (60min) not available, will use fallback chart")
            else:
                logger.info(f"📈 Chart data: {len(bars)} bars from Birdeye API")
            
            # 优先从 Birdeye token info 获取代币创建时间，如果没有则从第一个K线提取
            if token_info:
                # 尝试从 token_info 中获取创建时间
                creation_time = None
                # Birdeye API 可能返回的字段：created_timestamp, launch_time, first_trade_time 等
                for field in ["created_timestamp", "launch_time", "first_trade_time", "createdAt", "created_at", "firstTradeUnixTime", "firstTradeTime"]:
                    ts = token_info.get(field)
                    if ts:
                        try:
                            # 判断是秒还是毫秒时间戳
                            if ts > 1e11:
                                ts = ts / 1000
                            creation_time = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
                            logger.info(f"⏰ Token creation time from Birdeye token info: {creation_time} (field: {field})")
                            break
                        except Exception as e:
                            logger.debug(f"⚠️ Failed to parse {field}: {e}")
                            continue
                
                if creation_time:
                    metrics.first_trade_at = creation_time
            
            # 如果没有从 token_info 获取到，则从所有K线数据中提取最早的K线时间（真正的开盘时间）
            if not metrics.first_trade_at and bars_all and len(bars_all) > 0:
                # bars_all 已经按时间排序，第一个就是最早的K线（代币最开始交易的时间）
                first_bar_time = bars_all[0].get("t")
                if first_bar_time:
                    try:
                        from datetime import timezone
                        first_trade_dt = datetime.fromtimestamp(first_bar_time, tz=timezone.utc).replace(tzinfo=None)
                        # 更新 metrics 的 first_trade_at（优先使用这个作为开盘时间）
                        metrics.first_trade_at = first_trade_dt
                        logger.info(f"⏰ First trade time from Birdeye K-line (all history): {first_trade_dt} (timestamp: {first_bar_time})")
                        if metrics.pool_created_at:
                            diff_minutes = (first_trade_dt - metrics.pool_created_at).total_seconds() / 60
                            logger.debug(f"   Time difference from pool_created_at: {diff_minutes:.1f} minutes")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to parse first trade time: {e}")
            
            # 过滤检查
            filters_cfg = await state.filters_cfg(task_id=task_id_in_use)
            passed, reasons = apply_filters(metrics, filters_cfg)
            logger.info(f"🔍 Filter check: {'✅ PASSED' if passed else '❌ FAILED'}")
            if reasons:
                logger.info(f"   Reasons: {', '.join(reasons)}")
            
            elapsed = asyncio.get_event_loop().time() - start_time
            logger.info(f"⏱️  Total processing time: {elapsed:.2f}s")
            
        except ValueError as e:
            # Specific error messages
            error_msg = str(e)
            if "No pairs found" in error_msg:
                error_detail = (
                    f"**数据获取失败**\n\n"
                    f"已尝试从以下数据源获取：\n"
                    f"• GMGN API（优先，支持新代币）\n"
                    f"• DexScreener API（备选）\n\n"
                    f"**可能原因：**\n"
                    f"• 代币非常新，数据源尚未同步\n"
                    f"• 合约地址错误\n"
                    f"• 代币尚未创建交易对\n\n"
                    f"💡 提示：如果代币来自GMGN且刚创建，请稍等几分钟后再试"
                )
                logging.warning("数据源未找到代币 %s %s", chain, ca)
            else:
                error_detail = f"数据获取失败: {error_msg}"
                logging.warning("数据获取失败 %s %s: %s", chain, ca, error_msg)
            return None, None, error_detail
        except Exception as e:
            error_detail = f"数据获取失败: {str(e)}"
            logging.warning("fetch failed %s %s: %s", chain, ca, e)
            return None, None, error_detail
        
        caption = build_caption(metrics, None if passed else reasons)

        # 生成图表（如果 Birdeye API 失败，使用 fallback 图表）
        logger.info(f"📸 Generating chart for {ca[:8]}...")
        photo_buffer = None
        try:
            if bars and len(bars) > 0:
                photo_buffer = render_chart(metrics, bars)
                if photo_buffer:
                    logger.info(f"✅ Chart generated from Birdeye data")
        except Exception as e:
            logger.warning(f"⚠️ Chart generation failed: {e}")
        
        # 如果图表生成失败或没有数据，使用 fallback
        if not photo_buffer:
            logger.warning(f"⚠️ No K-line data available, using fallback chart")
            try:
                from .chart import _generate_fallback_chart
                df_fallback = _generate_fallback_chart(metrics)
                # 将 DataFrame 转换为 bars 格式
                bars_fallback = []
                for idx, row in df_fallback.iterrows():
                    # 处理时区：如果索引有时区，转换为 UTC 时间戳
                    ts = idx
                    if hasattr(ts, 'timestamp'):
                        ts_value = int(ts.timestamp())
                    else:
                        from pandas import Timestamp
                        ts_value = int(Timestamp(ts).timestamp())
                    bars_fallback.append({
                        "t": ts_value,
                        "o": float(row["Open"]),
                        "h": float(row["High"]),
                        "l": float(row["Low"]),
                        "c": float(row["Close"]),
                        "v": float(row.get("Volume", 0))
                    })
                photo_buffer = render_chart(metrics, bars_fallback)
                if photo_buffer:
                    logger.info(f"✅ Fallback chart generated")
                else:
                    raise ValueError("Fallback chart generation failed")
            except Exception as e2:
                error_msg = f"图表生成失败: {str(e2)}"
                logger.error(f"❌ {error_msg}")
                raise ValueError(error_msg)
        
        # If force_push (manual query), always return result to user
        if force_push:
            if not passed:
                # 转义 HTML 特殊字符，避免解析错误
                escaped_reasons = [html.escape(r) for r in reasons]
                error_msg = f"代币未通过筛选条件：\n" + "\n".join(f"• {r}" for r in escaped_reasons)
                return photo_buffer, caption, error_msg
            # Return photo and caption for manual query (even if no push targets)
            return photo_buffer, caption, None
        
        # Auto mode: only push if passed filters
        if passed:
            targets = []
            if task_cfg:
                targets = task_cfg.get("push_chats", [])
            logger.info(f"📤 Pushing to {len(targets)} target(s): {targets}")
            if targets:
                # 获取一个可用的 MTProto 客户端（用于发送到机器人）
                mtproto_client = None
                if client_pool.clients:
                    # 使用第一个可用的客户端
                    mtproto_client = list(client_pool.clients.values())[0]
                
                for chat_id in targets:
                    try:
                        # 判断是机器人（@username）还是群组/频道（数字ID）
                        is_bot = isinstance(chat_id, str) and chat_id.startswith("@")
                        
                        if is_bot:
                            # 机器人：使用 MTProto 客户端
                            if not mtproto_client:
                                logger.warning(f"⚠️  No MTProto client available, cannot send to bot {chat_id}")
                                continue
                            
                            payload = ca  # 对机器人仅发送 CA 地址
                            if photo_buffer:
                                photo_buffer.seek(0)
                                await mtproto_client.send_file(
                                    chat_id, 
                                    photo_buffer, 
                                    caption=payload,
                                    parse_mode="html"
                                )
                            else:
                                await mtproto_client.send_message(
                                    chat_id, 
                                    payload,
                                    parse_mode="html"
                                )
                            logger.info(f"✅ Sent to bot {chat_id} via MTProto")
                        else:
                            # 群组/频道：使用 Bot API
                            if photo_buffer:
                                photo_buffer.seek(0)
                                await bot_app.app.bot.send_photo(
                                    chat_id=chat_id, 
                                    photo=photo_buffer, 
                                    caption=caption,
                                    parse_mode="HTML"
                                )
                            else:
                                await bot_app.app.bot.send_message(
                                    chat_id=chat_id, 
                                    text=caption,
                                    parse_mode="HTML"
                                )
                            logger.info(f"✅ Sent to chat {chat_id} via Bot API")
                    except Exception as e:
                        logger.error(f"❌ Failed to send to chat {chat_id}: {e}")
            else:
                logger.warning(f"⚠️  No push targets configured, skipping auto push")
        else:
            logger.info(f"⏭️  Token filtered out, not pushing")
        
        return photo_buffer, caption, None

    # inject process_ca now that it is defined
    bot_app.process_ca = process_ca
    
    # start scheduler if tasks are configured
    scheduler = TaskScheduler(client_pool, process_ca)
    scheduler.load_tasks(client_pool.tasks_config())
    if scheduler.tasks:
        await scheduler.start()
        bot_app.scheduler = scheduler
        logger.info(f"🗓️  Task scheduler started with {len(scheduler.tasks)} task(s)")
    else:
        logger.info("🗓️  No tasks configured; scheduler not started")
    
    snap = await state.snapshot()
    logger.info("=" * 60)
    logger.info("📊 Current Configuration:")
    logger.info(f"   Listen chats: {len(snap.get('listen_chats', []))} groups")
    logger.info(f"   Push chats: {len(snap.get('push_chats', []))} groups")
    logger.info(f"   Filters: {sum(1 for f in snap.get('filters', {}).values() if f.get('min') is not None or f.get('max') is not None)} configured")
    logger.info("=" * 60)
    logger.info("✅ Bot ready! Waiting for messages...")
    logger.info("=" * 60)
    
    await bot_app.run()


if __name__ == "__main__":
    asyncio.run(main())

