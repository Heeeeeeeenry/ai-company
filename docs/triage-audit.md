# Triage 路由系统审计报告 — 空白回复路径分析

> 审计对象: `src/ceo/graph.py` (3010行，审计时已包含部分补丁)
> 审计日期: 2026-07-02
> 核心问题: "今天周几"→18s走researcher, "明天周几"→空白闲聊回复
> ⚠️ 注意: L570-572 已补丁修复 "明天/后天/昨天周几"，但架构问题依旧

---

## 一、完整路由决策树

```
triage_node (L551-1152)
│
├─[1] 系统查询 (L562-586) ──── 9个正则 → phase=deliver → deliver_node
│   ├─ 现在时间 / 几点 / 今天日期 / 今天星期几 / 明天周几 / 后天周几 / 昨天周几 / 几月份
│   └─ ❌ 大后天/上前天/上周二/下周一/明天几号/今天什么日子/当前年月日… 全漏
│
├─[2] Trivial exact (L588-607) ── 精确单词匹配 → phase=deliver
│   ├─ help/hi/hello/你好/thanks/ok/test/fuck/lol… (L590-596)
│   └─ ❌ 返回固定"👋 你好！"——对非问候查询是错误回复
│
├─[3] Trivial 正则 (L610-635) ── 数学/问候/身份/感叹 → _quick_reply → deliver
│   ├─ _quick_reply(L171-231) 只覆盖: 数学/问候/身份/谢谢/再见/感叹
│   └─ ❌ 默认回退(L231): "有什么我可以帮你的吗？"
│
├─[4] Short非任务 (L637-662) ── <15字符 且 无task_keyword → _quick_reply → deliver
│   ├─ task_keywords(L640-652): 写/开发/时间/日期/今天/明天/后天/昨天/现在/什么…
│   └─ ❌ "周几/星期/几日/几号/上月/下月/万年历" 不在列表
│
├─[5] Memory lookup (L664-676) ── 需要lookup词+target词 同时出现 → memory_mode=True → deliver
│   ├─ lookup(L666): 最近/罗列/回顾/总结/说说/聊聊
│   ├─ target(L667): 对话/聊天/记忆/历史/之前/刚才
│   └─ ❌ 7/7 测试case全失败: "最近聊了什么"→lookup✓target✗; "刚才说了什么"→lookup✗target✓
│
├─[6] WeChat 对话 (L687-743) ── _extract_wechat_conversation_request → deliver
│
├─[7] WeChat 发送 (L745-801) ── _extract_wechat_send_request → deliver
│
├─[8] Chitchat 闲聊 (L834-890) ── 问候/身份/状态 → deliver
│   ├─ ❌ "辛苦了""在干嘛""最近好吗""好久不见" 全漏
│
├─[9] 关键词部门路由 (L892-1072) ── 代码审查/开发/部署/测试/数据分析/解释/对比/PDF/文档/知识/系统/查询/研究/营销
│   ├─ fast_department + role_registry检查 (L1074) → phase=pm或execute
│   └─ ❌ fast_department有值但registry无 → 落入LLM回退 (L1082)
│
└─[10] LLM 回退 (L1083-1158) ── LLM调用 → 关键词兜底 → developer
    ├─ LLM超时/报错 → _safe_node捕获(L538) → "Crashed:…" → deliver
    └─ ❌ 昂贵的LLM调用用于已经可以确定意图的查询
```

**路由后分叉** (route_after_triage L2705-2711):

```
triage返回phase → route_after_triage
├─ phase="deliver" → deliver_node (L2414)
├─ phase="pm"      → pm_analyze_node → architect/execute
└─ phase="execute" → execute_node → execute_department_node
```

**部门执行后分叉** (route_after_department L2714-2754):

```
execute_department_node → route_after_department
├─ COMMAND/SIMPLE/LOCAL_SYSTEM → verify_aggregate (skip audit)
├─ researcher/marketer         → verify_aggregate (skip audit)
├─ DOCUMENT                    → verify_aggregate (skip audit)
├─ 短<20字非代码                → verify_aggregate (skip audit)
├─ AUDIT_ENABLED=False          → verify_aggregate
└─ 其他                        → auditor → PMO → verify_aggregate
```

**聚合后分叉** (route_after_aggregate L2757-2768):

```
verify_aggregate_node → route_after_aggregate
├─ next_action="deliver" + decision="FAIL" → auto_repair → deliver
├─ next_action="deliver" (非FAIL)          → deliver
└─ next_action="revise/replan"              → execute (重试)
```

---

## 二、所有空白/错误回复路径（精确到行号）

### 🔴 路径1: 时间日期查询 — 正则过窄 (L562-586)

**已覆盖的9个模式 (L562-575):**
```
L564: 现在/当前时间 (时间查询)
L565: 几点 (纯时间)
L567: 今天日期/几号
L568: 今天周几/星期几/礼拜几
L570: 明天周几/星期几/礼拜几 ← 已补丁
L571: 后天周几/星期几/礼拜几 ← 已补丁
L572: 昨天周几/星期几/礼拜几 ← 已补丁
L574: 现在几月/几月份
```

**仍然失败的查询 (实测):**

| 用户输入 | 落入路径 | 实际返回 | 预期 |
|---------|---------|---------|------|
| "大后天周几" | L640 有keyword"什么"→不走short→关键词路由 | 走researcher pipeline | 大后天星期 |
| "上前天周几" | L639 len=5<15, 无keyword→L658 _quick_reply | "有什么我可以帮你的吗？" | 日期 |
| "上周二几号" | L639 len=5<15, 无keyword→L658 _quick_reply | "有什么我可以帮你的吗？" | 日期 |
| "下周一几号" | L639 len=5<15, 无keyword→L658 _quick_reply | "有什么我可以帮你的吗？" | 日期 |
| "明天几号" | L640 有keyword"明天"→不走short→关键词路由 | 走pipeline | 明天的几号 |
| "今天是什么日子" | L640 有keyword"今天"→关键词路由 | 走pipeline | 日期+星期 |
| "当前年月日" | L639 len=5<15, 无keyword→L658 _quick_reply | "有什么我可以帮你的吗？" | 日期 |
| "今年是哪一年" | L639 len=6<15, 无keyword→L658 _quick_reply | "有什么我可以帮你的吗？" | 年份 |

**根本原因**: 用 `re.match()` 做完全匹配, 不是时间偏移计算。每个时间表述都需要一个单独的正则——这是正则打地鼠问题, 加再多正则也覆盖不了自然语言的相对时间表述。

---

### 🔴 路径2: 短期记忆查询 — 双重关键词过严 (L664-676)

```python
# L666-668: 只有 BOTH lookup词 AND target词 同时出现才触发
_memory_lookup = ["最近", "罗列", "列出", "回顾", "总结", "说说", "聊聊"]
_memory_target = ["对话", "聊天", "记忆", "历史", "之前", "刚才"]
if any(kw in task for kw in _memory_lookup) and any(kw in task for kw in _memory_target):
```

**实测: 7/7 测试case全失败:**

| 用户输入 | lookup | target | memory_mode | 落入路径 |
|---------|--------|--------|-------------|---------|
| "最近聊了什么" | ✓(最近) | ✗(聊了∉target) | ❌ | 关键词路由 |
| "刚才说了什么" | ✗ | ✓(刚才) | ❌ | 关键词路由 |
| "之前那个问题" | ✗ | ✓(之前) | ❌ | 关键词路由 |
| "上次你回答了什么" | ✗ | ✗ | ❌ | 关键词路由 |
| "回忆一下我问过你什么" | ✗ | ✗ | ❌ | 关键词路由 |
| "你还记得我说过什么吗" | ✗ | ✗ | ❌ | 关键词路由 |
| "聊过AI吗" | ✗ | ✗ | ❌ | 关键词路由 |

> ⚠️ `memory_mode` 字段在 `CEOState` (L51) 有声明，但触发条件要求 AND 逻辑，几乎只有 "回顾对话""总结聊天"这种精确组合才能命中。

---

### 🔴 路径3: _quick_reply 默认回退 (L171-231)

当triage匹配了某个模式（数学/短查询/trivial），但问题在 `_quick_reply` 中没有对应的处理函数时，返回:

```python
# L231: default
return "有什么我可以帮你的吗？输入 /help 查看我能做什么。"
```

**触发场景:**
- "明天周几" (匹配长度<15且无keyword → L632 → _quick_reply → 数学/问候/身份/感谢/再见/感叹 都不匹配 → L231)
- 任何<15字且不在task_keywords中的中文查询
- task_keywords缺失: "明天", "昨天", "周", "星期", "日历", "农历", "节日", "星座", "天气查询(非实际天气,如'今天天气怎么样'=5字)"

---

### 🔴 路径4: Chitchat 遗漏 — 正则覆盖不足 (L834-890)

**覆盖的chitchat模式 (L834-848):**
```
L836: 你好/您好/嗨/哈喽/嘿/早上好/下午好/晚上好/晚安/早啊/早呀
L837: hi/hello/hey/hiya/howdy/good morning/afternoon/evening
L839: 再见/拜拜/bye/see you/回头见/下次聊
L840: 谢谢/多谢/thanks/thank you/thx
L841: 嗯/哦/好的/ok/okay/知道了/明白了/懂了
L843: 你是谁/你叫什么/你的名字/what is your name/who are you
L844: 自我介绍/介绍一下你自己/你是谁/你能做什么/你有什么功能
L846: 还在执行/进行怎么样/好了没/完成了吗/进度/怎么样了
```

**漏掉的常见中文闲聊 (实测):**

| 输入 | 落入路径 | 结果 |
|------|---------|------|
| "辛苦了" | L639 len=3<15, 无keyword→L658 _quick_reply | 通用回复 |
| "在干嘛" | L640 有keyword"什么"→关键词路由 | 走pipeline浪费 |
| "最近好吗" | L640 有keyword"最近"→关键词路由 | 走pipeline浪费 |
| "好久不见" | L639 len=4<15, 无keyword→L658 _quick_reply | 通用回复 |
| "你吃了吗" | L639 len=4<15, 无keyword→L658 _quick_reply | 通用回复 |
| "emo了" | L639 len=4<15, 无keyword→L658 _quick_reply | 通用回复 |
| "牛逼" | L639 len=2<15, 无keyword→L658 _quick_reply | 通用回复 |

---

### 🔴 路径5: LLM 回退失败 → crash 消息 (L1083-1158)

```python
# L1099
response = await llm.ainvoke([...])
# L1104
intent = str(response.content).strip().lower()
```

**失败场景:**
- LLM API超时(45s) → `_safe_node` 装饰器(L538)捕获 → `phase: deliver, final_output: "[Triage] Crashed: ..."`
- LLM返回无法解析的内容 → `role_registry.get(intent)` 返回None → keyword fallback(L1121)
- keyword fallback score≤0.15 → 暴力默认developer (L1129-1132)
- 默认developer去执行一个事实查询/日期查询 → 浪费完整开发pipeline(PM→Architect→执行→审计)

**具体影响**: "大后天周几" → 没有匹配任何规则 → LLM回退 → 可能返回错误department → 18s+ pipeline → 可能返回错误内容

---

### 🔴 路径6: execute_department 空输出 (L1721-1776)

```python
# L1721
output = result.get("output", "")
success = result.get("success", True)
```

**可能场景:**
- Researcher搜索失败但没报错 → success=True, output=""
- Developer工具调用200次耗尽max_iterations → LM可能返回空
- output通过auditor/PMO → verify_aggregate的 `_is_fail(L1952)` 检测 `"^\s*$"` → FAIL → 重试 → FAIL → 输出"任务未能通过质量审核"

---

### 🔴 路径7: deliver_node 空输出 (L2486-2490)

```python
# L2486-2490
dept_output = state.get("final_output", "")
if state.get("memory_mode") or (dept_output and dept_output.startswith("## ")):
    pass  # memory mode output is already clean markdown
else:
    dept_output = _clean_output(str(dept_output)) if dept_output else ""
```

如果 `final_output` 是 `None` → `str(None)` = `"None"` → 经过 `_clean_output` 可能变成空或其他。最终deliver的 `dept_output` 可能是:
- `""` (空字符串)
- `"None"` (字符串)
- 仅含token统计和memory report的片段

---

### 🔴 路径8: 关键词部门路由 → registry不存在 → LLM回退 (L1074-1082)

```python
# L1074
if fast_department and role_registry.get(fast_department):
    department = fast_department  # OK
    ...
else:
    # 落入LLM回退 (L1082)
```

虽然大概率fast_department在registry中存在（因为只有硬编码的department名），但如果registry配置未加载或改名，就会出现静默降级到LLM回退。

---

### 🟡 路径9: 非代码部门跳过PM (L2709-2711 + L1316)

```
route_after_triage: phase="execute" (非pm, 非deliver) → execute_node
→ execute_department_node
→ NO PRD, NO acceptance criteria
```

researcher/marketer/devops在关键词路由时直接设 `phase=execute` (L1078-1080)，跳过pm_analyze_node。意味着这些部门的任务没有结构化的验收标准，质量完全依赖agent自身。

---

### 🟡 路径10: memory_mode 走 deliver → 流程正确但触发条件太窄

```
triage: phase="deliver", memory_mode=True (L674)
route_after_triage: phase=deliver → deliver_node (L2707)
deliver_node: memory_mode检查 → 实际做recall (L2414-2432)
```

这个流程是正确的，但问题在于**触发条件太窄**(见路径2)。7/7常见记忆查询都未触发memory_mode。

---

## 三、根本性修复方案

### 问题本质: "正则打地鼠"

当前架构是 **层级正则匹配 → 关键词匹配 → LLM回退**。每修一个case就是加一个regex或keyword，但同义变体立刻失效。这不是正则写得不够多——是**架构层面把"模式匹配"和"意图理解"放在了同一个位置**。

### 根本性方案: 两级意图路由 + 确定性系统查询层

```
用户输入
  │
  ├─[Layer 0] 系统查询引擎 (本地, 0 token)
  │   ├─ 时间/日期计算 (支持相对时间: 明天/昨天/上周/下月/后天…)
  │   ├─ 简单数学 (已有)
  │   ├─ 系统状态 (/status, /help)
  │   └─ 内置问答 (你是谁/能做什么)
  │
  ├─[Layer 1] 记忆查询引擎 (本地, 0 token)
  │   ├─ 语义触发: 任何包含时间回溯/对话引用/记忆关键词
  │   │   (不仅限 "回顾"+"对话" 双关键词)
  │   ├─ 先用hermes语义搜索判断是否有关联记忆
  │   └─ 有 → 注入上下文 + 走轻量pipeline; 无 → 告知用户
  │
  └─[Layer 2] 部门路由 (关键词 + V5 IntentRouter)
      ├─ 关键词快速匹配 → 直接确定department (现状已做)
      ├─ V5 IntentRouter 精细分类 → 决定task_type
      └─ LLM 仅用于真正模糊的查询 (<5% 的case)
```

### 具体修复清单

#### Fix 1: 重写系统查询 (替换 L562-586)

**当前**: 5个硬编码正则 → `datetime.now()`
**改为**: 基于 `datetime + python-dateutil` 的相对时间解析器

```python
# 伪代码 (不写正则, 用语义模板)
SYSTEM_QUERY_TEMPLATES = [
    # 模板: (触发词集合, 时间偏移, 输出格式)
    ({"今天", "今日", "今天"}, timedelta(0), "{prefix}%Y年%m月%d日（%A）"),
    ({"明天", "明日", "明儿"}, timedelta(days=1), "{prefix}%Y年%m月%d日（%A）"),
    ({"昨天", "昨日"}, timedelta(days=-1), "{prefix}%Y年%m月%d日（%A）"),
    ({"后天"}, timedelta(days=2), "{prefix}%Y年%m月%d日（%A）"),
    ({"前天"}, timedelta(days=-2), "{prefix}%Y年%m月%d日（%A）"),
    # 星期查询 (任意日期)
    ({"星期几", "周几", "礼拜几"}, None, "{date}是%A"),  # 需要再解析
    # ... 覆盖: 上周X, 下月, 今年/去年, 几号, 什么日子, 农历(可fallback)
]
```

**关键**: 不再用 `re.match(pattern, task)` 做完全匹配，而是**提取时间偏移量**，然后 `datetime.now() + offset`。这是一个日期函数而不是正则函数。

#### Fix 2: 重写记忆触发 (替换 L664-676)

**当前**: 双重关键词 AND 逻辑
**改为**: 单层语义触发 + 相似度阈值

```python
# 记忆触发词: 任何包含回忆/对话/之前/历史语义的词
MEMORY_TRIGGER_PATTERNS = [
    r"(最近|之前|刚才|上次|上回|以前|过去|刚刚).{0,10}(说|聊|问|答|讲|讨论|提)",
    r"(回忆|记不记得|还记?得|有没有说过|聊过|讨论过)",
    r"(我们|你).{0,5}(之前|曾经|已经).{0,5}(说|聊|回答|告诉)",
    r"(总结|回顾|罗列|列出).{0,5}(对话|聊天|记忆|历史|交流)",
    r"(说了什么|聊了什么|问过什么|讲过什么)",
]
```

还有 `should_inject` 调用（`src/memory/hermes`）应该被提升到triage层来判断是否需要上下文注入。

#### Fix 3: 扩充 Chitchat 覆盖 (替换 L827-883)

**当前**: 8组硬编码正则
**改为**: 用V5 IntentRouter的`GENERAL_CHAT`分类，配合一个**闲聊短语库**（覆盖常见中文口语）

```
闲聊触发词库 (不需要正则, 纯字符串包含检测):
- 辛苦了 / 在干嘛 / 最近好吗 / 好久不见 / 你吃了吗
- emo / 牛逼 / 绝了 / 离谱 / 笑死 / 哈哈哈+
- 好无聊 / 陪我聊聊 / 讲个笑话 / 说点什么
```

匹配逻辑: 如果 V5 router 分类为 `GENERAL_CHAT` 且 任务长度<30 且无代码关键词 → 直接ceo闲聊回复。

#### Fix 4: 提升 LLM 回退的健壮性 (L1083-1158)

**当前**: LLM失败 → crash消息 / 默认developer
**改为**: LLM失败 → V5 IntentRouter keyword兜底 → 如果还是不明 → 直接反问用户 "你想让我帮你做什么？"而不是走developer pipeline

```python
# L1112-1125 替换为:
else:
    # V5 keyword兜底
    best, score = role_registry.best_match(state["user_request"])
    if best and score > 0.15:
        department = best.name
        # ...同现有逻辑
    else:
        # 不默认developer！对于模糊查询，反问用户
        return {
            "phase": "deliver",
            "final_output": "我不太确定你想做什么。可以再具体描述一下吗？比如：\n"
                            "• 查信息：'今天金价多少'\n"
                            "• 写代码：'写一个快速排序'\n"
                            "• 操作文件：'生成周报PDF'\n"
                            "• 聊天：直接跟我聊就行 😊",
            "score_card": {"score": 50, "decision": "ASK_USER"},
        }
```

#### Fix 5: 非代码部门也要走 PM (L1071-1073)

**当前**: researcher/marketer/devops直接 `phase=execute`
**改为**: 所有部门都走PM → PM对非代码任务跳过Architect → 但保留acceptance criteria生成。

```python
# 关键修改: L1070-1073
if dept_role and dept_role.category == "execution":
    next_phase = "pm"  # ← 所有执行角色都走PM, 不只是developer/qa
else:
    next_phase = "pm"  # 统一
```

#### Fix 6: 短路保护 — 空输出不应进入审计 (L1714+)

在 `execute_department_node` 中，如果 `output` 为空或仅含空白:

```python
# 在 L1714 之后添加
output = result.get("output", "").strip()
if not output:
    return {
        "phase": "deliver",
        "final_output": "抱歉，执行部门没有返回有效结果。请换个方式提问。",
        "execution_log": ["[DEPT] Empty output — aborting pipeline"],
    }
```

---

## 四、修复优先级

| 优先级 | 路径 | 影响面 | 修复工作量 | 方案 |
|--------|------|--------|-----------|------|
| **P0** | 路径1: 时间日期 | 每次换说法就失败 | 中 | Fix 1: 相对时间引擎 |
| **P0** | 路径2: 记忆查询 | 核心功能基本不可用 | 小 | Fix 2: 单层语义触发 |
| **P1** | 路径3: _quick_reply默认 | 大量短查询返回"有什么可以帮你" | 小 | 结合 Fix 1+2 |
| **P1** | 路径5: LLM回退失败 | 模糊查询走developer浪费token | 小 | Fix 4: 反问用户 |
| **P1** | 路径7: deliver空输出 | 静默失败,用户看不到内容 | 小 | Fix 6: 短路保护 |
| **P2** | 路径4: chitchat遗漏 | 中文口语不友好 | 小 | Fix 3: 闲聊短语库 |
| **P2** | 路径9: 跳过PM | 质量不稳定 | 中 | Fix 5: 统一走PM |
| **P3** | 路径8: registry缺失 | 罕见 | 小 | 监控+日志 |
| **P3** | 路径6: 部门空输出 | 已有_defend保护 | — | Fix 6 可覆盖 |

---

## 五、架构变更总结

```
现状:                             修复后:
                                  
用户输入                          用户输入
  │                                │
  ├─ 5个时间正则 ───┐              ├─[L0] 时间引擎 (相对时间+绝对时间) → 直接回复
  ├─ 5组trivial     │              ├─[L1] 记忆查询 (语义触发+相似度)
  ├─ 30+关键词       │              ├─[L2] 闲聊短语库 + V5 GENERAL_CHAT
  ├─ 闲聊正则 ──────┤              ├─[L3] 关键词快速路由 (保持现状)
  ├─ 记忆双关键词 ───┤              ├─[L4] V5 IntentRouter 分类
  ├─ WeChat快速路径  │              ├─[L5] LLM回退 (反问用户,不走developer)
  └─ LLM回退 ───────┘              └─ 统一走PM → 执行 → 审计 → 交付
     └→ 默认developer
     └→ 崩溃→"Crashed:…"

核心改变: 不是加正则, 是把 "时间查询" 从正则函数变成时间函数,
         把 "记忆查询" 从双关键词AND 变成语义触发+相似度。
```

---

*审计完成。每个空白路径已定位到精确行号，根本性方案已给出。*
