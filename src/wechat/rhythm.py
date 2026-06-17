"""Rhythm Controller — decides IF and WHEN to reply.

This is what makes the AI NOT feel like a bot. Real people don't:
- Reply immediately every time
- Reply to every message
- Always use the same delay

Usage:
    from src.wechat.rhythm import RhythmController
    rhythm = RhythmController()
    decision = rhythm.should_reply(contact, last_message)
    if decision["should_reply"]:
        time.sleep(decision["delay"])
"""

import random
import logging
from typing import Optional
from datetime import datetime

logger = logging.getLogger("ai_company.wechat.rhythm")


# Delay ranges in seconds, by closeness bracket
DELAY_MAP = {
    "close":   (10, 120),    # Close friends: reply quickly
    "normal":  (30, 300),    # Normal: casual delay
    "distant": (60, 600),    # Distant: longer delay
    "client":  (120, 900),   # Client: wait a bit longer
}

# Probability of replying to non-question messages
REPLY_PROBABILITY = {
    "close":   0.9,
    "normal":  0.7,
    "distant": 0.5,
    "client":  0.9,  # Always reply to clients
}

# Messages that usually don't need a reply
NO_REPLY_PATTERNS = [
    "哈哈", "哈哈哈", "嗯", "哦", "好", "行", "ok", "OK",
    "知道了", "明白了", "懂了", "收到",
]


def _message_ends_conversation(text: str) -> bool:
    """Check if a message naturally ends a conversation."""
    text = text.strip().lower()
    return text in NO_REPLY_PATTERNS


def _message_asks_question(text: str) -> bool:
    """Check if a message asks a question."""
    text = text.strip()
    return "?" in text or "？" in text or any(
        kw in text for kw in ["吗", "呢", "不", "没", "怎么", "什么", "谁", "哪", "多少", "可以"]
    )


class RhythmController:
    """Controls when and whether to reply, with human-like variability."""
    
    def __init__(self, persona=None):
        self.persona = persona  # optional Persona reference for mood influence
        self._last_reply_time: dict[str, float] = {}
        self._reply_count: dict[str, int] = {}
    
    def should_reply(self, contact: str, last_message: str, 
                     closeness: int = 50) -> dict:
        """Decide if and when to reply to a message.
        
        Returns {
            "should_reply": bool,
            "delay": float (seconds),
            "reason": str,
        }
        """
        # Determine closeness bracket
        if closeness >= 80:
            bracket = "close"
        elif closeness >= 50:
            bracket = "normal"
        elif closeness >= 30:
            bracket = "distant"
        else:
            bracket = "client"
        
        # 1. Questions always get a reply (but with delay)
        if _message_asks_question(last_message):
            delay = self._calculate_delay(bracket)
            return {
                "should_reply": True,
                "delay": delay,
                "reason": "question_deserves_reply",
            }
        
        # 2. Conversation-ending messages → often don't reply
        if _message_ends_conversation(last_message):
            prob = REPLY_PROBABILITY[bracket] * 0.4  # much lower chance
            if random.random() > prob:
                return {
                    "should_reply": False,
                    "delay": 0,
                    "reason": "conversation_ending",
                }
        
        # 3. Normal messages → probabilistic reply
        prob = REPLY_PROBABILITY[bracket]
        if self.persona and self.persona.today_mood == "busy":
            prob *= 0.6
        elif self.persona and self.persona.today_mood == "lazy":
            prob *= 0.5
        
        if random.random() > prob:
            return {
                "should_reply": False,
                "delay": 0,
                "reason": "probabilistic_skip",
            }
        
        delay = self._calculate_delay(bracket)
        return {
            "should_reply": True,
            "delay": delay,
            "reason": "normal_reply",
        }
    
    def _calculate_delay(self, bracket: str) -> float:
        """Calculate human-like reply delay."""
        min_delay, max_delay = DELAY_MAP[bracket]
        
        # Add mood influence
        if self.persona:
            if self.persona.today_mood == "busy":
                min_delay *= 1.5
                max_delay *= 1.5
            elif self.persona.today_mood == "lazy":
                min_delay *= 2
                max_delay *= 2
        
        # Human delays follow a log-normal-ish distribution:
        # Most replies are quick-ish, occasional longer delays
        if random.random() < 0.7:
            # Quick reply (70% of time)
            delay = random.uniform(min_delay, min_delay + (max_delay - min_delay) * 0.3)
        else:
            # Slower reply (30% of time)
            delay = random.uniform(min_delay + (max_delay - min_delay) * 0.3, max_delay)
        
        return round(delay, 1)
    
    def should_end_conversation(self, contact: str, 
                                recent_messages: list) -> bool:
        """Decide if the AI should try to end the conversation.
        
        Real people don't keep chatting forever. They find natural exit points.
        """
        if len(recent_messages) < 3:
            return False
        
        # Check if last 3+ exchanges are all short/no-question
        last_few = recent_messages[-4:]
        all_short_no_question = all(
            len(msg.get("content", "")) < 10 
            and not _message_asks_question(msg.get("content", ""))
            for msg in last_few if msg.get("sender") != "me"
        )
        
        if all_short_no_question:
            return random.random() < 0.6  # 60% chance to end
        
        return False
    
    def get_reply_strategy(self, contact: str, closeness: int = 50,
                           should_end: bool = False) -> dict:
        """Get reply strategy: length, style, whether to ask questions."""
        strategy = {
            "max_length": 50,  # max chars in reply
            "ask_question": False,
            "end_hint": False,
        }
        
        if should_end:
            strategy["max_length"] = 15
            strategy["end_hint"] = True
            strategy["ask_question"] = False
        elif closeness >= 80:
            strategy["max_length"] = 80
            strategy["ask_question"] = random.random() < 0.4
        elif closeness >= 50:
            strategy["max_length"] = 60
            strategy["ask_question"] = random.random() < 0.3
        else:
            strategy["max_length"] = 50
        
        # Mood influence
        if self.persona and self.persona.today_mood in ("busy", "lazy"):
            strategy["max_length"] = int(strategy["max_length"] * 0.5)
            strategy["ask_question"] = False
        
        return strategy
