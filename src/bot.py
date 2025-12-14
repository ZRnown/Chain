from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Awaitable, Callable, List, Optional, Tuple

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

from .state import StateStore

logger = logging.getLogger("ca_filter_bot.bot")


CA_PATTERN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40}")


class BotApp:
    def __init__(
        self,
        admin_ids: List[int],
        state: StateStore,
        process_ca: Optional[Callable[[str, str, bool], Awaitable[Tuple[Optional[str], Optional[str], Optional[str]]]]],
    ):
        self.admin_ids = admin_ids
        self.state = state
        self.process_ca = process_ca
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
        # 内联按钮回调处理
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        # 监听文本消息（包括按钮点击后的文本输入）
        msg_filter = filters.TEXT & (~filters.COMMAND)
        self.app.add_handler(MessageHandler(msg_filter, self.on_text))

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
                [KeyboardButton("👥 监听群组"), KeyboardButton("📤 推送群组")],
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
            
            text += "📤 **推送群组管理**\n"
            text += "`/add_push [chat_id]` - 添加推送群（无参数则添加当前群）\n"
            text += "`/del_push <chat_id>` - 删除推送群\n"
            text += "`/list_push` - 查看所有推送群\n\n"
            
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
            img_buffer, caption, error_msg = await self.process_ca(chain, ca, True)
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
        
        text = "⚙️ **当前配置**\n\n"
        
        # 监听群组
        listen_chats = snap.get("listen_chats", [])
        text += f"👥 **监听群组** ({len(listen_chats)}个)\n"
        if listen_chats:
            for chat_id in listen_chats:
                text += f"• `{chat_id}`\n"
        else:
            text += "• 暂无\n"
        text += "\n"
        
        # 推送群组
        push_chats = snap.get("push_chats", [])
        text += f"📤 **推送群组** ({len(push_chats)}个)\n"
        if push_chats:
            for chat_id in push_chats:
                text += f"• `{chat_id}`\n"
        else:
            text += "• 暂无\n"
        text += "\n"
        
        # 筛选条件
        text += "🔍 **筛选条件**\n"
        filters_cfg = snap.get("filters", {})
        filter_names = {
            "market_cap_usd": "市值(USD)",
            "liquidity_usd": "池子(USD)",
            "open_minutes": "开盘时间(分钟)",
            "top10_ratio": "前十占比",
            "holder_count": "持有人数",
            "max_holder_ratio": "最大持仓占比",
            "trades_5m": "5分钟交易数",
        }
        for key, display_name in filter_names.items():
            f = filters_cfg.get(key, {})
            min_v = f.get("min")
            max_v = f.get("max")
            if min_v is None and max_v is None:
                text += f"• {display_name}: 未设置\n"
            else:
                min_str = f"{min_v:,.0f}" if min_v is not None else "无限制"
                max_str = f"{max_v:,.0f}" if max_v is not None else "无限制"
                text += f"• {display_name}: {min_str} ~ {max_str}\n"
        text += "\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")

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
            await update.message.reply_text("📋 **监听群组列表**\n\n暂无监听群组\n\n💡 使用 `/add_listen` 添加", parse_mode="Markdown")
            return
        text = f"📋 **监听群组列表** ({len(listen_chats)}个)\n\n"
        for idx, chat_id in enumerate(listen_chats, 1):
            text += f"{idx}. `{chat_id}`\n"
        text += "\n💡 使用 `/del_listen <chat_id>` 删除"
        await update.message.reply_text(text, parse_mode="Markdown")

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
            await update.message.reply_text("📋 **推送群组列表**\n\n暂无推送群组\n\n💡 使用 `/add_push` 添加", parse_mode="Markdown")
            return
        text = f"📋 **推送群组列表** ({len(push_chats)}个)\n\n"
        for idx, chat_id in enumerate(push_chats, 1):
            text += f"{idx}. `{chat_id}`\n"
        text += "\n💡 使用 `/del_push <chat_id>` 删除"
        await update.message.reply_text(text, parse_mode="Markdown")

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
        if is_admin and chat_id == user_id:  # 私聊中的按钮
            await self.handle_admin_button(update, context, text)
            return
        
        # 处理CA监听（群组消息）
        if not self.process_ca:
            return
        snap = await self.state.snapshot()
        if chat_id not in snap["listen_chats"]:
            logger.debug(f"⏭️  Message from non-listened chat {chat_id}, ignoring")
            return
        
        logger.info(f"📨 Message received from chat {chat_id}")
        found = set(CA_PATTERN.findall(text))
        logger.info(f"🔍 Found {len(found)} CA(s) in message: {[ca[:8] + '...' for ca in found]}")
        
        for ca in found:
            # Silently process (errors are logged but not shown to user in auto mode)
            await self.process_ca(chain_hint(ca), ca, False)
    
    async def handle_admin_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """处理管理员按钮菜单"""
        if text == "📊 查看配置":
            await self.cmd_settings(update, context)
        elif text == "👥 监听群组":
            await self.show_listen_menu(update.message)
        elif text == "📤 推送群组":
            await self.show_push_menu(update.message)
        elif text == "🔍 筛选条件":
            await self.show_filter_menu(update.message)
        else:
            # 可能是输入的值（用于设置筛选条件）
            # 检查是否有待处理的设置
            user_id = update.effective_user.id
            if hasattr(context, 'user_data') and context.user_data.get(f'{user_id}_waiting'):
                await self.handle_setting_input(update, context, text)
    
    async def show_listen_menu(self, message):
        """显示监听群组菜单"""
        snap = await self.state.snapshot()
        listen_chats = snap.get("listen_chats", [])
        
        keyboard = [
            [InlineKeyboardButton("➕ 添加群组", callback_data="add_listen_link")],
            [InlineKeyboardButton("📋 查看列表", callback_data="list_listen")],
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        count = len(listen_chats)
        await message.reply_text(
            f"👥 **监听群组管理**\n\n当前有 **{count}** 个监听群组\n\n"
            f"💡 点击「添加群组」后，发送群组邀请链接或公共群链接",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    
    async def show_push_menu(self, message):
        """显示推送群组菜单"""
        snap = await self.state.snapshot()
        push_chats = snap.get("push_chats", [])
        
        keyboard = [
            [InlineKeyboardButton("➕ 添加群组", callback_data="add_push_link")],
            [InlineKeyboardButton("📋 查看列表", callback_data="list_push")],
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        count = len(push_chats)
        await message.reply_text(
            f"📤 **推送群组管理**\n\n当前有 **{count}** 个推送群组\n\n"
            f"💡 点击「添加群组」后，发送群组邀请链接或公共群链接",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    
    async def show_filter_menu(self, message):
        """显示筛选条件菜单"""
        filter_names = {
            "market_cap_usd": "💰 市值(USD)",
            "liquidity_usd": "💧 池子(USD)",
            "open_minutes": "⏰ 开盘时间(分钟)",
            "top10_ratio": "👑 前十占比",
            "holder_count": "👥 持有人数",
            "max_holder_ratio": "🐳 最大持仓占比",
            "trades_5m": "📈 5分钟交易数",
        }
        
        keyboard = []
        for key, name in filter_names.items():
            keyboard.append([InlineKeyboardButton(name, callback_data=f"set_filter_{key}")])
        
        keyboard.append([InlineKeyboardButton("📋 查看所有筛选条件", callback_data="list_filters")])
        keyboard.append([InlineKeyboardButton("🔄 重置所有筛选", callback_data="reset_filters")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text(
            "🔍 **筛选条件设置**\n\n请选择要设置的筛选条件：",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    
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
            chat_id = int(data.split("_")[-1])
            await self.state.del_listen(chat_id)
            await query.edit_message_text(f"✅ 已删除监听群: `{chat_id}`", parse_mode="Markdown")
        elif data == "list_listen":
            await self.list_listen_callback(query)
        elif data == "back_listen":
            await self.show_listen_menu(query.message)
        
        # 推送群组
        elif data == "add_push_link":
            await query.edit_message_text("📝 请发送群组邀请链接或公共群链接：\n\n格式：\n• `https://t.me/joinchat/...` (私有群)\n• `https://t.me/groupname` (公共群)\n• 或直接发送群组ID（数字）")
            if not hasattr(context, 'user_data'):
                context.user_data = {}
            context.user_data[f'{user_id}_waiting'] = 'add_push_link'
        elif data.startswith("del_push_"):
            chat_id = int(data.split("_")[-1])
            await self.state.del_push(chat_id)
            await query.edit_message_text(f"✅ 已删除推送群: `{chat_id}`", parse_mode="Markdown")
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
            await query.edit_message_text(
                f"📝 设置筛选条件: <b>{display_name}</b>\n\n"
                f"请输入范围，格式：<code>最小值 最大值</code>\n"
                f"例如：<code>5000 1000000</code> 或 <code>null 0.15</code>\n\n"
                f"💡 使用 <code>null</code> 表示无限制",
                parse_mode="HTML"
            )
            if not hasattr(context, 'user_data'):
                context.user_data = {}
            context.user_data[f'{user_id}_waiting'] = f'set_filter_{filter_key}'
        elif data == "list_filters":
            await self.list_filters_callback(query)
        elif data == "reset_filters":
            # 重置所有筛选条件
            filter_keys = ["market_cap_usd", "liquidity_usd", "open_minutes", "top10_ratio", 
                          "holder_count", "max_holder_ratio", "trades_5m"]
            for key in filter_keys:
                await self.state.set_filter(key, None, None)
            await query.edit_message_text("✅ 已重置所有筛选条件")
        
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
                    chat_name = chat_info.get('title', f'群组 {chat_id}') if chat_info else f'群组 {chat_id}'
                    await update.message.reply_text(
                        f"✅ 已添加监听群\n\n"
                        f"**{chat_name}**\n"
                        f"ID: `{chat_id}`",
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text("❌ 无法从链接中提取群组ID，请检查链接格式")
            elif waiting == 'add_push_link':
                chat_id = await self._extract_chat_id_from_link(text.strip())
                if chat_id:
                    await self.state.add_push(chat_id)
                    chat_info = await self._get_chat_info(chat_id)
                    chat_name = chat_info.get('title', f'群组 {chat_id}') if chat_info else f'群组 {chat_id}'
                    await update.message.reply_text(
                        f"✅ 已添加推送群\n\n"
                        f"**{chat_name}**\n"
                        f"ID: `{chat_id}`",
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text("❌ 无法从链接中提取群组ID，请检查链接格式")
            elif waiting.startswith('set_filter_'):
                filter_key = waiting.replace('set_filter_', '')
                parts = text.strip().split()
                if len(parts) != 2:
                    await update.message.reply_text("❌ 格式错误，请输入：`最小值 最大值`", parse_mode="Markdown")
                    return
                min_v = None if parts[0].lower() == "null" else float(parts[0])
                max_v = None if parts[1].lower() == "null" else float(parts[1])
                await self.state.set_filter(filter_key, min_v, max_v)
                filter_names = {
                    "market_cap_usd": "市值(USD)", "liquidity_usd": "池子(USD)",
                    "open_minutes": "开盘时间(分钟)", "top10_ratio": "前十占比",
                    "holder_count": "持有人数", "max_holder_ratio": "最大持仓占比",
                    "trades_5m": "5分钟交易数",
                }
                display_name = filter_names.get(filter_key, filter_key)
                min_str = f"{min_v:,.0f}" if min_v is not None else "无限制"
                max_str = f"{max_v:,.0f}" if max_v is not None else "无限制"
                await update.message.reply_text(
                    f"✅ 筛选条件已更新\n\n**{display_name}**\n最小值: {min_str}\n最大值: {max_str}",
                    parse_mode="Markdown"
                )
            # 清除等待状态
            context.user_data[f'{user_id}_waiting'] = None
        except ValueError:
            await update.message.reply_text("❌ 输入格式错误，请重试")
        except Exception as e:
            await update.message.reply_text(f"❌ 设置失败: {e}")
    
    async def _extract_chat_id_from_link(self, link: str) -> Optional[int]:
        """从Telegram邀请链接中提取chat_id"""
        import re
        try:
            # 如果直接是数字ID（可能是负数，表示群组）
            link_clean = link.strip()
            if link_clean.lstrip('-').isdigit():
                return int(link_clean)
            
            # 处理私有群邀请链接: https://t.me/joinchat/...
            # 对于joinchat链接，bot需要先加入群组才能获取chat_id
            # 我们尝试通过join_chat方法加入，然后获取chat_id
            if 'joinchat' in link or 'join' in link:
                try:
                    # 提取邀请token
                    match = re.search(r'joinchat/([a-zA-Z0-9_-]+)', link)
                    if match:
                        invite_hash = match.group(1)
                        # 尝试加入群组
                        chat = await self.app.bot.join_chat(link)
                        return chat.id
                except Exception as e:
                    logger.warning(f"Failed to join chat from link {link}: {e}")
                    return None
            
            # 处理公共群链接: https://t.me/groupname 或 @groupname
            match = re.search(r'(?:t\.me/|@)([a-zA-Z0-9_]+)', link)
            if match:
                username = match.group(1)
                try:
                    chat = await self.app.bot.get_chat(f"@{username}")
                    return chat.id
                except Exception as e:
                    logger.warning(f"Failed to get chat from username {username}: {e}")
                    return None
            
            return None
        except Exception as e:
            logger.warning(f"Failed to extract chat_id from link {link}: {e}")
            return None
    
    async def _get_chat_info(self, chat_id: int) -> Optional[dict]:
        """获取群组信息"""
        try:
            chat = await self.app.bot.get_chat(chat_id)
            return {
                'title': chat.title,
                'username': chat.username,
                'type': chat.type
            }
        except Exception as e:
            logger.debug(f"Failed to get chat info for {chat_id}: {e}")
            return None
    
    async def list_listen_callback(self, query):
        snap = await self.state.snapshot()
        listen_chats = snap.get("listen_chats", [])
        if not listen_chats:
            await query.edit_message_text("📋 **监听群组列表**\n\n暂无监听群组", parse_mode="Markdown")
            return
        
        keyboard = []
        text = f"📋 **监听群组列表** ({len(listen_chats)}个)\n\n"
        for idx, chat_id in enumerate(listen_chats, 1):
            chat_info = await self._get_chat_info(chat_id)
            chat_name = chat_info.get('title', f'群组 {chat_id}') if chat_info else f'群组 {chat_id}'
            text += f"{idx}. **{chat_name}**\n   ID: `{chat_id}`\n\n"
            keyboard.append([InlineKeyboardButton(f"❌ 删除 {chat_name}", callback_data=f"del_listen_{chat_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="back_listen")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    
    async def list_push_callback(self, query):
        snap = await self.state.snapshot()
        push_chats = snap.get("push_chats", [])
        if not push_chats:
            await query.edit_message_text("📋 **推送群组列表**\n\n暂无推送群组", parse_mode="Markdown")
            return
        
        keyboard = []
        text = f"📋 **推送群组列表** ({len(push_chats)}个)\n\n"
        for idx, chat_id in enumerate(push_chats, 1):
            chat_info = await self._get_chat_info(chat_id)
            chat_name = chat_info.get('title', f'群组 {chat_id}') if chat_info else f'群组 {chat_id}'
            text += f"{idx}. **{chat_name}**\n   ID: `{chat_id}`\n\n"
            keyboard.append([InlineKeyboardButton(f"❌ 删除 {chat_name}", callback_data=f"del_push_{chat_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="back_push")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    
    async def list_filters_callback(self, query):
        snap = await self.state.snapshot()
        filters_cfg = snap.get("filters", {})
        text = self._format_filters(filters_cfg)
        await query.edit_message_text(text, parse_mode="Markdown")
    
    def _format_settings(self, snap):
        """格式化配置信息"""
        text = "⚙️ **当前配置**\n\n"
        
        listen_chats = snap.get("listen_chats", [])
        text += f"👥 **监听群组** ({len(listen_chats)}个)\n"
        if listen_chats:
            for chat_id in listen_chats:
                text += f"• `{chat_id}`\n"
        else:
            text += "• 暂无\n"
        text += "\n"
        
        push_chats = snap.get("push_chats", [])
        text += f"📤 **推送群组** ({len(push_chats)}个)\n"
        if push_chats:
            for chat_id in push_chats:
                text += f"• `{chat_id}`\n"
        else:
            text += "• 暂无\n"
        text += "\n"
        
        text += "🔍 **筛选条件**\n"
        filters_cfg = snap.get("filters", {})
        text += self._format_filters(filters_cfg)
        text += "\n"
        
        return text
    
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

