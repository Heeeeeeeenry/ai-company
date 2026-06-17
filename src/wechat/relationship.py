"""Relationship Model — per-contact relationship profiles.

Different people → different tone, different closeness → different replies.

Usage:
    from src.wechat.relationship import get_relationship
    rel = get_relationship("张三")
    # rel.closeness → 85
    # rel.build_prompt_fragment() → "这是你的大学同学，关系很好..."
"""

import os
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ai_company.wechat.relationship")

RELATIONSHIPS_FILE = os.path.expanduser("~/.ai-company/relationships.json")

# Default closeness by relation type
CLOSENESS_MAP = {
    "family": 95,
    "partner": 95,
    "close_friend": 85,
    "friend": 70,
    "classmate": 65,
    "colleague": 50,
    "acquaintance": 30,
    "client": 20,
    "stranger": 10,
}

RELATION_PROMPTS = {
    "family": "这是家人，说话可以很随意，不用客气",
    "partner": "这是伴侣，说话亲密随意",
    "close_friend": "关系很好的朋友，可以开玩笑、说真心话",
    "friend": "普通朋友，保持友好但不必太热情",
    "classmate": "老同学，可以聊聊往事，语气轻松",
    "colleague": "同事，保持专业但不生硬",
    "acquaintance": "不太熟的人，礼貌但不过分热情",
    "client": "客户，需要保持礼貌和专业",
    "stranger": "不认识的人，谨慎回复",
}


@dataclass
class Relationship:
    """One contact's relationship profile."""
    
    contact: str
    relation: str = "friend"  # family|partner|close_friend|friend|classmate|colleague|acquaintance|client|stranger
    closeness: int = 50       # 0-100
    notes: str = ""           # 备注信息
    facts: list = field(default_factory=list)  # [{"fact": "...", "when": "..."}]
    
    def build_prompt_fragment(self) -> str:
        """Build relationship context for LLM prompt."""
        prompt = RELATION_PROMPTS.get(self.relation, RELATION_PROMPTS["friend"])
        
        if self.notes:
            prompt += f"\n备注：{self.notes}"
        
        if self.facts:
            prompt += "\n\n关于对方的已知信息："
            for f in self.facts[-5:]:  # last 5 facts
                prompt += f"\n- {f['fact']}"
        
        return prompt
    
    def add_fact(self, fact: str):
        """Record a fact about this contact."""
        from datetime import datetime
        self.facts.append({
            "fact": fact,
            "when": datetime.now().isoformat()[:10],
        })
        # Keep last 50 facts
        if len(self.facts) > 50:
            self.facts = self.facts[-50:]
    
    def get_reply_tone(self) -> str:
        """Get tone guidance based on closeness."""
        if self.closeness >= 85:
            return "很随意的语气，可以开玩笑、说脏话（如果对方也这样）、发短句"
        elif self.closeness >= 60:
            return "友好的语气，可以开玩笑，但不要太随意"
        elif self.closeness >= 30:
            return "礼貌但自然的语气，保持适当距离"
        else:
            return "正式礼貌的语气，注意分寸"


class RelationshipStore:
    """Manages all contact relationships."""
    
    def __init__(self):
        self._contacts: dict[str, Relationship] = {}
        self._load()
    
    def _load(self):
        try:
            if os.path.exists(RELATIONSHIPS_FILE):
                with open(RELATIONSHIPS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for name, info in data.items():
                    self._contacts[name] = Relationship(contact=name, **{
                        k: v for k, v in info.items() 
                        if k in Relationship.__dataclass_fields__
                    })
        except Exception as e:
            logger.warning("Failed to load relationships: %s", e)
    
    def _save(self):
        try:
            os.makedirs(os.path.dirname(RELATIONSHIPS_FILE), exist_ok=True)
            data = {}
            for name, rel in self._contacts.items():
                data[name] = {
                    "relation": rel.relation,
                    "closeness": rel.closeness,
                    "notes": rel.notes,
                    "facts": rel.facts,
                }
            with open(RELATIONSHIPS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save relationships: %s", e)
    
    def get(self, contact: str) -> Relationship:
        """Get or create relationship for a contact."""
        if contact not in self._contacts:
            # Try fuzzy match
            for name in self._contacts:
                if contact in name or name in contact:
                    return self._contacts[name]
            # Create default
            self._contacts[contact] = Relationship(contact=contact)
            self._save()
        return self._contacts[contact]
    
    def update(self, contact: str, **kwargs):
        """Update relationship fields."""
        rel = self.get(contact)
        for k, v in kwargs.items():
            if hasattr(rel, k):
                setattr(rel, k, v)
        self._save()
    
    def add_fact(self, contact: str, fact: str):
        """Add a fact about a contact."""
        rel = self.get(contact)
        rel.add_fact(fact)
        self._save()


# Singleton
_store: Optional[RelationshipStore] = None

def get_relationship(contact: str) -> Relationship:
    global _store
    if _store is None:
        _store = RelationshipStore()
    return _store.get(contact)

def update_relationship(contact: str, **kwargs):
    global _store
    if _store is None:
        _store = RelationshipStore()
    _store.update(contact, **kwargs)

def add_fact(contact: str, fact: str):
    global _store
    if _store is None:
        _store = RelationshipStore()
    _store.add_fact(contact, fact)
