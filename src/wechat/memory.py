"""Long-term Memory — fact extraction and retrieval from conversations.

This is what makes the AI feel human across time. Without memory,
every conversation is like talking to a stranger. With it, the AI
remembers things like "张三7月去日本" and brings it up naturally later.

Usage:
    from src.wechat.memory import MemoryAgent
    mem = MemoryAgent()
    mem.extract_facts("张三", [{"sender": "张三", "content": "我7月去日本"}])
    mem.retrieve("张三", "最近怎么样")
"""

import os
import logging
from typing import Optional
from datetime import datetime

logger = logging.getLogger("ai_company.wechat.memory")


FACT_EXTRACTION_PROMPT = """从以下微信对话中提取关于 __CONTACT__ 的事实。

规则：
1. 只提取明确陈述的事实，不要推测
2. 每条事实一行，格式：- 事实内容
3. 重要的事实类型：
   - 行程/计划（去了哪里、要去哪里）
   - 工作/职业变化
   - 兴趣爱好
   - 家庭情况
   - 健康状态
   - 购买/消费
   - 任何"我..."句式
4. 如果对话中没有新事实，返回"无"
5. 不要提取问候、闲聊等没有信息量的内容

对话：
__CONVERSATION__

提取的事实："""


class MemoryAgent:
    """Extracts facts from conversations and retrieves them when needed."""
    
    def __init__(self):
        self._llm = None
    
    def _get_llm(self):
        if self._llm is None:
            from openai import OpenAI
            self._llm = OpenAI(
                api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                base_url="https://api.deepseek.com/v1",
            )
        return self._llm
    
    def extract_facts(self, contact: str, messages: list) -> list:
        """Extract facts from a list of messages and store them.
        
        Args:
            contact: Contact name
            messages: List of {"sender": "...", "content": "..."}
        
        Returns list of extracted fact strings.
        """
        if not messages:
            return []
        
        # Build conversation text
        conv_lines = []
        for msg in messages[-10:]:  # last 10 messages
            role = "我" if msg.get("sender") == "me" else msg.get("sender", "对方")
            conv_lines.append(f"{role}: {msg['content']}")
        
        conversation = "\n".join(conv_lines)
        prompt = FACT_EXTRACTION_PROMPT.replace("__CONTACT__", contact).replace(
            "__CONVERSATION__", conversation
        )
        
        try:
            client = self._get_llm()
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.3,
            )
            text = resp.choices[0].message.content.strip()
            
            if not text or text == "无":
                return []
            
            # Parse lines starting with "- "
            facts = []
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("- ") or line.startswith("-"):
                    fact = line.lstrip("- ").strip()
                    if fact and fact != "无":
                        facts.append(fact)
            
            # Store facts
            if facts:
                from src.wechat.relationship import add_fact
                for fact in facts:
                    add_fact(contact, fact)
            
            logger.info("Extracted %d facts about %s", len(facts), contact)
            return facts
            
        except Exception as e:
            logger.error("Fact extraction failed for %s: %s", contact, e)
            return []
    
    def retrieve(self, contact: str, context: str = "") -> str:
        """Retrieve relevant facts about a contact for reply generation.
        
        Args:
            contact: Contact name
            context: Current conversation context for relevance matching
        
        Returns prompt fragment with relevant facts.
        """
        from src.wechat.relationship import get_relationship
        rel = get_relationship(contact)
        
        if not rel.facts:
            return ""
        
        # If context provided, do simple keyword relevance scoring
        if context:
            scored = []
            for f in rel.facts:
                score = 0
                fact_text = f["fact"]
                # Simple keyword overlap
                context_words = set(context)
                fact_words = set(fact_text)
                overlap = context_words & fact_words
                score += len(overlap) * 2
                # Recency bonus
                try:
                    fact_date = datetime.fromisoformat(f["when"])
                    days_ago = (datetime.now() - fact_date).days
                    if days_ago < 7:
                        score += 5
                    elif days_ago < 30:
                        score += 3
                except Exception:
                    pass
                scored.append((score, f))
            
            scored.sort(key=lambda x: x[0], reverse=True)
            facts = [f for _, f in scored[:5] if _ > 0]
        else:
            facts = rel.facts[-5:]
        
        if not facts:
            return ""
        
        lines = ["\n关于对方的已知信息（可以在聊天中自然地提到）："]
        for f in facts:
            lines.append(f"- {f['fact']}（{f['when']}）")
        
        return "\n".join(lines)
