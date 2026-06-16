"""Telegram Bot - User Interface for AI Company

Receives messages, validates users, hands off to CEO Agent.
Supports text, voice (transcribed), and file attachments.
"""

import asyncio
import logging
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
from telegram.constants import ParseMode

from src.config import config
from src.ceo.graph import run_ceo
from src.memory.store import episode_memory, get_agent_state

logger = logging.getLogger(__name__)


# ─── Auth ─────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    """Check if user is in allowed list."""
    if not config.telegram_allowed_users:
        return True  # No restrictions
    return str(user_id) in config.telegram_allowed_users


# ─── Handlers ─────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("🚫 你没有访问权限。")
        return
    
    await update.message.reply_text(
        f"👋 你好 {user.first_name}！\n\n"
        f"我是 **AI Company** 的 CEO Agent。\n"
        f"你可以直接给我派任务，我会调度公司内的 AI 部门来完成。\n\n"
        f"**可用部门：**\n"
        f"🔬 Research — 搜索分析\n"
        f"💻 Coding — 写代码和测试\n"
        f"📢 Marketing — 内容营销\n"
        f"⚙️ Operation — 部署运维\n\n"
        f"直接发消息开始吧 👇",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "**AI Company 使用指南**\n\n"
        "直接发任务描述，CEO 会自动分派：\n\n"
        "🔬 **Research**: `帮我调研一下xxx市场`\n"
        "💻 **Coding**: `写一个xxx功能的Python脚本`\n"
        "📢 **Marketing**: `给新产品写一段推广文案`\n"
        "⚙️ **Operation**: `部署一个xxx服务到服务器`\n\n"
        "**命令：**\n"
        "/status — 查看当前状态\n"
        "/memory — 查看记忆中的信息\n"
        "/cancel — 取消当前任务\n"
        "/score N — 设置质量门槛(默认70)",
        parse_mode=ParseMode.MARKDOWN,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current agent state."""
    agent_state = get_agent_state("ceo")
    summary = agent_state.get_summary()
    
    # Truncate for Telegram
    if len(summary) > 4000:
        summary = summary[:3900] + "\n\n... (truncated)"
    
    await update.message.reply_text(
        f"📊 **CEO Status**\n\n{summary}"
    )


async def score_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set quality gate threshold."""
    try:
        new_score = int(context.args[0]) if context.args else 70
        new_score = max(0, min(100, new_score))
        config.gate_final_score = new_score
        await update.message.reply_text(
            f"✅ 质量门槛已设为 {new_score}/100"
        )
    except (IndexError, ValueError):
        await update.message.reply_text("用法: /score 80")


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search memory for relevant context."""
    query = " ".join(context.args) if context.args else "recent"
    results = await episode_memory.search(query, limit=5)
    
    if not results:
        await update.message.reply_text("🧠 没有找到相关记忆。")
        return
    
    lines = ["**🧠 Memory Search**\n"]
    for r in results:
        content = r.get("content", str(r))[:200]
        lines.append(f"• {content}")
    
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3900] + "\n..."
    
    await update.message.reply_text(msg)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current task."""
    agent_state = get_agent_state("ceo")
    current = agent_state.get_task()
    if current:
        agent_state.clear_task()
        await update.message.reply_text(f"❌ 已取消: _{current[:100]}_")
    else:
        await update.message.reply_text("当前没有运行中的任务。")


# ─── Main Message Handler ─────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process incoming messages through the CEO workflow."""
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("🚫 你没有访问权限。")
        return
    
    user_message = update.message.text
    if not user_message:
        return
    
    # Show typing indicator
    await update.message.chat.send_action(action="typing")
    
    # Processing message
    status_msg = await update.message.reply_text(
        f"🤔 CEO 正在分析你的需求...\n_{user_message[:100]}_"
    )
    
    try:
        # Run the CEO workflow
        result = await run_ceo(user_message)
        
        # Build response
        phase = result.get("phase", "unknown")
        score_card = result.get("score_card", {})
        final_output = result.get("final_output", "")
        execution_log = result.get("execution_log", [])
        
        # Score display
        score = score_card.get("score", "N/A")
        decision = score_card.get("decision", "")
        feedback = score_card.get("feedback", "")
        
        # Build the response message
        lines = []
        
        # Phase indicator
        emoji_map = {
            "deliver": "✅",
            "complete": "✅",
            "execute": "⚙️",
            "verify": "🔍",
        }
        emoji = emoji_map.get(phase, "🔄")
        lines.append(f"{emoji} **任务完成** | Phase: `{phase}`")
        
        # Execution log
        if execution_log:
            lines.append("\n📋 **执行过程**")
            for log in execution_log[-5:]:  # Last 5 steps
                lines.append(f"  {log}")
        
        # Score
        if score_card:
            score_emoji = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"
            lines.append(f"\n{score_emoji} **评分**: {score}/100 — {decision}")
            if feedback:
                lines.append(f"  _{feedback[:200]}_")
        
        # Build final message — use plain text to avoid Markdown parse errors
        # (CEO output may contain _, *, [, etc. from file paths and code)
        response = "\n".join(lines)
        if len(response) > 4000:
            response = response[:3900] + "\n\n... (truncated)"
        
        await status_msg.edit_text(response)
        
        # If there's a detailed output, send as plain text (code breaks Markdown/HTML)
        if final_output and len(str(final_output)) > 100:
            detail = str(final_output)[:4000]
            await update.message.reply_text(
                f"📄 详细结果:\n\n{detail}",
            )
    
    except Exception as e:
        logger.exception("CEO workflow failed")
        # 安全：不将异常详情暴露给用户
        error_type = type(e).__name__
        await status_msg.edit_text(
            f"❌ 处理出错了 ({error_type})\n\n请稍后重试。如问题持续，请联系管理员。"
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages - transcribe then process."""
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("🚫 你没有访问权限。")
        return
    
    await update.message.reply_text(
        "🎤 语音消息暂不支持自动转写。请发文字消息。",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors."""
    logger.error(f"Update {update} caused error: {context.error}")


# ─── Bot Setup ────────────────────────────────────

def create_bot() -> Application:
    """Create and configure the Telegram bot application."""
    
    app = Application.builder().token(config.telegram_bot_token).build()
    
    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("score", score_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    
    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    # Errors
    app.add_error_handler(error_handler)
    
    return app


async def start_bot():
    """Start the Telegram bot (called from asyncio.run)."""
    logger.info("Starting AI Company Telegram Bot...")
    
    app = create_bot()
    
    # Initialize and start, then poll
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    logger.info("Bot is running. Press Ctrl+C to stop.")
    
    try:
        # Keep running until stopped
        stop_event = asyncio.Event()
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
