from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import List, Optional, Tuple

from telethon import events

from .bot import BotApp, chain_hint, CA_PATTERN
from .chart import render_chart
from .client_pool import ClientPool
from .data_fetcher import DataFetcher
from .filters import apply_filters, apply_basic_filters, apply_risk_filters, need_risk_check
from .models import TokenMetrics
from .state import StateStore
from .storage import DedupeStore
from .task_scheduler import TaskScheduler
from .utils import short_num, format_time_ago


def build_caption(m: TokenMetrics, filtered: Optional[List[str]] = None) -> str:
    # 辅助函数
    def fmt_num(n): 
        return short_num(n) if n is not None else "N/A"
    
    def fmt_pct(n, precision=2): 
        """
        格式化百分比
        precision: 小数位数，默认2位。对于最大持仓占比，使用1位（精确到0.1）
        """
        if n is None:
            return "N/A"
        # 使用向下取整的方式保留指定小数位数，避免四舍五入
        try:
            val = Decimal(str(n)) * Decimal("100")
            # 根据precision参数决定小数位数
            if precision == 1:
                val = val.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
            else:
                val = val.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
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
    line3 = f"👥持有: {fmt_int(m.holders)} | 🔟Top10: {fmt_pct(m.top10_ratio)} | 📉5m交易: {tx_5m} | 🐳最大: {fmt_pct(m.max_holder_ratio, precision=1)}"

    # 第四行：风险评分 (SolSniffer | TokenSniffer)
    sol_score = f"{m.sol_sniffer_score:.1f}" if m.sol_sniffer_score is not None else "N/A"
    token_score = f"{m.token_sniffer_score:.1f}" if m.token_sniffer_score is not None else "N/A"
    line4 = f"🛡️风险评分: SolSniffer {sol_score} | TokenSniffer {token_score}"

    # 第五行：链接
    line5 = f"🔗 <a href='{gmgn_url}'>点击前往 GMGN 查看详情 ↗️</a>"
    
    content = [title_line, line1, line2, line3, line4, line5]
    
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
        get_api_key=state.get_api_key,  # 传入获取 API Key 的回调函数
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
            # 设置去重时间为1天（86400秒），防止重复推送
            is_seen = await dedupe.seen(key, ttl=86400)
            logger.debug(f"🔍 Dedupe check result: {is_seen}")
            if is_seen:
                logger.info(f"⏭️  CA already processed for task={task_id_in_use}, skipping: {ca[:8]}...")
                return None, None, None  # Already processed, skip silently
        
        try:
            logger.info(f"📥 Fetching data for {chain} - {ca[:8]}...")
            start_time = asyncio.get_event_loop().time()
            
            # GMGN 基础数据 + GeckoTerminal K线
            metrics_task = asyncio.create_task(fetcher.fetch_all(chain, ca))
            
            # GeckoTerminal：1小时 1m K 线
            try:
                bars = await fetcher.fetch_chart_by_address(chain, ca, minutes=60)
            except Exception as e:
                error_detail = f"图表数据获取失败（GeckoTerminal API 失败）: {str(e)}"
                logger.error(error_detail)
                logger.debug(f"GeckoTerminal error details:", exc_info=True)
                return None, None, error_detail

            # 等待 GMGN 数据
            metrics = await metrics_task
            
            # 检查是否有异常
            if isinstance(metrics, Exception):
                raise metrics
            if not bars:
                error_detail = "图表数据为空（未返回 60 分钟 1m K 线），已停止推送"
                logger.error(error_detail)
                return None, None, error_detail
            logger.info(f"✅ Data fetched: {metrics.symbol} | Price: ${metrics.price_usd} | MCap: ${metrics.market_cap}")
            logger.info(f"📈 Chart data: {len(bars)} bars from GeckoTerminal")
            
            # 使用 K 线的第一根时间作为开盘时间
            if bars and len(bars) > 0:
                try:
                    first_bar = bars[0]
                    first_bar_time = first_bar.get("t") or first_bar.get("time")
                    if first_bar_time:
                        # 判断是秒还是毫秒时间戳
                        if first_bar_time > 1e11:
                            first_bar_time = first_bar_time / 1000
                        first_trade_dt = datetime.fromtimestamp(first_bar_time, tz=timezone.utc).replace(tzinfo=None)
                        metrics.first_trade_at = first_trade_dt
                        logger.info(f"⏰ First trade time from K-line: {first_trade_dt}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to extract first trade time from K-line: {e}")

            # 过滤检查（分两步：先基础筛选，通过后再获取风险评分并筛选）
            filters_cfg = await state.filters_cfg(task_id=task_id_in_use)

            # 日志：显示风险评分筛选配置
            logger.info(f"📋 Task [{task_id_in_use}] risk filter config: "
                       f"SolSniffer={filters_cfg.sol_sniffer_score.min}-{filters_cfg.sol_sniffer_score.max}, "
                       f"TokenSniffer={filters_cfg.token_sniffer_score.min}-{filters_cfg.token_sniffer_score.max}")
            logger.info(f"📋 need_risk_check={need_risk_check(filters_cfg)}")

            # 第一步：基础筛选（不包含风险评分）
            basic_passed, basic_reasons = apply_basic_filters(metrics, filters_cfg)
            logger.info(f"🔍 Basic filter check: {'✅ PASSED' if basic_passed else '❌ FAILED'}")
            if basic_reasons:
                logger.info(f"   Reasons: {', '.join(basic_reasons)}")

            # 第二步：如果基础筛选通过，获取风险评分（用于显示和筛选）
            passed = basic_passed
            reasons = basic_reasons.copy()

            if basic_passed:
                # 只有设置了风险评分筛选条件时才获取风险评分并进行筛选
                if need_risk_check(filters_cfg):
                    logger.info(f"🛡️ Risk filter configured, fetching risk scores...")
                    await fetcher.fetch_risk_scores(metrics)
                    logger.info(f"✅ Risk scores fetched: SolSniffer={metrics.sol_sniffer_score}, TokenSniffer={metrics.token_sniffer_score}")

                    risk_passed, risk_reasons = apply_risk_filters(metrics, filters_cfg)
                    logger.info(f"🔍 Risk filter check: {'✅ PASSED' if risk_passed else '❌ FAILED'}")
                    if risk_reasons:
                        logger.info(f"   Reasons: {', '.join(risk_reasons)}")
                    passed = risk_passed
                    reasons.extend(risk_reasons)
                else:
                    logger.info(f"⏭️ No risk filter configured, skipping risk score fetch and filter")
            else:
                logger.info(f"⏭️ Basic filters failed, skipping risk score fetch")

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

        # 生成图表（不再使用 fallback，若失败直接报错）
        logger.info(f"📸 Generating chart for {ca[:8]}...")
        photo_buffer = None
        try:
            if bars and len(bars) > 0:
                photo_buffer = render_chart(metrics, bars)
                if photo_buffer:
                    logger.info(f"✅ Chart generated from Birdeye data")
                else:
                    raise ValueError("图表渲染失败，未生成图片缓冲")
            else:
                raise ValueError("图表数据为空，无法生成图表")
        except Exception as e:
            error_msg = f"图表生成失败: {str(e)}"
            logger.error(f"❌ {error_msg}")
            return None, None, error_msg
        
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
                for chat_id in targets:
                    try:
                        # 判断是机器人（@username）还是群组/频道（数字ID）
                        is_bot = isinstance(chat_id, str) and chat_id.startswith("@")
                        
                        if is_bot:
                            # 机器人：使用所有 MTProto 客户端，只发送纯 CA 文本，不带任何文件
                            payload = ca  # 对机器人仅发送 CA 地址
                            sent_count = 0
                            for cli_name, cli in client_pool.clients.items():
                                if cli.is_connected():
                                    try:
                                        # 直接使用用户名发送，不先获取实体，避免 Telethon 版本兼容性问题
                                        # Telethon 的 send_message 会自动解析用户名
                                        await cli.send_message(
                                            chat_id, 
                                            payload
                                        )
                                        sent_count += 1
                                        logger.info(f"✅ Sent to bot {chat_id} via MTProto client {cli_name}")
                                    except Exception as e:
                                        error_msg = str(e)
                                        # 如果是 TLObject 解析错误，可能是 Telethon 版本问题
                                        if "Constructor ID" in error_msg or "TLObject" in error_msg:
                                            logger.warning(f"⚠️  Telethon version compatibility issue for client {cli_name} when sending to {chat_id}")
                                            logger.debug(f"   Error: {error_msg[:200]}")
                                            logger.info(f"   Try updating Telethon: pip install --upgrade telethon")
                                        else:
                                            logger.warning(f"⚠️  Failed to send to bot {chat_id} via MTProto client {cli_name}: {error_msg[:200]}")
                                        logger.debug(f"   Full error:", exc_info=True)
                            if sent_count == 0:
                                logger.warning(f"⚠️  No connected MTProto client available or all failed, cannot send to bot {chat_id}")
                            elif sent_count < len([c for c in client_pool.clients.values() if c.is_connected()]):
                                logger.info(f"📊 Sent via {sent_count}/{len([c for c in client_pool.clients.values() if c.is_connected()])} connected client(s)")
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
    
    # 使用所有 MTProto 客户端作为群消息监听者（可监听到其他机器人的发言）
    if client_pool.clients:
        def register_listener(mt_listener, client_name: str):
            @mt_listener.on(events.NewMessage)
            async def _mt_on_message(event, _client_name=client_name):
                try:
                    chat = await event.get_chat()
                    chat_id = getattr(chat, "id", None)
                    if chat_id is None:
                        return
                    text = event.raw_text or ""
                    if not text:
                        return

                    logger.debug(f"📨 [MTProto:{_client_name}] Incoming message in chat {chat_id}: {text[:80]!r}")

                    # 根据任务配置中的 listen_chats 过滤需要处理的任务
                    snap = await state.snapshot()
                    tasks = snap.get("tasks", {})
                    if not tasks:
                        return

                    username = getattr(chat, "username", None)
                    name_keys = []
                    if username:
                        name_keys.append(f"@{username}")

                    matched_tasks: List[str] = []
                    for tid, cfg in tasks.items():
                        if not cfg.get("enabled"):
                            continue
                        listens = cfg.get("listen_chats", [])
                        # 统一成字符串 / 数字集合，并兼容 Bot API 的 -100 前缀形式
                        listen_keys_str = set()
                        listen_ids_int = set()
                        for v in listens:
                            listen_keys_str.add(str(v))
                            if isinstance(v, int):
                                listen_ids_int.add(v)
                                # 如果是 Bot API 的 -100 前缀群组 ID，提取出 channel_id 形式
                                s = str(v)
                                if s.startswith("-100") and len(s) > 4 and s[4:].isdigit():
                                    ch_id = int(s[4:])
                                    listen_ids_int.add(ch_id)
                                    listen_keys_str.add(str(ch_id))

                        chat_id_str = str(chat_id)
                        # 直接数字匹配 / 字符串匹配 / @username 匹配
                        if (
                            chat_id in listen_ids_int
                            or chat_id_str in listen_keys_str
                            or any(k in listen_keys_str for k in name_keys)
                        ):
                            matched_tasks.append(tid)

                    if not matched_tasks:
                        return

                    logger.debug(f"📨 [MTProto:{_client_name}] Message received from chat {chat_id} for tasks: {matched_tasks}")
                    found = set(CA_PATTERN.findall(text))
                    if not found:
                        return
                    logger.info(f"🔍 [MTProto:{_client_name}] Found {len(found)} CA(s) in message: {[ca[:8] + '...' for ca in found]}")

                    for ca in found:
                        for tid in matched_tasks:
                            asyncio.create_task(bot_app._process_ca_bg(chain_hint(ca), ca, task_id=tid))
                except Exception as e:
                    logger.error(f"❌ MTProto listener error ({_client_name}): {e}", exc_info=True)

        for cname, cli in client_pool.clients.items():
            register_listener(cli, cname)
        logger.info(f"📥 MTProto 客户端监听已启用（{len(client_pool.clients)} 个客户端，支持监听群内其他机器人消息）")
    else:
        logger.info("ℹ️ 未配置 MTProto 客户端，群消息监听仅依赖 Bot API（无法看到其他机器人消息）")
    
    # 启动任务调度器（即便当前没有任务，也保持实例可用，避免 /add_client 等命令提示未启用）
    scheduler = TaskScheduler(client_pool, process_ca, state_store=state)
    scheduler.load_tasks(client_pool.tasks_config())
    await scheduler.start()
    bot_app.scheduler = scheduler
    logger.info(f"🗓️  Task scheduler started with {len(scheduler.tasks)} task(s)")
    
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

