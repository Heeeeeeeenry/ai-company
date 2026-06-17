"""Conversation Manager — orchestrates the AI chat pipeline.

Architecture:
    MessageParser  →  MemoryAgent  →  RelationshipAgent  →  PersonaAgent
         ↓                                                     ↓
    RhythmController  ←──────────────────────────────────  ConversationManager
         ↓                                                     ↓
    ReplyGenerator  →  [Approval?]  →  WechatAgent

Priorities (by the user's design):
    P0: Contact profile + Long-term memory
    P1: Persona system + Reply rhythm
    P2: Mood state machine
    P3: Knowledge graph
    P4: Full auto-send (what most people start with, and fail)
"""

import os
import time
import random
import logging
from typing import Optional
from datetime import datetime

logger = logging.getLogger("ai_company.wechat.conversation")


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


REPLY_PROMPT = """__PERSONA__

__RELATIONSHIP__
__MEMORY__

下面是最近的微信对话：
__CONVERSATION__

__STRATEGY__

请以我的身份回复对方。只输出回复内容，不要加引号或任何解释。"""


class ConversationManager:
    """Full pipeline: read → think → decide → reply → send.
    
    Usage:
        mgr = ConversationManager("小号")
        mgr.step()  # one turn: read, decide, maybe reply
        mgr.run(turns=5)  # continuous loop
    """
    
    def __init__(self, contact: str, max_history: int = 30):
        self.contact = contact
        self.max_history = max_history
        self.history: list = []  # [{"sender": str, "content": str, "time": str}]
        self.in_chat = False
        
        # Lazy imports
        from src.wechat.coordinator import WechatCoordinator
        from src.wechat.vision import WechatVision
        from src.wechat.persona import get_persona
        from src.wechat.relationship import get_relationship
        from src.wechat.memory import MemoryAgent
        from src.wechat.rhythm import RhythmController
        
        self.coordinator = WechatCoordinator()
        self.vision = WechatVision()
        self.persona = get_persona()
        self.relationship = get_relationship(contact)
        self.memory = MemoryAgent()
        self.rhythm = RhythmController(persona=self.persona)
        
        self._llm = None
    
    def _get_llm(self):
        if self._llm is None:
            from openai import OpenAI
            self._llm = OpenAI(
                api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                base_url="https://api.deepseek.com/v1",
            )
        return self._llm
    
    # ═══════════════════════════════════════════
    # Step 1: Read messages (Qwen-VL)
    # ═══════════════════════════════════════════
    
    def read_recent(self, count: int = 8) -> list:
        """Read last N messages from chat window via Qwen-VL."""
        prompt = PROMPT_READ_MESSAGES.replace("__MAX__", str(count))
        img = self.vision._capture()
        if not img:
            return []
        
        result = self.vision._ask(img, prompt, max_tokens=500)
        if not result:
            return []
        
        messages = result if isinstance(result, list) else result.get("messages", [])
        
        # Deduplicate and add to history
        for msg in messages:
            if isinstance(msg, dict) and msg.get("content"):
                sender = msg["sender"]
                content = msg["content"]
                # Skip duplicates
                if self.history and self.history[-1]["content"] == content:
                    continue
                self.history.append({
                    "sender": sender,
                    "content": content,
                    "time": datetime.now().isoformat(),
                })
        
        # Trim history
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        
        return messages
    
    # ═══════════════════════════════════════════
    # Step 2: Extract memories from conversation
    # ═══════════════════════════════════════════
    
    def _extract_memories(self):
        """Extract facts from recent conversation and store."""
        recent = [m for m in self.history[-10:] if m["sender"] != "me"]
        if len(recent) >= 3:
            self.memory.extract_facts(self.contact, self.history[-10:])
    
    # ═══════════════════════════════════════════
    # Step 3: Generate reply (with full context)
    # ═══════════════════════════════════════════
    
    def generate_reply(self) -> Optional[str]:
        """Generate a reply using the full pipeline context."""
        if not self.history:
            return None
        
        # Check if last message is from me
        last_msg = self.history[-1]
        if last_msg["sender"] == "me":
            return None
        
        # ── Rhythm: should we even reply? ──
        rhythm_decision = self.rhythm.should_reply(
            self.contact,
            last_msg["content"],
            closeness=self.relationship.closeness,
        )
        
        if not rhythm_decision["should_reply"]:
            logger.info(
                "Skipping reply (reason: %s, message: %s)",
                rhythm_decision["reason"],
                last_msg["content"][:30],
            )
            return None
        
        # ── Extract memories from recent conversation ──
        self._extract_memories()
        
        # ── Build conversation context ──
        recent = self.history[-12:]
        conv_lines = []
        for msg in recent:
            role = "我" if msg["sender"] == "me" else msg["sender"]
            conv_lines.append(f"{role}: {msg['content']}")
        conversation_text = "\n".join(conv_lines)
        
        # ── Get reply strategy ──
        should_end = self.rhythm.should_end_conversation(
            self.contact, self.history
        )
        strategy = self.rhythm.get_reply_strategy(
            self.contact, self.relationship.closeness, should_end
        )
        
        # ── Build strategy hints ──
        strategy_lines = []
        if strategy["end_hint"]:
            strategy_lines.append("- 对方看起来不想继续聊了，回复要简短收尾，不要开新话题")
        if strategy["ask_question"]:
            strategy_lines.append("- 可以自然地反问对方一个问题，保持对话活跃")
        strategy_lines.append(f"- 回复控制在{strategy['max_length']}字以内")
        strategy_text = "\n".join(strategy_lines)
        
        # ── Build full prompt ──
        memory_text = self.memory.retrieve(
            self.contact, last_msg["content"]
        )
        
        prompt = (
            REPLY_PROMPT
            .replace("__PERSONA__", self.persona.build_system_prompt())
            .replace("__RELATIONSHIP__", self.relationship.build_prompt_fragment())
            .replace("__MEMORY__", memory_text if memory_text else "")
            .replace("__CONVERSATION__", conversation_text)
            .replace("__STRATEGY__", strategy_text)
        )
        
        logger.debug("Reply prompt length: %d chars", len(prompt))
        
        try:
            client = self._get_llm()
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=250,
                temperature=0.85,
            )
            reply = resp.choices[0].message.content.strip()
            reply = reply.strip('"').strip("'").strip("「").strip("」")
            reply = reply.strip()
            
            if reply and len(reply) > 0:
                return reply
            return None
            
        except Exception as e:
            logger.error("Reply generation failed: %s", e)
            return None
    
    # ═══════════════════════════════════════════
    # Step 4: Send reply (with rhythm delay)
    # ═══════════════════════════════════════════
    
    def send(self, message: str, contact: Optional[str] = None) -> dict:
        """Send a message, using quick_send if already in chat."""
        contact = contact or self.contact
        
        if self.in_chat:
            result = self.coordinator.quick_send(message)
        else:
            result = self.coordinator.send(contact, message)
            if result.get("success"):
                self.in_chat = True
        
        if result.get("success"):
            self.history.append({
                "sender": "me",
                "content": message,
                "time": datetime.now().isoformat(),
            })
        
        return result
    
    # ═══════════════════════════════════════════
    # Active conversation: generate opening message
    # ═══════════════════════════════════════════
    
    def _generate_opener(self) -> Optional[str]:
        """Generate a natural opening message to start a conversation.
        
        Uses persona + relationship context. Should sound like a real person
        casually starting a chat — not a bot greeting.
        """
        prompt = f"""你是一个正在用微信主动找人聊天的人。
        
{self.persona.build_system_prompt()}

{self.relationship.build_prompt_fragment()}

现在你想主动和{self.contact}聊聊天。请用一句话开场，要自然随意：
- 可以问对方在干嘛
- 可以分享一件小事
- 可以打个招呼
- 不要太正式，像真人聊天
- 一句话就够了

直接输出开场白，不要加引号："""
        
        try:
            client = self._get_llm()
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=80,
                temperature=0.95,
            )
            opener = resp.choices[0].message.content.strip()
            opener = opener.strip('"').strip("'").strip("「").strip("」")
            return opener if opener else None
        except Exception as e:
            logger.error("Failed to generate opener: %s", e)
            return None
    
    # ═══════════════════════════════════════════
    # Step 5: One full turn
    # ═══════════════════════════════════════════
    
    def step(self) -> Optional[str]:
        """One turn: read → extract memories → decide → generate → send.
        
        Returns the sent reply text, or None if no reply was sent.
        """
        # Read latest messages
        self.read_recent(count=8)
        
        if not self.history:
            logger.info("No messages found")
            return None
        
        last_msg = self.history[-1]
        logger.info(
            "Last message: [%s] %s",
            last_msg["sender"],
            last_msg["content"][:50],
        )
        
        # Generate reply (includes rhythm check internally)
        reply = self.generate_reply()
        if not reply:
            return None
        
        # Apply rhythm delay before sending
        rhythm_decision = self.rhythm.should_reply(
            self.contact,
            last_msg["content"],
            closeness=self.relationship.closeness,
        )
        
        delay = rhythm_decision.get("delay", random.uniform(30, 120))
        logger.info("Waiting %.1fs before replying...", delay)
        time.sleep(delay)
        
        # Send
        result = self.send(reply)
        if result.get("success"):
            logger.info("✓ Sent: %s", reply[:50])
            return reply
        else:
            logger.error("✗ Failed to send: %s", result.get("error"))
            return None
    
    # ═══════════════════════════════════════════
    # Step 6: Continuous loop
    # ═══════════════════════════════════════════
    
    def run(self, turns: int = 5, poll_interval: float = 8.0) -> list:
        """Run N turns of read-and-reply.
        
        Args:
            turns: Max number of reply turns
            poll_interval: Seconds between checks
        
        Returns list of reply dicts: {"text": str, "sent": bool, "error": str|None}
        """
        replies = []
        
        for turn in range(turns):
            logger.info("=== Turn %d/%d ===", turn + 1, turns)
            
            # Read + generate
            self.read_recent(count=8)
            
            if not self.history:
                logger.info("No messages found in chat")
                continue
            
            last_msg = self.history[-1]
            if last_msg["sender"] == "me":
                logger.info("Last message is mine, skipping")
                time.sleep(poll_interval)
                continue
            
            reply = self.generate_reply()
            if not reply:
                continue
            
            # Send
            result = self.send(reply)
            sent = result.get("success", False)
            error = result.get("error") if not sent else None
            
            replies.append({
                "text": reply,
                "sent": sent,
                "error": error,
            })
            
            if sent:
                logger.info("✓ Turn %d sent: %s", turn + 1, reply[:50])
            else:
                logger.error("✗ Turn %d failed (%s): %s", turn + 1, error, reply[:50])
            
            if turn < turns - 1:
                logger.info("Waiting %.1fs for next turn...", poll_interval)
                time.sleep(poll_interval)
        
        sent_count = sum(1 for r in replies if r["sent"])
        if sent_count > 0:
            logger.info("Completed: %d/%d replies sent", sent_count, len(replies))
        else:
            logger.info("Completed: no replies sent")
        
        return replies
