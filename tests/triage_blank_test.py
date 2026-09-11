#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
50+查询变体triage模拟 — 找出所有空白回复

模拟 triage_node 中的规则路由（不调 LLM），
标记哪些查询会产出空白 final_output。
"""

import re
from typing import Optional
from datetime import datetime

# ═══════════════════════════════════════════════════════════
# 从 graph.py 复制的辅助函数（与 triage_node 用的一模一样）
# ═══════════════════════════════════════════════════════════

def _strip_wrapping_quotes(text: str) -> str:
    """Trim whitespace, surrounding quotes, and trailing punctuation."""
    if not text:
        return ""
    text = text.strip().strip("\"' \"\"''《》「」『』")
    return text.strip(" ，,。：:；;")


def _quick_reply(task: str) -> str:
    """Generate a fast direct reply for trivial/short queries without LLM."""
    t = task.strip().lower()
    # Math patterns
    math_m = re.match(
        r"^(?:计算)?\s*(\d+)\s*([\+\-\*\/×÷])\s*(\d+)\s*(?:等于几|等于多少|是多少|=\?|\?)?$", t
    )
    if math_m:
        a, op, b = int(math_m.group(1)), math_m.group(2), int(math_m.group(3))
        op_map = {"+": a + b, "-": a - b, "*": a * b, "×": a * b,
                  "/": a / b if b != 0 else "∞", "÷": a / b if b != 0 else "∞"}
        result = op_map.get(op, "?")
        if isinstance(result, float) and result == int(result):
            result = int(result)
        return f"{a} {op} {b} = {result}"
    # Quadratic
    _quad = re.match(
        r"^(?:.*?)?"
        r"(?:x\^2|x²|x\s*\*\*\s*2)\s*"
        r"([+\-]?\s*\d+(?:\.\d+)?)\s*x\s*"
        r"([+\-]?\s*\d+(?:\.\d+)?)\s*=\s*0"
        r"(?:.*?)$", t)
    if _quad:
        try:
            b_raw = _quad.group(1).replace(" ", "")
            c_raw = _quad.group(2).replace(" ", "")
            b_val = float(b_raw) if b_raw else 0.0
            c_val = float(c_raw) if c_raw else 0.0
            disc = b_val * b_val - 4 * c_val
            if disc < 0:
                real = -b_val / 2
                imag = ((-disc) ** 0.5) / 2
                return f"x₁ = {real:.4f} + {imag:.4f}i\nx₂ = {real:.4f} - {imag:.4f}i"
            elif disc == 0:
                x = -b_val / 2
                return f"x = {x:.4f} (重根)"
            else:
                x1 = (-b_val + disc ** 0.5) / 2
                x2 = (-b_val - disc ** 0.5) / 2
                return f"x₁ = {x1:.4f}\nx₂ = {x2:.4f}"
        except (ValueError, ZeroDivisionError):
            pass
    if any(w in t for w in ["你好", "您好", "嗨", "哈喽", "halo", "hello", "hi"]):
        return "👋 你好！有什么可以帮你的？"
    if re.search(r"你(?:是谁|叫什么|会什么|能做什么|有什么用|在吗|好吗|聪明吗)", t):
        return "我是 AI-Company 的 CEO 助手，负责分析需求、分派任务、审核结果。有什么需要尽管说！"
    if any(w in t for w in ["谢谢", "thanks", "thx", "感谢"]):
        return "不客气！😊"
    if any(w in t for w in ["再见", "bye", "88", "拜拜"]):
        return "再见！有需要随时找我 👋"
    if re.match(r"^[哦嗯啊哈嘿哎咦哟]{1,3}[！!]*$", t):
        return "😄"
    if any(w in t for w in ["放屁", "扯淡", "胡说", "瞎说", "傻", "笨", "蠢", "弱智", "垃圾"]):
        return "别急，我在。你要么直接说哪儿答错了，要么把你想要的结果丢给我，我马上改。"
    if any(w in t for w in ["哈哈", "牛逼", "666", "太棒", "厉害", "真的假的"]):
        return "收到，继续。你要我接着干哪一段？"
    return "我在，直接说事。"


def _extract_wechat_send_request(task: str) -> Optional[tuple]:
    """Extract (contact, message) from natural-language WeChat send requests."""
    task = (task or "").strip()
    if not task or not re.search(r"微信|wechat|发消息|发微信|发信息|发送消息|发送信息|告诉|发给",
                                  task, re.IGNORECASE):
        return None

    combined_patterns = [
        r"微信(?:发送消息|发消息|发微信)?给\s*([^说，。:：\n]+?)\s*(?:发消息|发微信|发送消息)?\s*说\s*(.+)$",
    ]
    for pattern in combined_patterns:
        match = re.search(pattern, task, re.IGNORECASE)
        if match:
            contact = _strip_wrapping_quotes(match.group(1))
            message = _strip_wrapping_quotes(match.group(2))
            if contact and message:
                return contact, message

    quoted_parts = [
        _strip_wrapping_quotes(m.group(1))
        for m in re.finditer(r'["""「『](.+?)[""""」』]', task)
    ]
    if len(quoted_parts) >= 2 and all(quoted_parts[:2]):
        return quoted_parts[0], quoted_parts[1]

    message = ""
    message_match = None
    message_patterns = [
        r"(?:发微信|发消息|发送消息|发送信息|发信息)\s*[：:：]\s*(.+)$",
        r"(?:说|内容是|内容为|告诉)\s*[：:：]\s*(.+)$",
        r"(?:发送消息|发消息|发送信息|发信息)\s+(.+)$",
        r"(?:发微信|发消息)\s+(.+)$",
        r"[：:：]\s*(.+)$",
        r"[，,。]\s*(.+)$",
    ]
    for pattern in message_patterns:
        match = re.search(pattern, task, re.IGNORECASE)
        if match:
            message_match = match
            message = _strip_wrapping_quotes(match.group(1))
            if message:
                break

    search_text = task[:message_match.start()] if message_match else task
    contact = ""
    contact_patterns = [
        r"(?:给|发给|发送给|告诉)\s*(?:微信(?:\s*(?:好友|联系人))?\s*)?([^，。:：\n]+?)(?=发微信|发消息|发送消息|发信息|发送信息|$)",
        r"(?:给我的微信(?:好友|联系人)?|给微信(?:好友|联系人)?|给(?:好友|联系人)?)\s*[：:，,\s]*([^，。:：\n]+)",
        r"(?:微信(?:发送消息)?给)\s*([^，。:：\n]+)",
        r"(?:联系人)\s*[：:，,\s]*([^，。:：\n]+)",
    ]
    for pattern in contact_patterns:
        match = re.search(pattern, search_text, re.IGNORECASE)
        if match:
            contact = _strip_wrapping_quotes(match.group(1))
            if contact:
                break

    if contact and message:
        return contact, message
    return None


def _extract_wechat_conversation_request(task: str) -> Optional[tuple]:
    """Extract (contact, max_turns, initiate) from natural-language conversation requests."""
    task = (task or "").strip()
    if not task or not re.search(
        r"聊天|回复|代聊|帮我.*聊|替.*回复|聊几句|自动回复|闲聊|聊聊天|聊聊|唠唠",
        task, re.IGNORECASE
    ):
        return None

    initiate = bool(re.search(r"闲聊|聊聊天|聊聊|主动|随便聊|唠唠", task))
    max_turns = None
    turns_match = re.search(r"(\d+)\s*(?:轮|句|次|个来回)", task)
    if turns_match:
        max_turns = int(turns_match.group(1))

    contact_patterns = [
        r"(?:帮|替)\s*(?:我\s*)?(?:跟|和|回复)\s*([^\s，。:：\d聊代闲随便唠]+)",
        r"(?:跟|和|用微信和|用微信跟)\s*([^\s，。:：\d聊代闲随便唠]+?)\s*(?:聊天|聊几句|闲聊|聊聊|聊聊天|随便聊聊|唠唠|自动回复|$)",
        r"(?:回复|代聊)\s*([^\s，。:：\d聊代闲随便唠]+)",
    ]
    for pattern in contact_patterns:
        match = re.search(pattern, task, re.IGNORECASE)
        if match:
            contact = _strip_wrapping_quotes(match.group(1))
            if contact:
                return contact.strip(), max_turns, initiate
    return None


# ═══════════════════════════════════════════════════════════
# 模拟 triage_node 规则路由（不调 LLM）
# ═══════════════════════════════════════════════════════════

class TriageSimulator:
    """模拟 graph.py 中 triage_node 的规则路由逻辑。"""

    # ── System query time patterns ──
    SYSTEM_TIME_PATTERNS = [
        (r"^(?:现在|当前)(?:的|是)?时间(?:是|为)?(?:多少|几点|几|什么)?$", "TIME_NOW"),
        (r"^(?:现在|今天)(?:的|是)?日期(?:是|为)?(?:多少|几号|什么)?$", "DATE_TODAY"),
        (r"^(?:现在)?几点(?:了|钟)?$", "TIME_HOUR"),
        (r"^今天(?:是)?星期(?:几|什么)$", "WEEKDAY"),
        (r"^(?:现在|当前)(?:是)?(?:几月|几月份|什么月)$", "MONTH"),
        (r"^后天(?:的)?(?:阴历|农历)(?:日期)?(?:是)?(?:多少|什么)?$", "LUNAR_DAY_AFTER_TOMORROW"),
    ]

    # ── Trivial exact matches ──
    TRIVIAL_EXACT = {
        "help", "?", "h", "hi", "hello", "hey", "你好", "您好",
        "thanks", "thx", "ok", "好的", "test", "测试",
        "fuck", "shit", "damn", "wtf", "lol", "haha", "哈哈",
        "no", "yes", "yeah", "nope", "yep", "嗯", "哦", "啊",
        "bye", "goodbye", "再见", "88", "886",
        "what", "why", "when", "who", "how",
    }

    LEN2_WHITELIST = {"时间", "日期", "年代", "年份", "小时", "分钟"}

    # ── Trivial patterns ──
    TRIVIAL_PATTERNS = [
        r"^(?:计算)?\s*\d+\s*[\+\-\*\/×÷]\s*\d+\s*(?:等于几|等于多少|是多少|=\?|\?)?$",
        r".*?x(?:\^2|²|\*\*2)\s*[+\-]\s*\d+(?:\.\d+)?\s*x\s*[+\-]\s*\d+(?:\.\d+)?\s*=\s*0.*",
        r"^(?:你好|您好|嗨|哈喽|halo)(?:吗|啊|呀|哦|呢|！|!)*$",
        r"^(?:你(?:是谁|叫什么|会什么|能做什么|有什么用|在吗|好吗|怎么样|聪明吗))[？?！!]*$",
        r"^(?:哦|嗯|啊|哈|嘿|哎|咦|哟){1,3}[！!]*$",
    ]

    # ── Short query task keywords (<15 chars must match one to bypass fast-path) ──
    SHORT_TASK_KEYWORDS = [
        "写", "开发", "实现", "修改", "修复", "bug", "代码", "code",
        "函数", "接口", "api", "部署", "deploy", "数据库", "查询",
        "搜索", "查", "搜", "报告", "文档", "分析",
        "对话", "说过", "问过", "之前", "刚才", "历史", "聊天",
        "回答", "输出", "上一",
        "什么", "谁", "怎么", "为什么", "干嘛", "叫啥",
        "多大", "几岁", "哪里", "哪个", "多少", "何时",
        "最近", "罗列", "列出", "回顾", "总结", "说说",
        "时间", "日期", "今天", "现在",
    ]

    # ── Memory lookup keywords ──
    MEMORY_LOOKUP = ["最近", "罗列", "列出", "回顾", "总结", "说说", "聊聊"]
    MEMORY_TARGET = ["对话", "聊天", "记忆", "历史", "之前", "刚才"]

    # ── Chitchat patterns (bypass all, direct reply) ──
    CHITCHAT_PATTERNS = [
        r'^(你好|您好|嗨|哈[喽罗]|嘿|早上好|下午好|晚上好|晚安|早啊|早呀)',
        r'^(hi|hello|hey|hiya|howdy|good morning|good afternoon|good evening)\b',
        r'^(再见|拜拜|bye|see you|回头见|下次聊)',
        r'^(谢谢|多谢|thanks|thank you|thx)\b',
        r'^(嗯|哦|好[的了]?|ok|okay|知道了|明白了|懂了)\s*$',
        r'(你是谁|你叫什么|你的名字|what is your name|who are you)',
        r'(自我介绍|介绍一下?你自己|介绍.*自己|你是[什么谁]|你能做什么|你有什么功能)',
        r'(还在.*(?:执行|做|跑|处理)|进行.*怎么样|好了没|完成了吗|进度|怎么样了)',
        r'(what.*(?:status|progress)|is it done|are you done)',
    ]

    # ── Fast department routing keywords ──
    FAST_DEPARTMENTS = [
        # Code review -> developer
        ("developer", "CodeReview", [
            "代码审计", "代码审查", "审查代码", "代码质量", "code review",
            "代码打分", "代码评分", "审计代码", "review code",
            "审计.*项目.*代码", "审查.*项目.*质量", "项目.*代码.*审查", "审查.*打分",
        ]),
        # Development -> developer
        ("developer", "Dev", [
            r"写.*(?:api|函数|代码|程序|脚本|模块|类|接口)",
            r"实现.*(?:功能|方法|算法|逻辑)",
            r"创建.*(?:api|项目|服务|应用)",
            r"重构", r"修复.*(?:bug|问题)", r"优化.*(?:代码|性能)",
            "implement", "refactor", "build a", "create a",
        ]),
        # Deployment -> devops
        ("devops", "Deploy", ["部署", "deploy", "docker", "kubernetes", "k8s", "ci/cd"]),
        # Testing -> qa
        ("qa", "Test", ["测试", "test", "pytest", "单测", "单元测试"]),
        # Data analysis -> researcher
        ("researcher", "DataAnalysis", [
            "数据分析", "数据统计", "数据报表", "分析数据", "统计分析", "报表", "图表",
            r"统计.*数据", r"分析.*(?:趋势|规律|分布)", "data analysis", "analytics",
        ]),
        # Code explanation -> researcher
        ("researcher", "CodeExplain", [
            "解释代码", "这段代码", "理解代码", "代码含义",
            "代码.*做什么", "代码.*作用", "代码.*逻辑",
            "explain.*code", "what does.*do", "how does.*work",
        ]),
        # Comparison -> researcher
        ("researcher", "Compare", [
            "对比", "比较", r"\bvs\b", "优劣", "优缺点", "哪个更好", "选哪个", "benchmark",
        ]),
        # PDF generation -> developer
        ("developer", "PDFGen", [
            r"生成.*pdf", r"创建.*pdf", r"写.*pdf", r"导出.*pdf",
            r"生成.*文件", r"导出.*(?:文件|报表|excel)", "pdf", ".pdf",
        ]),
        # Document generation -> developer
        ("developer", "Document", [
            "写报告", "生成报告", "写文档", "写总结", "写纪要",
            "周报", "日报", "月报", "会议纪要",
            r"生成.*文档", r"写.*(?:文档|报告)",
        ]),
        # Knowledge Q&A -> researcher
        ("researcher", "Knowledge", [
            "什么是", "怎么理解", "如何理解", r"是什么",
            "介绍一下", "有哪些", "什么区别",
            r"^怎么", r"^如何",
            "what is", "how to", "explain", "define",
        ]),
        # Local system -> devops
        ("devops", "LocalSys", [
            r"检测.*(?:本地|运行|软件|进程|系统)",
            r"本地.*(?:软件|进程|运行|程序|检测|扫描)",
            r"打开.*(?:微信|QQ|钉钉|应用|软件|程序)",
            r"查看.*(?:置顶|联系人|聊天|微信|QQ)",
            r"有没有.*(?:运行|开启|安装|启动)",
            r"(?:运行|启动).*(?:微信|QQ|钉钉|程序)",
            r"发送.*(?:消息|微信|信息|短信)",
            r"给.*(?:微信|好友|联系人).*发",
            r"(?:微信|QQ|钉钉).*(?:发|消息|信息)",
            r"pgrep|ps\s|进程列表|进程信息",
            r"(?:macos|mac|系统).*(?:权限|设置|偏好|配置)",
        ]),
        # Simple lookup -> researcher
        ("researcher", "Lookup", [
            r"查(一下|查询|看|询)?", r"搜(一下|索)?",
            "股价", "股票", "金价", "银价", "油价", "汇率",
            "天气", "新闻", "最新", "今天", "昨日",
            "出生", "生日", "年龄", "多大",
            "价格", "多少钱",
            "search", "news", "price", "stock", "weather",
        ]),
        # Research -> researcher
        ("researcher", "Research", ["调研", "竞品", "research", "compare"]),
        # Marketing -> marketer
        ("marketer", "Market", ["文案", "推广", "营销", "公众号", "广告"]),
    ]

    @classmethod
    def triage(cls, task: str) -> dict:
        """
        模拟 triage_node 的规则路由。

        Returns:
            {
                "branch": str,           # 命中的分支名
                "final_output": str|None, # 最终输出，None=空白
                "is_blank": bool,        # 是否空白回复
                "phase": str,            # 下一阶段
                "department": str|None,  # 部门
                "detail": str,           # 额外说明
            }
        """
        task = (task or "").strip()
        task_lower = task.lower()

        # ── Branch 1: System time queries ──
        for pat, label in cls.SYSTEM_TIME_PATTERNS:
            if re.match(pat, task):
                now = datetime.now()
                replies = {
                    "TIME_NOW": f"现在是 {now.strftime('%Y年%m月%d日 %H:%M:%S')}",
                    "DATE_TODAY": f"今天是 {now.strftime('%Y年%m月%d日（%A）')}",
                    "TIME_HOUR": now.strftime('%H:%M:%S'),
                    "WEEKDAY": f"今天是{now.strftime('%A')}",
                    "MONTH": f"现在是{now.strftime('%m月')}",
                }
                return {
                    "branch": f"1-SystemQuery({label})",
                    "final_output": replies[label],
                    "is_blank": False,
                    "phase": "deliver",
                    "department": "ceo",
                    "detail": f"系统时间查询 → 即时回复",
                }

        # ── Branch 2: Trivial exact + len<=2 ──
        if task_lower in cls.TRIVIAL_EXACT:
            return {
                "branch": "2-TrivialExact",
                "final_output": "👋 你好！输入 /help 查看可用命令，或直接问我问题。",
                "is_blank": False,
                "phase": "deliver",
                "department": "ceo",
                "detail": "精确匹配 trivial 词汇",
            }

        if len(task) <= 2 and task not in cls.LEN2_WHITELIST:
            return {
                "branch": "2-Len<=2",
                "final_output": "👋 你好！输入 /help 查看可用命令，或直接问我问题。",
                "is_blank": False,
                "phase": "deliver",
                "department": "ceo",
                "detail": f"长度≤2 且不在白名单('{task}')",
            }

        # ── Branch 3: _trivial_patterns ──
        for pat in cls.TRIVIAL_PATTERNS:
            if re.match(pat, task.strip()):
                return {
                    "branch": f"3-TrivialPattern({pat[:30]}...)",
                    "final_output": _quick_reply(task),
                    "is_blank": False,
                    "phase": "deliver",
                    "department": "ceo",
                    "detail": f"匹配 trivial 正则",
                }

        # ── Branch 4: Short non-task (<15 chars, no keywords) ──
        if len(task) < 15:
            if not any(kw in task for kw in cls.SHORT_TASK_KEYWORDS):
                return {
                    "branch": "4-ShortNonTask",
                    "final_output": _quick_reply(task),
                    "is_blank": False,
                    "phase": "deliver",
                    "department": "ceo",
                    "detail": f"短查询(<15字)且无任务关键词",
                }

        # ── Branch 5: Memory lookup ──
        if any(kw in task for kw in cls.MEMORY_LOOKUP) and \
           any(kw in task for kw in cls.MEMORY_TARGET):
            return {
                "branch": "5-MemoryLookup",
                "final_output": None,
                "is_blank": True,
                "phase": "deliver",
                "department": "ceo",
                "detail": "memory_mode=True，无 final_output → 空白",
            }

        # ── Branch 6: WeChat conversation (跳过实际执行，仅检测匹配) ──
        conv = _extract_wechat_conversation_request(task)
        if conv:
            contact, max_turns, initiate = conv
            return {
                "branch": "6-WeChatConversation",
                "final_output": f"（模拟）与{contact}对话完成",
                "is_blank": False,
                "phase": "deliver",
                "department": "devops",
                "detail": f"微信对话: {contact}, initiate={initiate}, turns={max_turns or 3}",
            }

        # ── Branch 7: WeChat send ──
        wc = _extract_wechat_send_request(task)
        if wc:
            contact, message = wc
            return {
                "branch": "7-WeChatSend",
                "final_output": f"（模拟）微信消息已发送给 {contact}：{message}",
                "is_blank": False,
                "phase": "deliver",
                "department": "devops",
                "detail": f"微信发送: {contact} ← {message[:30]}",
            }

        # ── Branch 8: Chitchat patterns ──
        for pat in cls.CHITCHAT_PATTERNS:
            if re.search(pat, task_lower, re.IGNORECASE):
                return {
                    "branch": f"8-Chitchat({pat[:25]}...)",
                    "final_output": "（模拟）CEO chitchat 直接回复",
                    "is_blank": False,
                    "phase": "deliver",
                    "department": "ceo",
                    "detail": "闲聊/问候/自介 → CEO 直接回复",
                }

        # ── Branch 9: Fast department routing ──
        for dept, label, keywords in cls.FAST_DEPARTMENTS:
            for kw in keywords:
                try:
                    if re.search(kw, task_lower, re.IGNORECASE):
                        next_phase = "pm" if dept in ("developer", "qa") else "execute"
                        return {
                            "branch": f"9-FastDept({label}:{kw})",
                            "final_output": None,
                            "is_blank": True,
                            "phase": next_phase,
                            "department": dept,
                            "detail": f"关键词路由 → {dept}, phase={next_phase}，无 final_output → 空白",
                        }
                except re.error:
                    pass

        # ── Branch 10: LLM fallback ──
        return {
            "branch": "10-LLMFallback",
            "final_output": None,
            "is_blank": True,
            "phase": "pm|execute(LLM决定)",
            "department": "developer(默认)",
            "detail": "未匹配任何规则 → 走 LLM 路由，无 final_output → 空白",
        }


# ═══════════════════════════════════════════════════════════
# 50+ 查询变体
# ═══════════════════════════════════════════════════════════

TEST_QUERIES = [
    # ── Branch 1: System time queries (5个) ──
    ("现在时间", "1", "系统查询-当前时间"),
    ("现在时间是多少", "1", "系统查询-时间多少"),
    ("当前日期", "1", "系统查询-当前日期"),
    ("今天星期几", "1", "系统查询-星期几"),
    ("现在是几月", "1", "系统查询-几月"),
    ("现在几点", "1", "系统查询-几点"),
    ("后天的阴历日期是多少", "1", "系统查询-后天阴历"),

    # ── Branch 2: Trivial exact + len<=2 (8个) ──
    ("help", "2", "trivial exact-help"),
    ("你好", "2", "trivial exact-你好"),
    ("hi", "2", "trivial exact-hi"),
    ("ok", "2", "trivial exact-ok"),
    ("再见", "2", "trivial exact-再见"),
    ("?", "2", "trivial exact-?"),
    ("嗯", "2", "trivial exact-嗯"),
    ("ab", "2", "len<=2 非白名单"),

    # ── Branch 3: _trivial_patterns (6个) ──
    ("1+2等于几", "3", "trivial pattern-数学"),
    ("计算3*5", "3", "trivial pattern-计算"),
    ("你好吗", "3", "trivial pattern-你好吗"),
    ("你是谁？", "3", "trivial pattern-你是谁"),
    ("你会什么", "3", "trivial pattern-你会什么"),
    ("嗯嗯", "3", "trivial pattern-语气词"),
    ("x^2+4x+4=0", "3", "trivial pattern-二次方程"),

    # ── Branch 4: Short non-task <15 chars no keyword (5个) ──
    ("哈哈好笑", "4", "短闲聊"),
    ("牛逼", "4", "短感叹"),
    ("666", "4", "短数字感叹"),
    ("真的假的", "4", "短疑问-无关键词"),
    ("太棒了", "4", "短赞美"),
    ("放屁", "4", "短粗口"),
    ("狗蛋变傻了", "4", "短负反馈"),

    # ── Branch 5: Memory lookup (4个) ──
    ("最近聊天记录", "5", "memory lookup-最近聊天"),
    ("回顾之前的对话", "5", "memory lookup-回顾对话"),
    ("列出最近的记忆", "5", "memory lookup-列出记忆"),
    ("总结刚才聊了什么", "5", "memory lookup-总结刚才"),

    # ── Branch 6: WeChat conversation (4个) ──
    ("帮我跟张三聊天", "6", "微信对话-帮聊"),
    ("跟李四聊聊天", "6", "微信对话-主动闲聊"),
    ("替我跟王五聊几句", "6", "微信对话-聊几句"),
    ("用微信和刘六闲聊", "6", "微信对话-用微信闲聊"),

    # ── Branch 7: WeChat send (4个) ──
    ("微信给小明发消息说今天开会", "7", "微信发送-给X发"),
    ("发微信给小红：明天见", "7", "微信发送-发微信给"),
    ("微信发送消息给老王说项目完成了", "7", "微信发送-发送消息给"),
    ("给赵六发微信，晚上一起吃饭", "7", "微信发送-给X发微信"),

    # ── Branch 8: Chitchat patterns (5个) ──
    ("早上好", "8", "chitchat-早上好"),
    ("你有什么功能", "8", "chitchat-功能询问"),
    ("进度怎么样了", "8", "chitchat-进度查询"),
    ("good morning", "8", "chitchat-英文早安"),
    ("谢谢你的帮助", "8", "chitchat-感谢"),

    # ── Branch 9: Fast department routing — developer (4个) ──
    ("写一个API接口", "9", "fast-dept-写API"),
    ("重构用户模块", "9", "fast-dept-重构"),
    ("生成PDF报告", "9", "fast-dept-PDF"),
    ("写一份周报", "9", "fast-dept-周报"),

    # ── Branch 9: Fast department routing — researcher (4个) ──
    ("解释这段代码的作用", "9", "fast-dept-解释代码"),
    ("对比React和Vue", "9", "fast-dept-对比"),
    ("什么是量子计算", "9", "fast-dept-什么是"),
    ("查一下今天天气", "9", "fast-dept-查天气"),

    # ── Branch 9: Fast department routing — devops (2个) ──
    ("部署到生产环境", "9", "fast-dept-部署"),
    ("检测本地运行的进程", "9", "fast-dept-本地检测"),

    # ── Branch 9: Fast department routing — qa (2个) ──
    ("写单元测试", "9", "fast-dept-单元测试"),
    ("pytest 测试用户登录", "9", "fast-dept-pytest"),

    # ── Branch 9: Fast department routing — marketer (1个) ──
    ("写一篇公众号推广文案", "9", "fast-dept-文案"),

    # ── Branch 9: Fast department routing — code review (1个) ──
    ("代码审查这个项目", "9", "fast-dept-代码审查"),

    # ── Branch 10: LLM fallback (5个) ──
    ("你觉得人工智能的发展方向是什么", "10", "LLM-开放讨论"),
    ("帮我规划下周的工作安排", "10", "LLM-规划"),
    ("这个设计合理吗有什么建议", "10", "LLM-设计建议"),
    ("系统报错了帮忙看看日志", "10", "LLM-模糊查错"),
    ("帮我想个团队建设的活动", "10", "LLM-创意"),

    # ── 额外边界情况 ──
    ("现在", "2", "len<=2 白名单外"),  # len=2, not in whitelist → Branch 2
    ("今天", "9", "fast-dept-Lookup(今天)"),  # 被 Lookup 关键词匹配
    ("hello there", "8", "chitchat-hello there"),  # chitchat 先于 fast-dept

    # ── 空输入 ──
    ("", "2", "空字符串 len=0 ≤2 → TrivialExact 外 → len≤2"),
    ("  ", "2", "仅空格 strip后为空"),
]


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

def main():
    print("=" * 90)
    print("  AI-Company Triage 空白回复检测 — 50+ 查询变体模拟")
    print("=" * 90)
    print()

    results = []
    blank_cases = []
    branch_summary = {}

    for task, expected_branch, desc in TEST_QUERIES:
        result = TriageSimulator.triage(task)
        actual_branch = result["branch"].split("(")[0].split("-")[0]
        branch_name = result["branch"]
        is_blank = result["is_blank"]
        final_output = result["final_output"]

        # Track branch stats
        branch_id = branch_name.split("(")[0] if "(" in branch_name else branch_name
        if branch_id not in branch_summary:
            branch_summary[branch_id] = {"total": 0, "blank": 0, "non_blank": 0}
        branch_summary[branch_id]["total"] += 1
        if is_blank:
            branch_summary[branch_id]["blank"] += 1
        else:
            branch_summary[branch_id]["non_blank"] += 1

        blank_mark = "⚠️ 空白" if is_blank else "✅"
        output_preview = (
            final_output[:60].replace("\n", "\\n") + ("..." if len(final_output or "") > 60 else "")
            if final_output else "（无输出）"
        )

        results.append({
            "task": task,
            "desc": desc,
            "branch": branch_name,
            "is_blank": is_blank,
            "output_preview": output_preview,
            "detail": result["detail"],
        })

        if is_blank:
            blank_cases.append(results[-1])

        print(f"{blank_mark} [{actual_branch}] {desc}")
        print(f"   查询: 「{task}」")
        print(f"   分支: {branch_name}")
        print(f"   输出: {output_preview}")
        print(f"   说明: {result['detail']}")
        print()

    # ── 汇总 ──
    print("=" * 90)
    print("  📊 汇总统计")
    print("=" * 90)
    total = len(results)
    blank_count = sum(1 for r in results if r["is_blank"])
    non_blank_count = total - blank_count
    print(f"  总查询数: {total}")
    print(f"  非空白:   {non_blank_count}  ({non_blank_count/total*100:.1f}%)")
    print(f"  空白:     {blank_count}  ({blank_count/total*100:.1f}%)")
    print()

    print("─" * 90)
    print("  按分支统计:")
    print("─" * 90)
    for bid in sorted(branch_summary.keys()):
        s = branch_summary[bid]
        print(f"  {bid:30s}  总数={s['total']:2d}  空白={s['blank']:2d}  非空白={s['non_blank']:2d}")
    print()

    # ── 所有空白回复列表 ──
    print("=" * 90)
    print("  ⚠️  所有空白回复 (final_output = None)")
    print("=" * 90)
    if not blank_cases:
        print("  无空白回复！所有查询都有 final_output。")
    else:
        for i, bc in enumerate(blank_cases, 1):
            print(f"  {i:2d}. [{bc['branch']}] 「{bc['task']}」→ {bc['detail']}")
    print()

    # ── 分类说明 ──
    print("=" * 90)
    print("  📋 空白回复分类")
    print("=" * 90)
    print("""
  分支 5 (MemoryLookup):
    查询要求回顾/列出/总结 + 对话/聊天/记忆
    → 返回 memory_mode=True，但 triage_node 本身不设 final_output
    → 后续由 SIMPLE_QUERY 流程异步填充，triage 阶段空白

  分支 9 (FastDept):
    关键词命中开发/研究/运维/测试/营销部门
    → triage_node 只做路由，不产生输出
    → final_output 由部门 Agent 执行后填充，triage 阶段空白

  分支 10 (LLMFallback):
    未匹配任何规则，进入 LLM 路由决策
    → triage_node 不设 final_output，由 LLM + 部门 Agent 填充
    → triage 阶段空白
""")

    # ── 空白风险矩阵 ──
    print("=" * 90)
    print("  🎯 空白回复风险矩阵")
    print("=" * 90)
    print("""
  ┌─────────────────────┬──────────┬──────────────────────────────────────┐
  │ 分支                │ 风险等级 │ 说明                                 │
  ├─────────────────────┼──────────┼──────────────────────────────────────┤
  │ 5-MemoryLookup      │ 🟡 中    │ memory_mode 需要下游正确处理，否则   │
  │                     │          │ 用户无反馈。加一个 "正在回顾..."      │
  │                     │          │ 的过渡回复可降低风险。               │
  ├─────────────────────┼──────────┼──────────────────────────────────────┤
  │ 9-FastDept          │ 🟢 低    │ 正常路由行为，部门 Agent 会产出结果。 │
  │                     │          │ 若部门失败则需 PMO 兜底。            │
  ├─────────────────────┼──────────┼──────────────────────────────────────┤
  │ 10-LLMFallback      │ 🟢 低    │ 与 FastDept 同理，LLM + Agent 产出。 │
  │                     │          │ LLM 调用失败时可能空白。             │
  └─────────────────────┴──────────┴──────────────────────────────────────┘
""")


if __name__ == "__main__":
    main()
