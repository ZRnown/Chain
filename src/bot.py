from __future__ import annotations

import asyncio
import html
import logging
import os
import time
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Tuple, Dict, Any

from telegram import Update, BotCommand, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import events

from .state import StateStore

logger = logging.getLogger("ca_filter_bot.bot")

# 中国时区（UTC+8）
TZ_SHANGHAI = timezone(timedelta(hours=8))


CA_PATTERN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40}")


class BotApp:
    def __init__(
        self,
        admin_ids: List[int],
        state: StateStore,
        process_ca: Optional[Callable[[str, str, bool], Awaitable[Tuple[Optional[str], Optional[str], Optional[str]]]]],
        scheduler=None,
    ):
        self.admin_ids = admin_ids
        self.state = state
        self.process_ca = process_ca
        self.scheduler = scheduler
        tg_token = os.getenv("TG_BOT_TOKEN")
        if not tg_token:
            raise RuntimeError("TG_BOT_TOKEN environment variable is required")
        self.app: Application = (
            ApplicationBuilder()
            .token(tg_token)
            .concurrent_updates(True)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("menu", self.cmd_menu))
        self.app.add_handler(CommandHandler("help", self.cmd_menu))
        self.app.add_handler(CommandHandler("c", self.cmd_c))
        self.app.add_handler(CommandHandler("settings", self.cmd_settings))
        self.app.add_handler(CommandHandler("tasks", self.cmd_tasks))
        self.app.add_handler(CommandHandler("task_pause", self.cmd_task_pause))
        self.app.add_handler(CommandHandler("task_resume", self.cmd_task_resume))
        self.app.add_handler(CommandHandler("add_client", self.cmd_add_client))
        self.app.add_handler(CommandHandler("add_task", self.cmd_add_task))
        # 内联按钮回调处理
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        # 监听文本消息（包括按钮点击后的文本输入）
        msg_filter = filters.TEXT & (~filters.COMMAND)
        self.app.add_handler(MessageHandler(msg_filter, self.on_text))
        # 监听文档（用于接收 .session 文件等）
        doc_filter = filters.Document.ALL
        self.app.add_handler(MessageHandler(doc_filter, self.on_document))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        is_admin = user_id in self.admin_ids
        
        text = (
            "🤖 **CA过滤机器人已启动**\n\n"
            "🔍 使用 `/c <合约地址>` 手动查询CA\n\n"
            "💡 **提示**：机器人会自动监听已配置的群组，提取合约地址并过滤推送。"
        )
        
        if is_admin:
            # 给管理员显示键盘菜单
            keyboard = [
                [KeyboardButton("📊 查看配置"), KeyboardButton("🔍 筛选条件")],
                [KeyboardButton("👥 监听群组"), KeyboardButton("📤 推送目标")],
                [KeyboardButton("🗓️ 任务管理")],
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            text += "\n\n✅ **管理员权限已激活**\n使用下方按钮进行配置"
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        is_admin = update.effective_user.id in self.admin_ids
        
        text = "📋 **CA过滤机器人 - 命令菜单**\n\n"
        
        text += "🔍 **查询命令**\n"
        text += "`/c <合约地址>` - 手动查询CA并返回结果\n"
        text += "`/settings` - 查看当前所有配置\n\n"
        
        if is_admin:
            text += "👥 **监听群组管理**\n"
            text += "`/add_listen [chat_id]` - 添加监听群（无参数则添加当前群）\n"
            text += "`/del_listen <chat_id>` - 删除监听群\n"
            text += "`/list_listen` - 查看所有监听群\n\n"
            
            text += "📤 **推送目标管理**\n"
            text += "`/add_push [chat_id]` - 添加推送目标（群/机器人/个人）\n"
            text += "`/del_push <chat_id>` - 删除推送目标\n"
            text += "`/list_push` - 查看所有推送目标\n\n"
            
            text += "⚙️ **筛选条件设置**\n"
            text += "`/set_filter <名称> <最小值|null> <最大值|null>` - 设置筛选条件\n"
            text += "`/list_filters` - 查看所有筛选条件\n\n"
            text += "筛选条件名称：\n"
            text += "• `market_cap_usd` - 市值（USD）\n"
            text += "• `liquidity_usd` - 池子大小（USD）\n"
            text += "• `open_minutes` - 开盘时间（分钟）\n"
            text += "• `top10_ratio` - 前十持仓占比（0-1，如0.3表示30%）\n"
            text += "• `holder_count` - 持有人数\n"
            text += "• `max_holder_ratio` - 最大持仓占比（0-1）\n"
            text += "• `trades_5m` - 5分钟交易数\n\n"
            
            text += "💡 **示例**\n"
            text += "`/set_filter market_cap_usd 5000 1000000` - 市值5K-1M\n"
            text += "`/set_filter top10_ratio null 0.3` - 前十占比<30%\n"
        else:
            text += "⚠️ 仅管理员可使用配置命令\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_c(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        logger.info(f"📥 /c command from user {user_id}")
        
        if not context.args:
            await update.message.reply_text(
                "❌ 用法: `/c <合约地址>`\n\n"
                "💡 支持 Solana 和 BSC 链的合约地址",
                parse_mode="Markdown"
            )
            return
        ca = context.args[0].strip()
        chain = chain_hint(ca)
        logger.info(f"🔍 Manual query: {chain} - {ca}")
        await update.message.reply_text(f"⏳ 正在处理 `{ca}` ...", parse_mode="Markdown")
        if not self.process_ca:
            await update.message.reply_text("❌ 处理功能未就绪")
            return
        try:
            current_task = await self.state.current_task()
            img_buffer, caption, error_msg = await self.process_ca(chain, ca, True, task_id=current_task)
            if error_msg:
                await update.message.reply_text(
                    f"❌ <b>查询失败</b>\n\n<code>{ca}</code>\n\n{error_msg}",
                    parse_mode="HTML"
                )
            elif img_buffer and caption:
                # Send photo with caption (img_buffer is BytesIO)
                img_buffer.seek(0)  # 确保指针在开头
                await update.message.reply_photo(photo=img_buffer, caption=caption, parse_mode="HTML")
            elif caption:
                # Send text only if no photo
                await update.message.reply_text(caption, parse_mode="HTML")
            else:
                await update.message.reply_text(f"❌ 未找到数据: <code>{ca}</code>", parse_mode="HTML")
        except Exception as e:
            logger.error(f"❌ Error in cmd_c: {e}", exc_info=True)
            await update.message.reply_text(f"❌ 处理失败: {str(e)}")

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        snap = await self.state.snapshot()
        
        tasks = snap.get("tasks", {})
        current = snap.get("current_task")
        if not tasks:
            await update.message.reply_text("⚠️ 暂无任务配置。", parse_mode="HTML")
            return
        
        # 获取 scheduler 中的任务信息（用于显示定时时间）
        scheduler_tasks = {}
        if self.scheduler:
            for st in self.scheduler.list_tasks():
                scheduler_tasks[st.get("id")] = st
        
        text = f"⚙️ <b>所有任务配置</b> ({len(tasks)}个)\n\n"
        
        # 遍历所有任务
        for tid, task_cfg in tasks.items():
            is_current = (tid == current)
            current_tag = "（当前）" if is_current else ""
            text += f"━━━━━━━━━━━━━━━━━━━━\n"
            text += f"📋 <b>{html.escape(tid)}</b> {current_tag}\n\n"
            
            # 显示定时信息
            start_time = task_cfg.get("start_time")
            end_time = task_cfg.get("end_time")
            interval_minutes = None
            if tid in scheduler_tasks:
                st = scheduler_tasks[tid]
                interval_minutes = st.get("interval_minutes")
            
            if interval_minutes:
                text += f"⏰ <b>定时任务</b>: 每 {interval_minutes} 分钟\n"
            if start_time or end_time:
                text += f"🕐 <b>时间窗</b>: {start_time or '--:--'} ~ {end_time or '--:--'}\n"
            if interval_minutes or start_time or end_time:
                text += "\n"
            
            listen_chats = task_cfg.get("listen_chats", [])
            text += f"👥 <b>监听群组</b> ({len(listen_chats)}个)\n"
            if listen_chats:
                for chat_id in listen_chats[:5]:  # 最多显示5个
                    chat_info = await self._get_chat_info(chat_id)
                    chat_name = chat_info.get('title', f'群组 {chat_id}') if chat_info else f'群组 {chat_id}'
                    chat_name_escaped = html.escape(str(chat_name))
                    chat_id_escaped = html.escape(str(chat_id))
                    text += f"• <b>{chat_name_escaped}</b> (<code>{chat_id_escaped}</code>)\n"
                if len(listen_chats) > 5:
                    text += f"• ... 还有 {len(listen_chats) - 5} 个\n"
            else:
                text += "• 暂无\n"
            text += "\n"
            
            push_chats = task_cfg.get("push_chats", [])
            text += f"📤 <b>推送目标</b> ({len(push_chats)}个)\n"
            if push_chats:
                for chat_id in push_chats[:5]:  # 最多显示5个
                    chat_info = await self._get_chat_info(chat_id)
                    if chat_info:
                        chat_name = chat_info.get('title', f'目标 {chat_id}')
                        chat_type = chat_info.get('type', 'unknown')
                        username = chat_info.get('username')
                        chat_id_display = chat_info.get('id', chat_id)
                        
                        type_info = {
                            'group': ('👥', '群组'),
                            'supergroup': ('👥', '群组'),
                            'channel': ('📢', '频道'),
                            'private': ('👤', '个人'),
                            'bot': ('🤖', '机器人')
                        }.get(chat_type, ('📌', '目标'))
                        
                        type_icon, type_name = type_info
                        chat_name_escaped = html.escape(str(chat_name))
                        chat_id_escaped = html.escape(str(chat_id_display))
                        username_str = f" @{html.escape(str(username))}" if username else ""
                        text += f"• {type_icon} <b>{chat_name_escaped}</b> ({type_name}) <code>{chat_id_escaped}</code>{username_str}\n"
                    else:
                        chat_id_escaped = html.escape(str(chat_id))
                        text += f"• 📌 <b>目标</b> (<code>{chat_id_escaped}</code>)\n"
                if len(push_chats) > 5:
                    text += f"• ... 还有 {len(push_chats) - 5} 个\n"
            else:
                text += "• 暂无\n"
            text += "\n"
            
            text += "🔍 <b>筛选条件</b>\n"
            filters_cfg = task_cfg.get("filters", {})
            filter_names = {
                "market_cap_usd": "市值(USD)",
                "liquidity_usd": "池子(USD)",
                "open_minutes": "开盘时间(分钟)",
                "top10_ratio": "前十占比",
                "holder_count": "持有人数",
                "max_holder_ratio": "最大持仓占比",
                "trades_5m": "5分钟交易数",
            }
            has_filter = False
            for key, display_name in filter_names.items():
                f = filters_cfg.get(key, {})
                min_v = f.get("min")
                max_v = f.get("max")
                if min_v is not None or max_v is not None:
                    has_filter = True
                    min_str = f"{min_v:,.0f}" if min_v is not None else "无限制"
                    max_str = f"{max_v:,.0f}" if max_v is not None else "无限制"
                    # 对于百分比类型，使用更精确的格式
                    if key in ["top10_ratio", "max_holder_ratio"]:
                        min_str = f"{min_v*100:.1f}%" if min_v is not None else "无限制"
                        max_str = f"{max_v*100:.1f}%" if max_v is not None else "无限制"
                    text += f"• {display_name}: {min_str} ~ {max_str}\n"
            if not has_filter:
                text += "• 未设置\n"
            text += "\n"
        
        await update.message.reply_text(text, parse_mode="HTML")

    async def cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        if not self.scheduler:
            await update.message.reply_text("⚠️ 未启用任务调度（缺少配置或启动失败）")
            return
        tasks = self.scheduler.list_tasks()
        if not tasks:
            await update.message.reply_text("📋 当前无任务")
            return
        lines = ["📋 任务列表:"]
        for t in tasks:
            status = "✅ 启用" if t.get("enabled") else "⏸️ 暂停"
            lines.append(f"- {t.get('id')} | {t.get('name')} | {status} | 每{t.get('interval_minutes')}分钟 | client={t.get('client')}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_task_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        if not self.scheduler:
            await update.message.reply_text("⚠️ 未启用任务调度")
            return
        if not context.args:
            await update.message.reply_text("用法: /task_pause <task_id>")
            return
        task_id = context.args[0]
        ok = self.scheduler.pause(task_id)
        await update.message.reply_text("✅ 已暂停" if ok else "❌ 未找到任务")

    async def cmd_task_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        if not self.scheduler:
            await update.message.reply_text("⚠️ 未启用任务调度")
            return
        if not context.args:
            await update.message.reply_text("用法: /task_resume <task_id>")
            return
        task_id = context.args[0]
        ok = self.scheduler.resume(task_id)
        await update.message.reply_text("✅ 已恢复" if ok else "❌ 未找到任务")

    async def cmd_add_client(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        if not self.scheduler or not self.scheduler.client_pool:
            await update.message.reply_text("⚠️ 未启用任务调度/客户端池")
            return
        # 用法: /add_client <name> <session>
        if len(context.args) < 2:
            await update.message.reply_text("用法: /add_client <name> <session_path 或 string_session>")
            return
        name = context.args[0]
        session_path = " ".join(context.args[1:])
        try:
            final_name = await self.scheduler.client_pool.add_client(name, session_path)
        except Exception as e:
            await update.message.reply_text(f"❌ 添加失败: {e}\n⚙️ 请确认 .env 已设置 TELEGRAM_API_ID / TELEGRAM_API_HASH")
            return
        await update.message.reply_text(f"✅ 客户端已添加并启动：{final_name}")

    async def cmd_add_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        if not self.scheduler:
            await update.message.reply_text("⚠️ 未启用任务调度")
            return
        # 用法: /add_task <id> <client> <chain> <ca> <interval_minutes> <targets_csv>
        if len(context.args) < 6:
            await update.message.reply_text("用法: /add_task <id> <client> <chain> <ca> <interval_minutes> <targets_csv>")
            return
        task_id = context.args[0]
        client = context.args[1]
        chain = context.args[2]
        ca = context.args[3]
        try:
            interval = int(context.args[4])
        except Exception:
            await update.message.reply_text("❌ interval_minutes 需要是数字")
            return
        targets_csv = context.args[5]
        targets = [t.strip() for t in targets_csv.split(",") if t.strip()]
        task = {
            "id": task_id,
            "name": task_id,
            "client": client,
            "chain": chain,
            "ca": ca,
            "targets": targets,
            "interval_minutes": interval,
            "enabled": True,
        }
        ok = self.scheduler.add_task(task)
        await update.message.reply_text("✅ 任务已添加" if ok else "❌ 任务ID已存在")

    async def cmd_add_listen(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        chat_id = None
        if context.args:
            try:
                chat_id = int(context.args[0])
            except Exception:
                await update.message.reply_text("❌ 用法: `/add_listen [chat_id]`\n无参数则添加当前群", parse_mode="Markdown")
                return
        else:
            chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            await update.message.reply_text("❌ 无法获取群组ID")
            return
        await self.state.add_listen(chat_id)
        await update.message.reply_text(f"✅ 已添加监听群: `{chat_id}`\n\n💡 使用 `/list_listen` 查看所有监听群", parse_mode="Markdown")

    async def cmd_del_listen(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        if not context.args:
            await update.message.reply_text("❌ 用法: `/del_listen <chat_id>`", parse_mode="Markdown")
            return
        try:
            chat_id = int(context.args[0])
        except Exception:
            await update.message.reply_text("❌ 无效的chat_id")
            return
        snap = await self.state.snapshot()
        if chat_id not in snap.get("listen_chats", []):
            await update.message.reply_text(f"❌ 监听列表中不存在: `{chat_id}`", parse_mode="Markdown")
            return
        await self.state.del_listen(chat_id)
        await update.message.reply_text(f"✅ 已删除监听群: `{chat_id}`", parse_mode="Markdown")

    async def cmd_list_listen(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        snap = await self.state.snapshot()
        listen_chats = snap.get("listen_chats", [])
        if not listen_chats:
            await update.message.reply_text("📋 <b>监听群组列表</b>\n\n暂无监听群组\n\n💡 使用 <code>/add_listen</code> 添加", parse_mode="HTML")
            return
        text = f"📋 <b>监听群组列表</b> ({len(listen_chats)}个)\n\n"
        for idx, chat_id in enumerate(listen_chats, 1):
            chat_info = await self._get_chat_info(chat_id)
            chat_name = chat_info.get('title', f'目标 {chat_id}') if chat_info else f'目标 {chat_id}'
            chat_name_escaped = html.escape(str(chat_name))
            chat_id_escaped = html.escape(str(chat_id))
            text += f"{idx}. <b>{chat_name_escaped}</b>\n   ID: <code>{chat_id_escaped}</code>\n\n"
        text += "💡 使用 <code>/del_listen &lt;chat_id&gt;</code> 删除"
        await update.message.reply_text(text, parse_mode="HTML")

    async def cmd_add_push(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        chat_id = None
        if context.args:
            try:
                chat_id = int(context.args[0])
            except Exception:
                await update.message.reply_text("❌ 用法: `/add_push [chat_id]`\n无参数则添加当前群", parse_mode="Markdown")
                return
        else:
            chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            await update.message.reply_text("❌ 无法获取群组ID")
            return
        await self.state.add_push(chat_id)
        await update.message.reply_text(f"✅ 已添加推送群: `{chat_id}`\n\n💡 使用 `/list_push` 查看所有推送群", parse_mode="Markdown")

    async def cmd_del_push(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        if not context.args:
            await update.message.reply_text("❌ 用法: `/del_push <chat_id>`", parse_mode="Markdown")
            return
        try:
            chat_id = int(context.args[0])
        except Exception:
            await update.message.reply_text("❌ 无效的chat_id")
            return
        snap = await self.state.snapshot()
        if chat_id not in snap.get("push_chats", []):
            await update.message.reply_text(f"❌ 推送列表中不存在: `{chat_id}`", parse_mode="Markdown")
            return
        await self.state.del_push(chat_id)
        await update.message.reply_text(f"✅ 已删除推送群: `{chat_id}`", parse_mode="Markdown")

    async def cmd_list_push(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        snap = await self.state.snapshot()
        push_chats = snap.get("push_chats", [])
        if not push_chats:
            await update.message.reply_text("📋 <b>推送群组列表</b>\n\n暂无推送群组\n\n💡 使用 <code>/add_push</code> 添加", parse_mode="HTML")
            return
        text = f"📋 <b>推送群组列表</b> ({len(push_chats)}个)\n\n"
        for idx, chat_id in enumerate(push_chats, 1):
            chat_info = await self._get_chat_info(chat_id)
            if chat_info:
                chat_name = chat_info.get('title', f'目标 {chat_id}')
                chat_type = chat_info.get('type', 'unknown')
                username = chat_info.get('username')
                chat_id_display = chat_info.get('id', chat_id)
                
                # 类型图标和名称
                type_info = {
                    'group': ('👥', '群组'),
                    'supergroup': ('👥', '群组'),
                    'channel': ('📢', '频道'),
                    'private': ('👤', '个人'),
                    'bot': ('🤖', '机器人')
                }.get(chat_type, ('📌', '目标'))
                
                type_icon, type_name = type_info
                chat_name_escaped = html.escape(str(chat_name))
                chat_id_escaped = html.escape(str(chat_id_display))
                username_str = f" @{html.escape(str(username))}" if username else ""
                text += f"{idx}. {type_icon} <b>{chat_name_escaped}</b> ({type_name})\n   ID: <code>{chat_id_escaped}</code>{username_str}\n\n"
            else:
                chat_name = f'群组 {chat_id}'
                chat_name_escaped = html.escape(str(chat_name))
                chat_id_escaped = html.escape(str(chat_id))
                text += f"{idx}. <b>{chat_name_escaped}</b>\n   ID: <code>{chat_id_escaped}</code>\n\n"
        text += "💡 使用 <code>/del_push &lt;chat_id&gt;</code> 删除"
        await update.message.reply_text(text, parse_mode="HTML")

    async def cmd_set_filter(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        if len(context.args) != 3:
            await update.message.reply_text(
                "❌ 用法: `/set_filter <名称> <最小值|null> <最大值|null>`\n\n"
                "💡 示例:\n"
                "`/set_filter market_cap_usd 5000 1000000` - 市值5K-1M\n"
                "`/set_filter top10_ratio null 0.3` - 前十占比<30%\n\n"
                "使用 `/list_filters` 查看所有筛选条件",
                parse_mode="Markdown"
            )
            return
        name, min_s, max_s = context.args
        # 对于百分比类型（前十占比/最大持仓），输入用 1-100 的整数，内部以 0-1 存储
        if name in ("top10_ratio", "max_holder_ratio"):
            def parse_pct(s: str):
                if s.lower() == "null":
                    return None
                try:
                    iv = int(s)
                except Exception:
                    raise ValueError("percent must be integer 1-100")
                if iv < 0 or iv > 100:
                    raise ValueError("percent must be between 0 and 100")
                return iv / 100.0
            try:
                min_v = parse_pct(min_s)
                max_v = parse_pct(max_s)
            except Exception as e:
                await update.message.reply_text(f"❌ 设置失败: {e}")
                return
        else:
            min_v = None if min_s.lower() == "null" else _maybe_float(min_s)
            max_v = None if max_s.lower() == "null" else _maybe_float(max_s)
        try:
            await self.state.set_filter(name, min_v, max_v)
        except Exception as e:
            await update.message.reply_text(f"❌ 设置失败: {e}")
            return
        
        filter_names = {
            "market_cap_usd": "市值(USD)",
            "liquidity_usd": "池子(USD)",
            "open_minutes": "开盘时间(分钟)",
            "top10_ratio": "前十占比",
            "holder_count": "持有人数",
            "max_holder_ratio": "最大持仓占比",
            "trades_5m": "5分钟交易数",
        }
        display_name = filter_names.get(name, name)
        min_str = f"{min_v:,.0f}" if min_v is not None else "无限制"
        max_str = f"{max_v:,.0f}" if max_v is not None else "无限制"
        await update.message.reply_text(
            f"✅ 筛选条件已更新\n\n"
            f"**{display_name}** ({name})\n"
            f"最小值: {min_str}\n"
            f"最大值: {max_str}",
            parse_mode="Markdown"
        )

    async def cmd_list_filters(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in self.admin_ids:
            await update.message.reply_text("❌ 无权限")
            return
        snap = await self.state.snapshot()
        filters_cfg = snap.get("filters", {})
        
        filter_names = {
            "market_cap_usd": "市值(USD)",
            "liquidity_usd": "池子(USD)",
            "open_minutes": "开盘时间(分钟)",
            "top10_ratio": "前十占比 (0-1)",
            "holder_count": "持有人数",
            "max_holder_ratio": "最大持仓占比 (0-1)",
            "trades_5m": "5分钟交易数",
        }
        
        text = "🔍 **筛选条件列表**\n\n"
        has_set = False
        for key, display_name in filter_names.items():
            f = filters_cfg.get(key, {})
            min_v = f.get("min")
            max_v = f.get("max")
            if min_v is None and max_v is None:
                text += f"• **{display_name}** (`{key}`): ❌ 未设置\n"
            else:
                has_set = True
                # 对于百分比类型，显示为百分号
                if key in ["top10_ratio", "max_holder_ratio"]:
                    min_str = f"{min_v*100:.1f}%" if min_v is not None else "无限制"
                    max_str = f"{max_v*100:.1f}%" if max_v is not None else "无限制"
                else:
                    min_str = f"{min_v:,.2f}" if min_v is not None else "无限制"
                    max_str = f"{max_v:,.2f}" if max_v is not None else "无限制"
                text += f"• **{display_name}** (`{key}`): ✅ {min_str} ~ {max_str}\n"
        
        if not has_set:
            text += "\n⚠️ 所有筛选条件均未设置，所有CA都会推送\n"
        
        text += "\n💡 使用 `/set_filter <名称> <最小值|null> <最大值|null>` 设置"
        await update.message.reply_text(text, parse_mode="Markdown")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        is_admin = user_id in self.admin_ids
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            return
        
        text = update.message.text if update.message else ""
        if not text:
            return
        
        # 处理管理员按钮菜单
        if is_admin and chat_id == user_id:  # 私聊中的按钮/配置输入
            await self.handle_admin_button(update, context, text)
            return
        
        # 处理CA监听（群组消息）
        if not self.process_ca:
            return
        snap = await self.state.snapshot()
        tasks = snap.get("tasks", {})
        if not tasks:
            logger.debug("⏭️  No tasks configured, ignoring message")
            return

        # 找到包含该监听群的已启用任务
        matched_tasks = []
        for tid, cfg in tasks.items():
            if cfg.get("enabled") and chat_id in cfg.get("listen_chats", []):
                matched_tasks.append(tid)

        if not matched_tasks:
            logger.debug(f"⏭️  Chat {chat_id} not in any enabled task listen list")
            return
        
        logger.info(f"📨 Message received from chat {chat_id} for tasks: {matched_tasks}")
        found = set(CA_PATTERN.findall(text))
        logger.info(f"🔍 Found {len(found)} CA(s) in message: {[ca[:8] + '...' for ca in found]}")
        
        for ca in found:
            # 每个任务独立后台处理，避免阻塞
            for tid in matched_tasks:
                asyncio.create_task(self._process_ca_bg(chain_hint(ca), ca, task_id=tid))
    
    async def handle_admin_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """处理管理员按钮菜单"""
        # 先处理通用“完成”指令（结束等待状态）
        user_id = update.effective_user.id
        if text.strip() in ("完成", "完毕", "done", "Done", "DONE"):
            if hasattr(context, 'user_data') and context.user_data.get(f'{user_id}_waiting'):
                context.user_data[f'{user_id}_waiting'] = None
                await update.message.reply_text("✅ 已结束当前配置流程")
                return

        if text == "📊 查看配置":
            await self.cmd_settings(update, context)
        elif text == "👥 监听群组":
            await self.show_listen_menu(update.message)
        elif text == "📤 推送目标":
            await self.show_push_menu(update.message)
        elif text == "🔍 筛选条件":
            await self.show_filter_menu(update.message)
        elif text == "🗓️ 任务管理":
            await self.show_task_menu(update.message)
        else:
            # 可能是输入的值（用于设置筛选条件）
            # 检查是否有待处理的设置
            if hasattr(context, 'user_data') and context.user_data.get(f'{user_id}_waiting'):
                await self.handle_setting_input(update, context, text)
    
    async def show_listen_menu(self, message):
        """显示监听群组菜单"""
        snap = await self.state.snapshot()
        current = snap.get("current_task")
        if not current:
            await message.reply_text("⚠️ 请先创建并选择任务，然后再配置监听群组。", parse_mode="HTML")
            return
        listen_chats = snap.get("tasks", {}).get(current, {}).get("listen_chats", [])
        
        keyboard = [
            [InlineKeyboardButton("➕ 添加群组", callback_data="add_listen_link")],
            [InlineKeyboardButton("📋 查看列表", callback_data="list_listen")],
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        count = len(listen_chats)
        await message.reply_text(
            f"👥 <b>监听群组管理</b>（当前任务：{html.escape(current)}）\n\n当前有 <b>{count}</b> 个监听群组\n\n"
            f"💡 点击「添加群组」后，发送群组邀请链接或公共群链接",
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    
    async def show_push_menu(self, message):
        """显示推送目标菜单（群组/机器人/个人）"""
        snap = await self.state.snapshot()
        current = snap.get("current_task")
        if not current:
            await message.reply_text("⚠️ 请先创建并选择任务，然后再配置推送目标。", parse_mode="HTML")
            return
        push_chats = snap.get("tasks", {}).get(current, {}).get("push_chats", [])
        
        keyboard = [
            [InlineKeyboardButton("➕ 添加目标", callback_data="add_push_link")],
            [InlineKeyboardButton("📋 查看列表", callback_data="list_push")],
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        count = len(push_chats)
        await message.reply_text(
            f"📤 <b>推送目标管理</b>（当前任务：{html.escape(current)}）\n\n当前有 <b>{count}</b> 个推送目标（群组/机器人/个人）\n\n"
            f"💡 点击「添加目标」后，发送群组/机器人的邀请链接、@用户名或chat_id（数字）",
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    
    async def show_filter_menu(self, message, edit: bool = False):
        """显示筛选条件菜单，显示已设置的值"""
        snap = await self.state.snapshot()
        current = snap.get("current_task")
        if not current:
            text = "⚠️ 请先创建并选择任务，然后再配置筛选条件。"
            if edit:
                await message.edit_message_text(text, parse_mode="HTML")
            else:
                await message.reply_text(text, parse_mode="HTML")
            return
        
        filters_cfg = snap.get("tasks", {}).get(current, {}).get("filters", {})
        
        filter_names = {
            "market_cap_usd": "💰 市值(USD)",
            "liquidity_usd": "💧 池子(USD)",
            "open_minutes": "⏰ 开盘时间(分钟)",
            "top10_ratio": "👑 前十占比",
            "holder_count": "👥 持有人数",
            "max_holder_ratio": "🐳 最大持仓占比",
            "trades_5m": "📈 5分钟交易数",
        }
        
        # 构建菜单文本，显示已设置的值
        text = f"🔍 <b>筛选条件设置</b>（当前任务：{html.escape(current)}）\n\n"
        
        keyboard = []
        for key, name in filter_names.items():
            f = filters_cfg.get(key, {})
            min_v = f.get("min")
            max_v = f.get("max")
            
            # 在按钮名称后显示已设置的值
            if min_v is not None or max_v is not None:
                # 对于百分比类型，显示为百分号（0.23 -> 23.0%）
                if key in ["top10_ratio", "max_holder_ratio"]:
                    min_str = f"{min_v*100:.1f}%" if min_v is not None else "无"
                    max_str = f"{max_v*100:.1f}%" if max_v is not None else "无"
                else:
                    min_str = f"{min_v:,.0f}" if min_v is not None else "无"
                    max_str = f"{max_v:,.0f}" if max_v is not None else "无"
                button_text = f"{name} ({min_str}~{max_str})"
            else:
                button_text = f"{name} (未设置)"
            
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"set_filter_{key}")])
        
        keyboard.append([InlineKeyboardButton("📋 查看所有筛选条件", callback_data="list_filters")])
        keyboard.append([InlineKeyboardButton("🔄 重置所有筛选", callback_data="reset_filters")])
        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="back_task_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if edit:
            await message.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)

    async def show_task_menu(self, message):
        """显示任务管理菜单"""
        keyboard = [
            [InlineKeyboardButton("📋 查看任务", callback_data="list_tasks")],
            [InlineKeyboardButton("👤 客户端列表", callback_data="list_clients")],
            [InlineKeyboardButton("➕ 添加客户端", callback_data="add_client_prompt")],
            [InlineKeyboardButton("➕ 添加任务", callback_data="add_task_prompt")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = "🗓️ <b>任务管理</b>\n\n支持多客户端、多任务定时推送。\n请选择操作："
        # 判断是 Update 对象还是 CallbackQuery 对象
        if hasattr(message, 'edit_message_text'):
            # 是 CallbackQuery，使用 edit_message_text
            await message.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            # 是 Message 对象，使用 reply_text
            await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理内联按钮回调"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        if user_id not in self.admin_ids:
            await query.edit_message_text("❌ 无权限")
            return
        
        data = query.data
        
        # 监听群组
        if data == "add_listen_link":
            await query.edit_message_text("📝 请发送群组邀请链接或公共群链接：\n\n格式：\n• `https://t.me/joinchat/...` (私有群)\n• `https://t.me/groupname` (公共群)\n• 或直接发送群组ID（数字）")
            if not hasattr(context, 'user_data'):
                context.user_data = {}
            context.user_data[f'{user_id}_waiting'] = 'add_listen_link'
        elif data.startswith("del_listen_"):
            # 支持数字ID和@username，不能简单 split 再转 int
            raw_id = data[len("del_listen_"):]
            chat_key: object
            if raw_id.lstrip("-").isdigit():
                chat_key = int(raw_id)
            else:
                chat_key = raw_id  # 例如 @some_bot 或 @channel_name
            await self.state.del_listen(chat_key)
            await query.edit_message_text(f"✅ 已删除监听群: <code>{html.escape(str(chat_key))}</code>", parse_mode="HTML")
        elif data == "list_listen":
            await self.list_listen_callback(query)
        elif data == "back_listen":
            await self.show_listen_menu(query.message)
        
        # 推送群组
        elif data == "add_push_link":
            await query.edit_message_text(
                "📝 请发送推送目标：\n\n"
                "• 群组/机器人的邀请链接\n"
                "• @用户名\n"
                "• chat_id（数字，可为负数）",
                parse_mode="HTML"
            )
            if not hasattr(context, 'user_data'):
                context.user_data = {}
            context.user_data[f'{user_id}_waiting'] = 'add_push_link'
        elif data.startswith("del_push_"):
            # 支持数字ID和@username，不能简单 split 再转 int
            raw_id = data[len("del_push_"):]
            chat_key: object
            if raw_id.lstrip("-").isdigit():
                chat_key = int(raw_id)
            else:
                chat_key = raw_id  # 例如 @some_bot 或 @channel_name
            await self.state.del_push(chat_key)
            await query.edit_message_text(f"✅ 已删除推送目标: <code>{html.escape(str(chat_key))}</code>", parse_mode="HTML")
        elif data == "list_push":
            await self.list_push_callback(query)
        elif data == "back_push":
            await self.show_push_menu(query.message)
        
        # 筛选条件
        elif data.startswith("set_filter_"):
            filter_key = data.replace("set_filter_", "")
            # 使用HTML模式避免Markdown解析错误
            filter_names = {
                "market_cap_usd": "市值(USD)",
                "liquidity_usd": "池子(USD)",
                "open_minutes": "开盘时间(分钟)",
                "top10_ratio": "前十占比",
                "holder_count": "持有人数",
                "max_holder_ratio": "最大持仓占比",
                "trades_5m": "5分钟交易数",
            }
            display_name = filter_names.get(filter_key, filter_key)
            
            # 获取当前已设置的值
            snap = await self.state.snapshot()
            current = snap.get("current_task")
            filters_cfg = snap.get("tasks", {}).get(current, {}).get("filters", {}) if current else {}
            f = filters_cfg.get(filter_key, {})
            current_min = f.get("min")
            current_max = f.get("max")
            
            # 显示当前值
            current_text = ""
            if current_min is not None or current_max is not None:
                min_str = f"{current_min:,.0f}" if current_min is not None else "无限制"
                max_str = f"{current_max:,.0f}" if current_max is not None else "无限制"
                # 对于百分比类型，使用更精确的格式
                if filter_key in ["top10_ratio", "max_holder_ratio"]:
                    min_str = f"{current_min:.1f}" if current_min is not None else "无限制"
                    max_str = f"{current_max:.1f}" if current_max is not None else "无限制"
                current_text = f"\n\n当前设置：<b>{min_str} ~ {max_str}</b>"
            
            # 根据类型显示不同的提示（百分比类使用 1-100 的整数）
            if filter_key == "max_holder_ratio":
                hint = "例如：<code>1 20</code> 或 <code>null 15</code>（输入 1-100 的整数，代表百分比）"
            elif filter_key in ["top10_ratio"]:
                hint = "例如：<code>1 30</code> 或 <code>null 20</code>（输入 1-100 的整数，代表百分比）"
            else:
                hint = "例如：<code>5000 1000000</code> 或 <code>null 15</code>"
            
            await query.edit_message_text(
                f"📝 设置筛选条件: <b>{display_name}</b>{current_text}\n\n"
                f"请输入范围，格式：<code>最小值 最大值</code>\n"
                f"{hint}\n\n"
                f"💡 使用 <code>null</code> 表示无限制\n"
                f"💡 设置完成后会自动返回菜单，可继续设置其他条件",
                parse_mode="HTML"
            )
            if not hasattr(context, 'user_data'):
                context.user_data = {}
            context.user_data[f'{user_id}_waiting'] = f'set_filter_{filter_key}'
            context.user_data[f'{user_id}_filter_menu_query'] = query  # 保存query以便返回菜单
        elif data == "list_filters":
            await self.list_filters_callback(query)
        elif data == "reset_filters":
            # 重置所有筛选条件
            filter_keys = ["market_cap_usd", "liquidity_usd", "open_minutes", "top10_ratio", 
                          "holder_count", "max_holder_ratio", "trades_5m"]
            for key in filter_keys:
                await self.state.set_filter(key, None, None)
            await query.edit_message_text("✅ 已重置当前任务的所有筛选条件")
        
        # 任务管理
        elif data == "list_tasks":
            await self.list_tasks_callback(query)
        elif data == "add_client_prompt":
            await query.edit_message_text(
                "📝 <b>批量添加客户端</b>\n\n"
                "可以上传多个 session 文件或发送多个字符串 session：\n\n"
                "• 上传 <code>.session</code> 文件（自动生成名称）\n"
                "• 发送字符串：<code>名称 session字符串</code>\n"
                "• 或直接发送 session 字符串（自动生成名称）\n\n"
                "完成后输入：<code>完成</code> 或 <code>done</code>\n\n"
                "⚙️ 请先在 <code>.env</code> 设置 <code>TELEGRAM_API_ID</code> / <code>TELEGRAM_API_HASH</code>（或 APP_ID / APP_HASH）。",
                parse_mode="HTML"
            )
            if not hasattr(context, 'user_data'):
                context.user_data = {}
            context.user_data[f'{user_id}_waiting'] = 'add_client'
            context.user_data[f'{user_id}_client_count'] = 0
        elif data == "add_task_prompt":
            await query.edit_message_text(
                "📝 <b>创建任务</b>\n\n"
                "只需输入任务名称，例如：<code>任务A</code>\n"
                "创建后会自动切换为当前任务，默认处于“暂停”状态。\n"
                "请继续配置：监听群组、推送目标、筛选条件，并在任务列表中启用。",
                parse_mode="HTML"
            )
            if not hasattr(context, 'user_data'):
                context.user_data = {}
            context.user_data[f'{user_id}_waiting'] = 'add_task'
        elif data == "list_clients":
            await self.list_clients_callback(query)
        elif data.startswith("del_client_"):
            client_name = data.replace("del_client_", "")
            if self.scheduler and self.scheduler.client_pool:
                ok = await self.scheduler.client_pool.remove_client(client_name)
                if ok:
                    await query.answer("✅ 客户端已删除")
                    await self.list_clients_callback(query)
                else:
                    await query.answer("❌ 未找到该客户端")
            else:
                await query.answer("⚠️ 未启用任务调度/客户端池")
        elif data.startswith("task_select:"):
            task_id = data.split(":", 1)[1]
            await self.state.set_current_task(task_id)
            await query.answer(f"已切换到任务 {task_id}")
            await self.list_tasks_callback(query)
        elif data.startswith("task_enable:"):
            task_id = data.split(":", 1)[1]
            # 检查时间窗
            task_cfg = await self.state.task_settings(task_id)
            start_time = task_cfg.get("start_time")
            end_time = task_cfg.get("end_time")
            has_window = start_time or end_time
            
            if has_window:
                # 检查是否在时间窗内
                in_window = self._is_in_time_window(start_time, end_time)
                if not in_window:
                    window_str = f"{start_time or '不限制'} ~ {end_time or '不限制'}"
                    await query.answer(f"⚠️ 当前不在时间窗内 ({window_str})", show_alert=True)
                    await self.list_tasks_callback(query)
                    return
            
            await self.state.set_task_enabled(task_id, True)
            # 同步到 scheduler
            if self.scheduler:
                for t in self.scheduler.tasks:
                    if t.get("id") == task_id:
                        t["enabled"] = True
                self.scheduler.client_pool.update_tasks_config(self.scheduler.tasks)
            await query.answer("已启用")
            await self.list_tasks_callback(query)
        elif data.startswith("task_disable:"):
            task_id = data.split(":", 1)[1]
            await self.state.set_task_enabled(task_id, False)
            # 同步到 scheduler
            if self.scheduler:
                for t in self.scheduler.tasks:
                    if t.get("id") == task_id:
                        t["enabled"] = False
                self.scheduler.client_pool.update_tasks_config(self.scheduler.tasks)
            await query.answer("已暂停")
            await self.list_tasks_callback(query)
        elif data.startswith("task_delete:"):
            task_id = data.split(":", 1)[1]
            ok = await self.state.delete_task(task_id)
            await query.answer("已删除" if ok else "未找到任务")
            await self.list_tasks_callback(query)
        elif data.startswith("task_window:"):
            task_id = data.split(":", 1)[1]
            await query.edit_message_text(
                f"🕒 为任务 <b>{html.escape(task_id)}</b> 设置时间窗\n\n"
                f"请输入：<code>HH:MM HH:MM</code>\n"
                f"第一个是开始时间，第二个是结束时间；\n"
                f"留空或输入 <code>none</code> 代表不限制。\n"
                f"例：<code>09:00 23:00</code> 或 <code>none 06:00</code>。",
                parse_mode="HTML"
            )
            if not hasattr(context, 'user_data'):
                context.user_data = {}
            context.user_data[f'{user_id}_waiting'] = f'set_window:{task_id}'
            # 保存原始 callback query（用于输入完成或出错后返回任务列表并刷新）
            context.user_data[f'{user_id}_window_menu_query'] = query
        elif data == "back_task_menu":
            # 返回到任务管理菜单
            keyboard = [
                [InlineKeyboardButton("📋 查看任务", callback_data="list_tasks")],
                [InlineKeyboardButton("👤 客户端列表", callback_data="list_clients")],
                [InlineKeyboardButton("➕ 添加客户端", callback_data="add_client_prompt")],
                [InlineKeyboardButton("➕ 添加任务", callback_data="add_task_prompt")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            text = "🗓️ <b>任务管理</b>\n\n支持多客户端、多任务定时推送。\n请选择操作："
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        
    async def handle_setting_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """处理设置输入"""
        user_id = update.effective_user.id
        if not hasattr(context, 'user_data'):
            context.user_data = {}
        waiting = context.user_data.get(f'{user_id}_waiting', '')
        
        try:
            if waiting == 'add_listen_link':
                chat_id = await self._extract_chat_id_from_link(text.strip())
                if chat_id:
                    await self.state.add_listen(chat_id)
                    chat_info = await self._get_chat_info(chat_id)
                    chat_name = chat_info.get('title', f'目标 {chat_id}') if chat_info else f'目标 {chat_id}'
                    chat_name_escaped = html.escape(str(chat_name))
                    chat_id_escaped = html.escape(str(chat_id))
                    await update.message.reply_text(
                        f"✅ 已为当前任务添加监听群\n\n"
                        f"<b>{chat_name_escaped}</b>\n"
                        f"ID: <code>{chat_id_escaped}</code>",
                        parse_mode="HTML"
                    )
                else:
                    await update.message.reply_text("❌ 无法解析该链接/用户名，请检查格式")
            elif waiting == 'add_push_link':
                chat_id = await self._extract_chat_id_from_link(text.strip())
                if chat_id:
                    await self.state.add_push(chat_id)
                    chat_info = await self._get_chat_info(chat_id)
                    chat_name = chat_info.get('title', f'群组 {chat_id}') if chat_info else f'群组 {chat_id}'
                    chat_name_escaped = html.escape(str(chat_name))
                    chat_id_escaped = html.escape(str(chat_id))
                    await update.message.reply_text(
                        f"✅ 已为当前任务添加推送目标\n\n"
                        f"<b>{chat_name_escaped}</b>\n"
                        f"ID/用户名: <code>{chat_id_escaped}</code>",
                        parse_mode="HTML"
                    )
                else:
                    await update.message.reply_text("❌ 无法从链接中提取群组ID，请检查链接格式")
            elif waiting.startswith('set_filter_'):
                filter_key = waiting.replace('set_filter_', '')
                parts = text.strip().split()
                if len(parts) != 2:
                    await update.message.reply_text("❌ 格式错误，请输入：<code>最小值 最大值</code>", parse_mode="HTML")
                    return
                
                # 解析最小/最大值
                # 对于百分比类型（前十占比 / 最大持仓），要求输入 1-100 的整数，内部以 0-1 存储
                if filter_key in ("top10_ratio", "max_holder_ratio"):
                    def parse_pct_str(s: str):
                        if s.lower() in ("null", "none", "无", "空", "清空", ""):
                            return None
                        try:
                            iv = int(s)
                        except Exception:
                            raise ValueError(f"百分比需为整数，范围 0-100：{s}")
                        if iv < 0 or iv > 100:
                            raise ValueError(f"百分比需在 0-100 之间：{s}")
                        return iv / 100.0
                    try:
                        min_v = parse_pct_str(parts[0])
                        max_v = parse_pct_str(parts[1])
                    except ValueError as e:
                        await update.message.reply_text(f"❌ 格式错误: {e}", parse_mode="HTML")
                        return
                else:
                    # 普通数值解析
                    try:
                        min_v = None if parts[0].lower() in ("null", "none", "无", "空", "清空", "") else float(parts[0])
                    except ValueError:
                        await update.message.reply_text(f"❌ 最小值格式错误：<code>{parts[0]}</code>", parse_mode="HTML")
                        return
                    try:
                        max_v = None if parts[1].lower() in ("null", "none", "无", "空", "清空", "") else float(parts[1])
                    except ValueError:
                        await update.message.reply_text(f"❌ 最大值格式错误：<code>{parts[1]}</code>", parse_mode="HTML")
                        return
                
                await self.state.set_filter(filter_key, min_v, max_v)
                
                filter_names = {
                    "market_cap_usd": "市值(USD)", "liquidity_usd": "池子(USD)",
                    "open_minutes": "开盘时间(分钟)", "top10_ratio": "前十占比",
                    "holder_count": "持有人数", "max_holder_ratio": "最大持仓占比",
                    "trades_5m": "5分钟交易数",
                }
                display_name = filter_names.get(filter_key, filter_key)
                display_name_escaped = html.escape(str(display_name))
                
                # 格式化显示值
                if filter_key in ["top10_ratio", "max_holder_ratio"]:
                    min_str = f"{min_v*100:.1f}%" if min_v is not None else "无限制"
                    max_str = f"{max_v*100:.1f}%" if max_v is not None else "无限制"
                else:
                    min_str = f"{min_v:,.0f}" if min_v is not None else "无限制"
                    max_str = f"{max_v:,.0f}" if max_v is not None else "无限制"
                
                # 清除等待状态
                context.user_data[f'{user_id}_waiting'] = None
                
                # 如果有保存的菜单query，返回菜单页面
                saved_query = context.user_data.get(f'{user_id}_filter_menu_query')
                if saved_query:
                    # 更新菜单显示
                    await self.show_filter_menu(saved_query, edit=True)
                    context.user_data[f'{user_id}_filter_menu_query'] = None
                    await update.message.reply_text(
                        f"✅ <b>{display_name_escaped}</b> 已更新：{min_str} ~ {max_str}\n\n"
                        f"💡 已自动返回菜单，可继续设置其他条件",
                        parse_mode="HTML"
                    )
                else:
                    await update.message.reply_text(
                        f"✅ 筛选条件已更新\n\n<b>{display_name_escaped}</b>\n最小值: {min_str}\n最大值: {max_str}",
                        parse_mode="HTML"
                    )
            elif waiting == 'add_client':
                # 检查是否输入"完成"
                if text.strip().lower() in ('完成', 'done', 'finish'):
                    count = context.user_data.get(f'{user_id}_client_count', 0)
                    context.user_data[f'{user_id}_waiting'] = None
                    context.user_data[f'{user_id}_client_count'] = 0
                    await update.message.reply_text(f"✅ 批量添加完成！共添加 {count} 个客户端")
                    return
                
                # 支持：
                # 1) "name session"（自定义名称）
                # 2) 纯 session 字符串（自动使用该账号 username 作为名称）
                raw = text.strip()
                parts = raw.split(maxsplit=1)
                name = parts[0] if len(parts) == 2 else None
                session = parts[1] if len(parts) == 2 else raw
                if not self.scheduler or not self.scheduler.client_pool:
                    await update.message.reply_text("⚠️ 未启用任务调度/客户端池")
                    return
                try:
                    final_name = await self.scheduler.client_pool.add_client(name, session)
                    count = context.user_data.get(f'{user_id}_client_count', 0) + 1
                    context.user_data[f'{user_id}_client_count'] = count

                    # 为新添加的 MTProto 客户端注册消息监听（用于监听群内其他机器人/用户消息）
                    client = self.scheduler.client_pool.get_client(final_name)
                    if client:
                        @client.on(events.NewMessage)
                        async def _mt_on_message(event):
                            try:
                                chat = await event.get_chat()
                                chat_id = getattr(chat, "id", None)
                                if chat_id is None:
                                    return
                                text_mt = event.raw_text or ""
                                if not text_mt:
                                    return

                                snap_mt = await self.state.snapshot()
                                tasks_mt = snap_mt.get("tasks", {})
                                if not tasks_mt:
                                    return

                                username = getattr(chat, "username", None)
                                name_keys = []
                                if username:
                                    name_keys.append(f"@{username}")

                                matched_tasks: List[str] = []
                                for tid, cfg in tasks_mt.items():
                                    if not cfg.get("enabled"):
                                        continue
                                    listens = cfg.get("listen_chats", [])
                                    if chat_id in listens or any(k in listens for k in name_keys):
                                        matched_tasks.append(tid)

                                if not matched_tasks:
                                    return

                                logger.info(f"📨 [MTProto] Message received from chat {chat_id} for tasks: {matched_tasks}")
                                found_mt = set(CA_PATTERN.findall(text_mt))
                                if not found_mt:
                                    return
                                logger.info(f"🔍 [MTProto] Found {len(found_mt)} CA(s) in message: {[ca[:8] + '...' for ca in found_mt]}")

                                for ca_mt in found_mt:
                                    for tid in matched_tasks:
                                        asyncio.create_task(self._process_ca_bg(chain_hint(ca_mt), ca_mt, task_id=tid))
                            except Exception as e:
                                logger.error(f"❌ MTProto listener error (new client): {e}", exc_info=True)

                    await update.message.reply_text(
                        f"✅ 客户端已添加：{final_name}（第 {count} 个）\n继续上传文件或发送字符串，完成后输入「完成」"
                    )
                except Exception as e:
                    await update.message.reply_text(f"❌ 添加失败: {e}")
            elif waiting == 'add_task':
                name = text.strip()
                if not name:
                    await update.message.reply_text("❌ 任务名称不能为空")
                    return
                created = await self.state.create_task(name)
                if not created:
                    await update.message.reply_text("❌ 任务已存在，请换一个名称")
                    return
                await self.state.set_current_task(name)
                count = context.user_data.get(f'{user_id}_task_count', 0) + 1
                context.user_data[f'{user_id}_task_count'] = count
                await update.message.reply_text(
                    f"✅ 已创建任务并切换为当前：{name}\n"
                    f"（默认暂停，请在任务列表启用；继续配置监听群、推送目标、筛选条件）\n"
                    f"已创建数量：{count}",
                    parse_mode="HTML"
                )
            elif waiting.startswith('set_window:'):
                task_id = waiting.split(':', 1)[1]
                parts = text.strip().split()
                if len(parts) != 2:
                    await update.message.reply_text("❌ 请输入两个值：<code>HH:MM HH:MM</code>，或用 <code>none</code> 代表不限制。", parse_mode="HTML")
                    # 返回任务列表界面，清理等待状态
                    saved_query = context.user_data.get(f'{user_id}_window_menu_query')
                    if saved_query:
                        await self.list_tasks_callback(saved_query)
                    else:
                        await self.show_task_menu(update.message)
                    context.user_data[f'{user_id}_waiting'] = None
                    context.user_data[f'{user_id}_window_menu_query'] = None
                    return
                start_raw, end_raw = parts
                def norm(val):
                    # 支持多种清空方式：none, None, null, Null, 无, 空, 清空
                    val_lower = val.lower().strip()
                    if val_lower in ("none", "null", "无", "空", "清空", ""):
                        return None
                    if len(val) == 5 and val[2] == ":" and val[:2].isdigit() and val[3:].isdigit():
                        h = int(val[:2]); m = int(val[3:])
                        if 0 <= h < 24 and 0 <= m < 60:
                            return f"{h:02d}:{m:02d}"
                    return "invalid"
                start_v = norm(start_raw)
                end_v = norm(end_raw)
                if start_v == "invalid" or end_v == "invalid":
                    await update.message.reply_text("❌ 时间格式错误，请输入 <code>HH:MM HH:MM</code>，或用 <code>none</code> 代表不限制。", parse_mode="HTML")
                    # 输入错误，返回任务列表界面
                    saved_query = context.user_data.get(f'{user_id}_window_menu_query')
                    if saved_query:
                        await self.list_tasks_callback(saved_query)
                    else:
                        await self.show_task_menu(update.message)
                    context.user_data[f'{user_id}_waiting'] = None
                    context.user_data[f'{user_id}_window_menu_query'] = None
                    return
                # 成功解析，保存时间窗并刷新任务列表
                await self.state.set_task_window(task_id, start_v, end_v)
                if self.scheduler:
                    for t in self.scheduler.tasks:
                        if t.get("id") == task_id:
                            t["start_time"] = start_v
                            t["end_time"] = end_v
                            # 立即根据新的时间窗更新任务的启用状态与 next_run，保证自动启停生效
                            try:
                                in_window = self._is_in_time_window(start_v, end_v)
                            except Exception:
                                in_window = True
                            if in_window:
                                t["enabled"] = True
                                t["next_run"] = time.time()
                            else:
                                t["enabled"] = False
                                # 将 next_run 设置为下一个时间窗开始时刻（中国时区）
                                try:
                                    if start_v:
                                        h, m = start_v.split(":")
                                        sh = int(h); sm = int(m)
                                        from datetime import datetime as _dt, timedelta as _td
                                        now_dt = _dt.now(TZ_SHANGHAI)
                                        candidate = now_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
                                        if candidate <= now_dt:
                                            candidate = candidate + _td(days=1)
                                        t["next_run"] = candidate.timestamp()
                                    else:
                                        t["next_run"] = time.time()
                                except Exception:
                                    t["next_run"] = time.time()
                    self.scheduler.client_pool.update_tasks_config(self.scheduler.tasks)
                start_str = start_v or "不限制"
                end_str = end_v or "不限制"
                await update.message.reply_text(f"✅ 已更新任务时间窗：{start_str} ~ {end_str}", parse_mode="HTML")
                saved_query = context.user_data.get(f'{user_id}_window_menu_query')
                if saved_query:
                    await self.list_tasks_callback(saved_query)
                else:
                    await self.show_task_menu(update.message)
                context.user_data[f'{user_id}_waiting'] = None
                context.user_data[f'{user_id}_window_menu_query'] = None
            # 清除等待状态
            context.user_data[f'{user_id}_waiting'] = None
        except ValueError:
            await update.message.reply_text("❌ 输入格式错误，请重试")
        except Exception as e:
            await update.message.reply_text(f"❌ 设置失败: {e}")
    
    async def _extract_chat_id_from_link(self, link: str):
        """从Telegram邀请链接中提取chat_id或username（支持群组/机器人/个人）"""
        import re
        try:
            link_clean = link.strip()
            
            # 如果直接是数字ID（可能是负数，表示群组）
            if link_clean.lstrip('-').isdigit():
                return int(link_clean)
            
            # 如果直接是@username格式，返回字符串
            if link_clean.startswith('@'):
                return link_clean
            
            # 处理私有群邀请链接: https://t.me/joinchat/... 或 https://t.me/+...
            if 'joinchat' in link_clean or link_clean.startswith("https://t.me/+") or link_clean.startswith("t.me/+"):
                try:
                    chat = await self.app.bot.join_chat(link_clean)
                    return chat.id
                except Exception as e:
                    logger.warning(f"Failed to join chat from link {link}: {e}")
                    return None
            
            # 处理公共群/机器人链接: https://t.me/groupname 或 @groupname
            match = re.search(r'(?:t\.me/|@)([a-zA-Z0-9_]+)', link)
            if match:
                username = match.group(1)
                try:
                    # 尝试获取chat信息，如果成功返回ID，否则返回@username字符串
                    chat = await self.app.bot.get_chat(f"@{username}")
                    return chat.id
                except Exception as e:
                    # 如果获取失败，可能是机器人或无效用户名，返回@username字符串
                    logger.debug(f"Failed to get chat from username {username}: {e}, returning @username")
                    return f"@{username}"
            
            return None
        except Exception as e:
            logger.warning(f"Failed to extract chat_id from link {link}: {e}")
            return None

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理文档消息（用于接收 .session 文件）"""
        if not update.message or not update.effective_user:
            return
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id if update.effective_chat else None
        if user_id not in self.admin_ids or chat_id != user_id:
            # 仅在管理员私聊中处理
            return
        if not hasattr(context, 'user_data'):
            context.user_data = {}
        waiting = context.user_data.get(f'{user_id}_waiting', '')
        if waiting != 'add_client':
            return

        if not self.scheduler or not self.scheduler.client_pool:
            await update.message.reply_text("⚠️ 未启用任务调度/客户端池")
            return

        doc = update.message.document
        if not doc:
            return
        try:
            # 保存到本地 sessions 目录
            sessions_dir = Path("sessions")
            sessions_dir.mkdir(parents=True, exist_ok=True)
            filename = doc.file_name or f"{doc.file_unique_id}.session"
            dest = sessions_dir / filename
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(custom_path=str(dest))

            final_name = await self.scheduler.client_pool.add_client(None, str(dest))
            count = context.user_data.get(f'{user_id}_client_count', 0) + 1
            context.user_data[f'{user_id}_client_count'] = count
            await update.message.reply_text(
                f"✅ 已从文件添加客户端：{final_name}（第 {count} 个）\n路径：`{dest}`\n继续上传文件或发送字符串，完成后输入「完成」",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Failed to handle session document: {e}")
            await update.message.reply_text(f"❌ 处理 session 文件失败: {e}")
    
    async def _get_chat_info(self, chat_id) -> Optional[dict]:
        """获取聊天信息（支持群组/机器人/个人）"""
        try:
            # 支持字符串（@username）或整数（chat_id）
            chat = await self.app.bot.get_chat(chat_id)
            chat_type = chat.type
            title = None
            username = None
            
            if chat_type == "private":
                title = f"{chat.first_name or ''} {chat.last_name or ''}".strip() or chat.username or f"用户 {chat.id}"
                username = chat.username
            elif chat_type in ("group", "supergroup"):
                title = chat.title
                username = chat.username
            elif chat_type == "channel":
                title = chat.title
                username = chat.username
            elif chat_type == "bot":
                title = chat.first_name or chat.username or f"机器人 {chat.id}"
                username = chat.username
            else:
                title = getattr(chat, 'title', None) or getattr(chat, 'first_name', None) or f"目标 {chat.id}"
                username = getattr(chat, 'username', None)
            
            return {
                'title': title,
                'username': username,
                'type': chat_type,
                'id': chat.id
            }
        except Exception as e:
            logger.debug(f"Failed to get chat info for {chat_id}: {e}")
            return None
    
    async def list_listen_callback(self, query):
        snap = await self.state.snapshot()
        current = snap.get("current_task")
        tasks = snap.get("tasks", {})
        if not current:
            await query.edit_message_text("⚠️ 请先创建并选择任务，再配置监听群组。", parse_mode="HTML")
            return
        listen_chats = tasks.get(current, {}).get("listen_chats", [])
        if not listen_chats:
            await query.edit_message_text(f"📋 <b>监听群组列表</b>（当前任务：{html.escape(current)}）\n\n暂无监听群组", parse_mode="HTML")
            return
        
        keyboard = []
        text = f"📋 <b>监听群组列表</b>（当前任务：{html.escape(current)}） ({len(listen_chats)}个)\n\n"
        for idx, chat_id in enumerate(listen_chats, 1):
            chat_info = await self._get_chat_info(chat_id)
            chat_name = html.escape(chat_info.get('title', f'群组 {chat_id}') if chat_info else f'群组 {chat_id}')
            text += f"{idx}. <b>{chat_name}</b>\n   ID: <code>{chat_id}</code>\n\n"
            keyboard.append([InlineKeyboardButton(f"❌ 删除 {chat_name}", callback_data=f"del_listen_{chat_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="back_listen")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
    
    async def list_push_callback(self, query):
        snap = await self.state.snapshot()
        current = snap.get("current_task")
        tasks = snap.get("tasks", {})
        if not current:
            await query.edit_message_text("⚠️ 请先创建并选择任务，再配置推送目标。", parse_mode="HTML")
            return
        push_chats = tasks.get(current, {}).get("push_chats", [])
        if not push_chats:
            await query.edit_message_text(f"📋 <b>推送目标列表</b>（当前任务：{html.escape(current)}）\n\n暂无推送目标", parse_mode="HTML")
            return
        
        keyboard = []
        text = f"📋 <b>推送目标列表</b>（当前任务：{html.escape(current)}） ({len(push_chats)}个)\n\n"
        for idx, chat_id in enumerate(push_chats, 1):
            chat_info = await self._get_chat_info(chat_id)
            if chat_info:
                chat_name = html.escape(chat_info.get('title', f'目标 {chat_id}'))
                chat_type = chat_info.get('type', 'unknown')
                username = chat_info.get('username')
                chat_id_display = chat_info.get('id', chat_id)
                
                type_icon = {
                    'group': '👥',
                    'supergroup': '👥',
                    'channel': '📢',
                    'private': '👤',
                    'bot': '🤖'
                }.get(chat_type, '📌')
                
                type_name = {
                    'group': '群组',
                    'supergroup': '群组',
                    'channel': '频道',
                    'private': '个人',
                    'bot': '机器人'
                }.get(chat_type, '目标')
                
                username_str = f" @{html.escape(username)}" if username else ""
                text += f"{idx}. {type_icon} <b>{chat_name}</b> ({type_name})\n   ID: <code>{chat_id_display}</code>{username_str}\n\n"
            else:
                chat_id_escaped = html.escape(str(chat_id))
                text += f"{idx}. 📌 <b>目标</b>\n   ID/用户名: <code>{chat_id_escaped}</code>\n\n"
            keyboard.append([InlineKeyboardButton(f"❌ 删除", callback_data=f"del_push_{chat_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="back_push")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
    
    async def list_tasks_callback(self, query):
        snap = await self.state.snapshot()
        tasks = snap.get("tasks", {})
        current = snap.get("current_task")
        keyboard = []
        lines = []
        if not tasks:
            lines.append("📋 <b>任务列表</b>\n\n暂无任务")
        else:
            lines.append(f"📋 <b>任务列表</b> ({len(tasks)}个)\n")
            # 从 task_scheduler 获取任务的 interval_minutes
            scheduler_tasks = {}
            if self.scheduler:
                for st in self.scheduler.list_tasks():
                    scheduler_tasks[st.get("id")] = st
            
            for tid, cfg in tasks.items():
                # 优先使用 scheduler 中的实际状态（如果存在）
                actual_enabled = cfg.get("enabled")
                if tid in scheduler_tasks:
                    st = scheduler_tasks[tid]
                    actual_enabled = st.get("enabled", actual_enabled)
                    # 如果 scheduler 中的状态与 state 不一致，同步更新 state
                    if actual_enabled != cfg.get("enabled"):
                        await self.state.set_task_enabled(tid, actual_enabled)
                
                # 检查时间窗：如果设置了时间窗且不在时间窗内，强制显示为暂停
                start_time = cfg.get("start_time")
                end_time = cfg.get("end_time")
                has_window = start_time or end_time
                if has_window:
                    in_window = self._is_in_time_window(start_time, end_time)
                    if not in_window:
                        # 不在时间窗内，强制显示为暂停
                        actual_enabled = False
                
                status = "✅ 启用" if actual_enabled else "⏸️ 暂停"
                tag = "（当前）" if tid == current else ""
                listen_count = len(cfg.get("listen_chats", []))
                push_count = len(cfg.get("push_chats", []))
                # 获取定时信息
                interval_minutes = None
                next_run_time = None
                if tid in scheduler_tasks:
                    st = scheduler_tasks[tid]
                    interval_minutes = st.get("interval_minutes")
                    # 获取下次运行时间（UTC+8）
                    next_run_ts = st.get("next_run")
                    if next_run_ts:
                        from datetime import datetime, timezone, timedelta
                        tz_shanghai = timezone(timedelta(hours=8))
                        next_run_dt = datetime.fromtimestamp(next_run_ts, tz=tz_shanghai)
                        next_run_time = next_run_dt.strftime('%m-%d %H:%M')
                
                lines.append(f"• <b>{html.escape(tid)}</b> {tag} | {status}")
                interval_str = f" | ⏰ 每{interval_minutes}分钟" if interval_minutes else ""
                next_run_str = f" | 下次: {next_run_time}" if next_run_time else ""
                window_str = ""
                if start_time or end_time:
                    window_str = f" | 时间窗: {start_time or '--:--'} ~ {end_time or '--:--'}"
                lines.append(f"  监听: {listen_count} | 推送: {push_count}{interval_str}{next_run_str}{window_str}")
                btn_row = []
                if tid == current:
                    btn_row.append(InlineKeyboardButton("✅ 当前", callback_data="noop"))
                else:
                    btn_row.append(InlineKeyboardButton(f"切换 {tid}", callback_data=f"task_select:{tid}"))
                # 检查时间窗，决定是否允许手动启用/禁用（使用上面已经获取的 start_time 和 end_time）
                can_manual_toggle = True
                if has_window:
                    in_window = self._is_in_time_window(start_time, end_time)
                    # 只有在时间窗内或没有设置时间窗时才能手动切换
                    can_manual_toggle = in_window
                
                # 使用实际状态（优先 scheduler）
                if actual_enabled:
                    if can_manual_toggle:
                        btn_row.append(InlineKeyboardButton("⏸️ 暂停", callback_data=f"task_disable:{tid}"))
                    else:
                        btn_row.append(InlineKeyboardButton("⏸️ 暂停", callback_data="noop"))
                else:
                    if can_manual_toggle:
                        btn_row.append(InlineKeyboardButton("▶️ 启用", callback_data=f"task_enable:{tid}"))
                    else:
                        btn_row.append(InlineKeyboardButton("▶️ 启用", callback_data="noop"))
                btn_row.append(InlineKeyboardButton("⏰ 时间窗", callback_data=f"task_window:{tid}"))
                btn_row.append(InlineKeyboardButton("🗑️ 删除", callback_data=f"task_delete:{tid}"))
                keyboard.append(btn_row)
                lines.append("")
        keyboard.append([InlineKeyboardButton("⬅️ 返回", callback_data="back_task_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=reply_markup)

    async def list_clients_callback(self, query):
        if not self.scheduler or not self.scheduler.client_pool:
            await query.edit_message_text("⚠️ 未启用任务调度/客户端池")
            return
        items = self.scheduler.client_pool.describe_clients()
        if not items:
            await query.edit_message_text("👤 <b>客户端列表</b>\n\n暂无客户端", parse_mode="HTML")
            return
        lines = ["👤 <b>客户端列表</b>\n"]
        keyboard = []
        for c in items:
            display_name = c.get('name')
            internal_name = c.get('internal_name') or display_name
            username = c.get('username')
            show_name = f"@{username}" if username else display_name
            lines.append(f"• <b>{show_name}</b> | api_id=<code>{c.get('api_id')}</code>")
            lines.append(f"  session: <code>{c.get('session_type')}</code> (<code>{c.get('session_preview')}</code>)")
            lines.append(f"  状态: {c.get('status')}\n")
            keyboard.append([InlineKeyboardButton(f"❌ 删除 {show_name}", callback_data=f"del_client_{internal_name}")])
        keyboard.append([InlineKeyboardButton("⬅️ 返回", callback_data="back_task_menu")])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def list_filters_callback(self, query):
        snap = await self.state.snapshot()
        current = snap.get("current_task")
        if not current:
            await query.edit_message_text("⚠️ 请先创建并选择任务，再配置筛选条件。", parse_mode="HTML")
            return
        filters_cfg = snap.get("tasks", {}).get(current, {}).get("filters", {})
        text = f"🔍 <b>筛选条件</b>（当前任务：{html.escape(current)}）\n" + self._format_filters(filters_cfg)
        await query.edit_message_text(text, parse_mode="HTML")
    
    async def _format_settings(self, snap):
        """格式化配置信息"""
        text = "⚙️ <b>当前配置</b>\n\n"
        
        listen_chats = snap.get("listen_chats", [])
        text += f"👥 <b>监听群组</b> ({len(listen_chats)}个)\n"
        if listen_chats:
            for chat_id in listen_chats:
                chat_info = await self._get_chat_info(chat_id)
                chat_name = chat_info.get('title', f'群组 {chat_id}') if chat_info else f'群组 {chat_id}'
                chat_name_escaped = html.escape(str(chat_name))
                chat_id_escaped = html.escape(str(chat_id))
                text += f"• <b>{chat_name_escaped}</b> (<code>{chat_id_escaped}</code>)\n"
        else:
            text += "• 暂无\n"
        text += "\n"
        
        push_chats = snap.get("push_chats", [])
        text += f"📤 <b>推送目标</b> ({len(push_chats)}个)\n"
        if push_chats:
            for chat_id in push_chats:
                chat_info = await self._get_chat_info(chat_id)
                if chat_info:
                    chat_name = chat_info.get('title', f'目标 {chat_id}')
                    chat_type = chat_info.get('type', 'unknown')
                    username = chat_info.get('username')
                    chat_id_display = chat_info.get('id', chat_id)
                    
                    # 类型图标和名称
                    type_info = {
                        'group': ('👥', '群组'),
                        'supergroup': ('👥', '群组'),
                        'channel': ('📢', '频道'),
                        'private': ('👤', '个人'),
                        'bot': ('🤖', '机器人')
                    }.get(chat_type, ('📌', '目标'))
                    
                    type_icon, type_name = type_info
                    chat_name_escaped = html.escape(str(chat_name))
                    chat_id_escaped = html.escape(str(chat_id_display))
                    username_str = f" @{html.escape(str(username))}" if username else ""
                    text += f"• {type_icon} <b>{chat_name_escaped}</b> ({type_name}) <code>{chat_id_escaped}</code>{username_str}\n"
                else:
                    chat_id_escaped = html.escape(str(chat_id))
                    text += f"• 📌 <b>目标</b> (<code>{chat_id_escaped}</code>)\n"
        else:
            text += "• 暂无\n"
        text += "\n"
        
        text += "🔍 <b>筛选条件</b>\n"
        filters_cfg = snap.get("filters", {})
        text += self._format_filters(filters_cfg)
        text += "\n"
        
        return text

    async def _process_ca_bg(self, chain: str, ca: str, task_id: Optional[str] = None):
        """后台处理 CA，添加超时与异常保护，避免阻塞主流程"""
        try:
            await asyncio.wait_for(
                self.process_ca(chain, ca, False, task_id=task_id),
                timeout=120.0  # 2分钟超时，防止长期阻塞
            )
        except asyncio.TimeoutError:
            logger.error(f"⏱️  Timeout processing CA {ca[:8]}... (exceeded 120s)")
        except Exception as e:
            logger.error(f"❌ Error processing CA {ca[:8]}...: {e}", exc_info=True)
    
    def _is_in_time_window(self, start_time: Optional[str], end_time: Optional[str]) -> bool:
        """检查当前时间是否在时间窗内（与 task_scheduler.py 中的逻辑一致）"""
        if not start_time and not end_time:
            return True  # 没有设置时间窗，始终允许
        
        now_dt = datetime.now(TZ_SHANGHAI)
        now_minutes = now_dt.hour * 60 + now_dt.minute
        start_minutes = None
        end_minutes = None
        
        try:
            if start_time:
                h, m = start_time.split(":")
                start_minutes = int(h) * 60 + int(m)
            if end_time:
                h, m = end_time.split(":")
                end_minutes = int(h) * 60 + int(m)
        except Exception:
            logger.warning(f"⚠️ Invalid start/end time format: {start_time} - {end_time}")
            return True  # 格式错误时允许运行，避免阻塞
        
        # 判断是否在时间窗内（支持跨天）
        if start_minutes is not None and end_minutes is not None:
            if start_minutes <= end_minutes:
                return start_minutes <= now_minutes <= end_minutes
            else:
                return now_minutes >= start_minutes or now_minutes <= end_minutes
        elif start_minutes is not None:
            return now_minutes >= start_minutes
        elif end_minutes is not None:
            return now_minutes <= end_minutes
        else:
            return True

    def _format_filters(self, filters_cfg):
        """格式化筛选条件"""
        filter_names = {
            "market_cap_usd": "市值(USD)",
            "liquidity_usd": "池子(USD)",
            "open_minutes": "开盘时间(分钟)",
            "top10_ratio": "前十占比",
            "holder_count": "持有人数",
            "max_holder_ratio": "最大持仓占比",
            "trades_5m": "5分钟交易数",
        }
        text = ""
        for key, display_name in filter_names.items():
            f = filters_cfg.get(key, {})
            min_v = f.get("min")
            max_v = f.get("max")
            if min_v is None and max_v is None:
                text += f"• {display_name}: 未设置\n"
            else:
                if key in ["top10_ratio", "max_holder_ratio"]:
                    min_str = f"{min_v*100:.1f}%" if min_v is not None else "无限制"
                    max_str = f"{max_v*100:.1f}%" if max_v is not None else "无限制"
                else:
                    min_str = f"{min_v:,.0f}" if min_v is not None else "无限制"
                    max_str = f"{max_v:,.0f}" if max_v is not None else "无限制"
                text += f"• {display_name}: {min_str} ~ {max_str}\n"
        return text

    async def _setup_commands(self):
        """Setup bot commands menu."""
        # All commands that will appear in the menu
        commands = [
            BotCommand("start", "启动机器人"),
            BotCommand("menu", "查看命令菜单"),
            BotCommand("c", "查询合约地址"),
            BotCommand("settings", "查看当前配置"),
            BotCommand("add_client", "添加MTProto客户端（管理员）"),
            BotCommand("add_task", "添加任务（管理员）"),
            BotCommand("tasks", "查看任务列表（管理员）"),
            BotCommand("task_pause", "暂停任务（管理员）"),
            BotCommand("task_resume", "恢复任务（管理员）"),
            BotCommand("add_listen", "添加监听群组（管理员）"),
            BotCommand("del_listen", "删除监听群组（管理员）"),
            BotCommand("list_listen", "查看监听群组列表（管理员）"),
            BotCommand("add_push", "添加推送群组（管理员）"),
            BotCommand("del_push", "删除推送群组（管理员）"),
            BotCommand("list_push", "查看推送群组列表（管理员）"),
            BotCommand("set_filter", "设置筛选条件（管理员）"),
            BotCommand("list_filters", "查看筛选条件列表（管理员）"),
        ]
        # Set commands menu for all users
        await self.app.bot.set_my_commands(commands)
        print("📋 Bot commands menu configured")

    async def run(self):
        """Run the bot (async)."""
        print("🤖 Bot starting...")
        await self.app.initialize()
        await self.app.start()
        # Setup bot commands menu
        await self._setup_commands()
        print("✅ Bot is running! Send /start to test.")
        await self.app.updater.start_polling(drop_pending_updates=True)
        # Keep running - wait for stop signal
        try:
            # Create an event that will never be set, keeping the loop alive
            stop_event = asyncio.Event()
            await stop_event.wait()
        except KeyboardInterrupt:
            print("\n🛑 Received stop signal...")
        finally:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    async def stop(self):
        if self.app.updater:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()


def chain_hint(address: str) -> str:
    if address.startswith("0x") and len(address) == 42:
        return "bsc"
    if len(address) >= 32 and len(address) <= 44:
        return "solana"
    return "bsc"


def _maybe_float(s: str):
    try:
        return float(s)
    except Exception:
        raise ValueError("not a number")

