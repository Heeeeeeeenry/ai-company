"""AI Conversation Engine — read WeChat messages, generate replies, send back.

Pattern:
  1. Screenshot chat window → Qwen-VL reads last N messages
  2. DeepSeek generates contextual reply based on conversation history
  3. quick_send() dispatches reply (skip contact search, already in chat)

Memory: in-session conversation log, survives within goudan session lifetime.
"""

import logging
import time
import json
from typing import Optional

logger = logging.getLogger("ai_company.wechat.conversation")


# ─── Qwen-VL prompt: read recent messages from chat screenshot ───

PROMPT_READ_MESSAGES = """Analyze this WeChat screenshot of a chat conversation.
Extract the last __MAX__ messages visible in the chat window.
For each message, identify:
  - sender: "me" if it's the current user's message (right-aligned, green/white bubble), 
            or the contact's name if from the other person
  - content: the exact text of the message

Return ONLY JSON array (oldest first):
[
  {"sender": "me" or "contact_name", "content": "message text"},
  ...
]"""


class Conversation:
    """AI-powered WeChat conversation handler.
    
    Tracks context in-session. Each Conversation instance remembers:
      - contact name
      - message history (sender/content pairs)
      - whether we're currently in the chat window
    
    Usage:
      conv = Conversation("小号")
      conv.reply()  # reads, generates, sends one reply
      conv.listen_and_reply_loop(max_turns=5)  # continuous loop
    """
    
    def __init__(self, contact: str, max_history: int = 20):
        self.contact = contact
        self.max_history = max_history
        self.history: list = []  # [{"sender": "...", "content": "..."}, ...]
        self.in_chat = False  # set to True after first send()
        
        from src.wechat.coordinator import WechatCoordinator
        from src.wechat.vision import WechatVision
        self.coordinator = WechatCoordinator()
        self.vision = WechatVision()
        
        # DeepSeek client for reply generation
        self._llm = None
    
    def _get_llm(self):
        if self._llm is None:
            import os
            from openai import OpenAI
            self._llm = OpenAI(
                api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                base_url="https://api.deepseek.com/v1",
            )
        return self._llm
    
    # ─── Message reading (vision) ───
    
    def read_recent(self, count: int = 5) -> list:
        """Read the last `count` messages from the WeChat chat window via Qwen-VL.
        
        Returns list of {"sender": str, "content": str}, newest last.
        """
        prompt = PROMPT_READ_MESSAGES.replace("__MAX__", str(count))
        img = self.vision._capture()
        if not img:
            logger.warning("Failed to capture screenshot for message reading")
            return []
        
        result = self.vision._ask(img, prompt, max_tokens=500)
        if not result:
            logger.warning("Qwen-VL returned None for message reading")
            return []
        
        # result should be a JSON array
        if isinstance(result, list):
            messages = result
        elif isinstance(result, dict) and "messages" in result:
            messages = result["messages"]
        else:
            logger.warning("Unexpected Qwen-VL response format: %s", str(result)[:200])
            return []
        
        # Update local history (deduplicate by checking last N)
        for msg in messages:
            if isinstance(msg, dict) and msg.get("content"):
                self._add_to_history(msg["sender"], msg["content"])
        
        return messages
    
    def _add_to_history(self, sender: str, content: str):
        """Add message to local history, avoiding exact duplicates."""
        if self.history and self.history[-1]["content"] == content:
            return  # skip exact duplicate
        self.history.append({"sender": sender, "content": content})
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
    
    # ─── AI reply generation (DeepSeek) ───
    
    def generate_reply(self, context_window: int = 10) -> Optional[str]:
        """Generate a contextual reply using DeepSeek based on recent messages.
        
        Reads the latest messages, then generates a natural response.
        """
        # Read latest messages first
        self.read_recent(count=8)
        
        if not self.history:
            logger.warning("No message history available for reply generation")
            return None
        
        # Build conversation context for the LLM
        recent = self.history[-context_window:]
        context_lines = []
        for msg in recent:
            role = "我" if msg["sender"] == "me" else msg["sender"]
            context_lines.append(f"{role}: {msg['content']}")
        
        conversation_text = "\n".join(context_lines)
        
        # Check if the last message is from us (don't reply to ourselves)
        if recent[-1]["sender"] == "me":
            logger.info("Last message is from me, waiting for other person...")
            return None
        
        prompt = f"""你是一个正在帮我和别人微信聊天的助手。以下是最近的对话：

{conversation_text}

请以我的身份，用自然口语化的中文回复对方。要求：
- 简短自然，像真人聊天，不要长篇大论
- 可以适当使用语气词（哦、呢、吧、哈等）
- 保持轻松友好的语气
- 不要用"你好""请问"等客套开头
- 只输出回复内容，不要加任何解释或前缀

我的回复："""

        try:
            client = self._get_llm()
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.9,
            )
            reply = resp.choices[0].message.content.strip()
            # Strip quotes that LLM sometimes wraps around
            reply = reply.strip('"').strip("'").strip("「").strip("」")
            return reply
        except Exception as e:
            logger.error("DeepSeek reply generation failed: %s", e)
            return None
    
    # ─── Send reply ───
    
    def send(self, message: str, contact: Optional[str] = None) -> dict:
        """Send a message. Uses quick_send if we're already in chat."""
        contact = contact or self.contact
        
        if self.in_chat:
            result = self.coordinator.quick_send(message)
        else:
            result = self.coordinator.send(contact, message)
            if result.get("success"):
                self.in_chat = True
        
        if result.get("success"):
            self._add_to_history("me", message)
        
        return result
    
    # ─── High-level: one-turn reply ───
    
    def reply(self) -> Optional[str]:
        """Read the latest messages, generate a reply, and send it.
        
        Returns the sent reply text, or None if no reply was needed/sent.
        """
        reply_text = self.generate_reply()
        if not reply_text:
            return None
        
        result = self.send(reply_text)
        if result.get("success"):
            logger.info("Sent reply: %s", reply_text[:50])
            return reply_text
        else:
            logger.error("Failed to send reply: %s", result.get("error"))
            return None
    
    # ─── Continuous loop ───
    
    def listen_and_reply(self, max_turns: int = 3, poll_interval: float = 5.0) -> list:
        """Continuously listen for new messages and reply.
        
        Args:
            max_turns: Maximum number of reply turns
            poll_interval: Seconds to wait between checks
        
        Returns list of sent replies.
        """
        replies = []
        last_msg_count = len(self.history)
        
        for turn in range(max_turns):
            logger.info("Turn %d/%d: reading messages...", turn + 1, max_turns)
            
            self.read_recent(count=5)
            new_count = len(self.history)
            
            if new_count == last_msg_count:
                logger.info("No new messages, waiting %.1fs...", poll_interval)
                time.sleep(poll_interval)
                continue
            
            # Check if last message is from us
            if self.history and self.history[-1]["sender"] == "me":
                last_msg_count = new_count
                logger.info("Last message is mine, waiting...")
                time.sleep(poll_interval)
                continue
            
            reply = self.reply()
            if reply:
                replies.append(reply)
            
            last_msg_count = len(self.history)
            if turn < max_turns - 1:
                time.sleep(poll_interval)
        
        return replies
