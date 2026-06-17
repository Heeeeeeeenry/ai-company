"""Persona System — defines who "I" am when chatting.

This is the core identity. Without this, every AI reply sounds like ChatGPT.
With it, replies sound like ME.

Usage:
    from src.wechat.persona import Persona, get_persona
    p = get_persona()
    prompt = p.build_system_prompt()  # inject into LLM
"""

import os
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ai_company.wechat.persona")

PERSONA_FILE = os.path.expanduser("~/.ai-company/persona.json")

DEFAULT_PERSONA = {
    "name": "Lee",
    
    "talk_style": [
        "简洁，像真人聊微信",
        "经常发短句，几乎不会长篇大论",
        "可以适当使用语气词：哦、呢、吧、哈、啊",
        "偶尔开玩笑，不一本正经",
        "不用'你好''请问'等客套开头",
    ],
    
    "habits": [
        "消息通常1-2句话",
        "很少用表情包，偶尔用emoji",
        "有时候只回复一个字：好、行、嗯",
        "不想继续聊的时候会敷衍：哈哈、嗯嗯",
        "不会主动问太多问题",
    ],
    
    "forbidden": [
        "不要说'很高兴和你聊天'之类的话",
        "不要像客服一样说话",
        "不要用'当然可以''没问题'开头",
        "不要长篇大论超过3句话",
        "不要使用Markdown格式",
    ],
    
    "today_mood": "normal",  # normal | busy | tired | happy | lazy
    "context": "正在忙工作，偶尔看一眼手机",
}


@dataclass
class Persona:
    """My chat persona — loaded from JSON, editable."""
    
    name: str = "Lee"
    talk_style: list = field(default_factory=list)
    habits: list = field(default_factory=list)
    forbidden: list = field(default_factory=list)
    today_mood: str = "normal"
    context: str = ""
    
    def build_system_prompt(self) -> str:
        """Build the system prompt fragment for LLM reply generation."""
        
        mood_hints = {
            "normal": "正常状态，回复自然随意",
            "busy": "比较忙，回复尽量简短，不想展开话题",
            "tired": "有点累，回复敷衍一点",
            "happy": "心情不错，可以稍微热情一点",
            "lazy": "很懒，能少打字就少打字",
        }
        
        lines = [
            f"你叫{self.name}，正在用微信和人聊天。以下是你的聊天风格：",
            "",
            "【说话方式】",
        ]
        for s in self.talk_style:
            lines.append(f"- {s}")
        
        lines.append("")
        lines.append("【习惯】")
        for h in self.habits:
            lines.append(f"- {h}")
        
        lines.append("")
        lines.append("【绝对不要】")
        for f in self.forbidden:
            lines.append(f"- {f}")
        
        lines.append("")
        lines.append("【今天状态】")
        lines.append(f"- {mood_hints.get(self.today_mood, mood_hints['normal'])}")
        if self.context:
            lines.append(f"- {self.context}")
        
        return "\n".join(lines)
    
    def update_mood(self, mood: str):
        """Update today's mood."""
        valid = {"normal", "busy", "tired", "happy", "lazy"}
        if mood in valid:
            self.today_mood = mood
            self._save()
    
    def _save(self):
        """Persist to disk."""
        data = {
            "name": self.name,
            "talk_style": self.talk_style,
            "habits": self.habits,
            "forbidden": self.forbidden,
            "today_mood": self.today_mood,
            "context": self.context,
        }
        try:
            os.makedirs(os.path.dirname(PERSONA_FILE), exist_ok=True)
            with open(PERSONA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save persona: %s", e)


def get_persona() -> Persona:
    """Load persona from disk, falling back to defaults."""
    try:
        if os.path.exists(PERSONA_FILE):
            with open(PERSONA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Persona(**{k: v for k, v in data.items() if k in Persona.__dataclass_fields__})
    except Exception as e:
        logger.warning("Failed to load persona, using defaults: %s", e)
    
    return Persona(**DEFAULT_PERSONA)
