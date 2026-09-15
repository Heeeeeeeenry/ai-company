# AI Company V5 — Enterprise Agent Architecture

## 设计目标

从「软件公司模拟」进化为「通用AI Agent系统」。

## 架构层级（9层）

```
User → Intent Router → Workflow Planner → Capability Registry → Agent Pool
                                                                    ↓
Response ← Memory Writer ← Self-Healing ← Verifier ← Execution Layer
```

---

## P0.1: Intent Router (src/intent/router.py)

职责：第一层路由，所有请求先经过这里。

### 意图分类（11种）

| 意图 | 说明 | 示例 |
|------|------|------|
| COMMAND | Shell命令 | pwd, ls, cat ~/.zshrc |
| SEARCH | 信息搜索 | 查金价, 最近天气 |
| RESEARCH | 深度研究 | 分析OpenClaw, 调研框架 |
| VISION | 视觉任务 | 截图分析, 识别UI |
| SOCIAL | 社交/微信 | 给小号发微信, 聊天 |
| MEMORY | 记忆操作 | 记住XXX, 之前聊过什么 |
| CODING | 编码任务 | 写一个API, 修bug |
| SYSTEM | 系统操作 | 打开微信, 检查进程 |
| FILE | 文件操作 | 读取文件, 生成PDF |
| AUTOMATION | 自动化 | 批量处理, 定时任务 |
| GENERAL_CHAT | 闲聊 | 你好, 今天如何 |

### 实现方式

```python
class IntentRouter:
    def classify(self, text: str) -> IntentResult:
        # Layer 1: Fast keyword match (0 LLM cost)
        # Layer 2: LLM classification (for ambiguous input)
        pass

@dataclass
class IntentResult:
    intent: str          # COMMAND|SEARCH|RESEARCH|...
    confidence: float    # 0-1
    params: dict         # extracted parameters
    routing_hint: str    # suggested agent
```

### 关键词快路（无需LLM）

优先级从高到低：
1. Shell命令检测 → COMMAND
2. 微信社交相关 → SOCIAL
3. 搜索/查询 → SEARCH
4. 编码/开发 → CODING
5. 系统操作 → SYSTEM
6. LLM兜底

---

## P0.2: Capability Registry (src/capability/registry.py)

职责：完全可插拔的能力注册表，新增能力无需修改代码。

### 接口

```python
class Capability:
    name: str           # web_search, vision, wechat, shell, coding...
    description: str    # 自然语言描述
    agent: str          # 默认处理Agent
    tools: list[str]    # 关联工具
    requires: list[str] # 前置能力

class CapabilityRegistry:
    def register(self, cap: Capability) -> None
    def unregister(self, name: str) -> None
    def resolve(self, intent: str) -> list[Capability]
    def list_all(self) -> list[Capability]
    def get_agent_for_capability(self, name: str) -> str
```

### 默认能力

```python
DEFAULT_CAPABILITIES = {
    "web_search": Capability(name="web_search", agent="ResearchAgent",
        tools=["web_search", "web_fetch", "market_series", "weather"]),
    "vision": Capability(name="vision", agent="VisionAgent",
        tools=["vision_analyze", "screenshot"]),
    "shell": Capability(name="shell", agent="SystemAgent",
        tools=["run_command", "process_manage"]),
    "wechat": Capability(name="wechat", agent="WechatAgent",
        tools=["wechat_send", "wechat_read"]),
    "coding": Capability(name="coding", agent="CodingAgent",
        tools=["read_file", "write_file", "run_python", "patch"]),
    "file_io": Capability(name="file_io", agent="SystemAgent",
        tools=["read_file", "write_file"]),
    "browser": Capability(name="browser", agent="BrowserAgent",
        tools=["browser_navigate", "browser_click"]),
    "memory": Capability(name="memory", agent="MemoryAgent",
        tools=["memory_search", "memory_save"]),
}
```

### 意图→能力映射

```python
INTENT_CAPABILITY_MAP = {
    "COMMAND": ["shell"],
    "SEARCH": ["web_search"],
    "RESEARCH": ["web_search", "file_io"],
    "VISION": ["vision"],
    "SOCIAL": ["wechat", "vision"],
    "MEMORY": ["memory"],
    "CODING": ["coding", "file_io", "web_search"],
    "SYSTEM": ["shell"],
    "FILE": ["file_io"],
    "AUTOMATION": ["shell", "web_search"],
    "GENERAL_CHAT": [],
}
```

### 新增能力示例

```python
# 未来添加 Excel 能力（零代码改动）
registry.register(Capability(
    name="excel",
    description="Excel文件读写和处理",
    agent="ExcelAgent",
    tools=["excel_read", "excel_write", "excel_format"]
))
```

---

## P0.3: Verifier (src/verification/verifier.py)

职责：执行后验证，确保操作确实成功。

### 接口

```python
class Verifier:
    async def verify(self, intent: str, result: dict, context: dict) -> VerifyResult:
        """根据意图类型选择合适的验证策略"""
        pass

@dataclass
class VerifyResult:
    success: bool
    score: int            # 0-100
    detail: str           # 验证详情
    needs_retry: bool     # 是否需要重试
    retry_strategy: str   # 重试策略
```

### 验证策略

| 意图 | 验证方式 |
|------|----------|
| COMMAND | 检查output非空 + 无error关键字 |
| SEARCH | 检查结果包含数据源 + 非stale |
| SOCIAL/WeChat | 截图→Vision确认消息已发送 |
| CODING | AST语法检查 + lint |
| FILE | 检查文件存在 + 内容正确 |
| SYSTEM | 检查进程状态 |

### WeChat 验证（关键）

```
发送消息 → 截图 → Vision确认 → {sent: true, content_match: true}
```

绝不盲信成功。Vision无法确认时返回 fail。

---

## P1.1: Dynamic Workflow Planner (src/workflow/planner.py)

职责：根据意图和能力动态生成执行计划，不再写死流程。

### 接口

```python
class WorkflowPlanner:
    def plan(self, intent: IntentResult, capabilities: list[Capability]) -> WorkflowPlan:
        """生成执行计划"""
        pass

@dataclass
class WorkflowPlan:
    steps: list[WorkflowStep]
    reviewers: list[str]
    skip_audit: bool

@dataclass  
class WorkflowStep:
    name: str
    agent: str
    action: str
    params: dict
    depends_on: list[str]  # 前置步骤名
```

### 示例

```
查金价 → SEARCH
  Plan: [search_gold_price(summarize=False)]

写代码 → CODING
  Plan: [requirements_analysis, coding, test, review]

发微信 → SOCIAL
  Plan: [locate_wechat, open_chat, send_message, verify_sent]
```

---

## P1.2: Memory Layer (src/memory/)

职责：三级记忆系统。

### Session Memory（当前会话）
- 生命周期：当前窗口
- 存储：短期上下文

### User Memory（用户全局）
- 生命周期：永久
- 存储：用户偏好、环境信息
- 示例：language=中文, os=macOS, code_style=PEP8

### Knowledge Memory（能力知识）
- 生命周期：永久
- 存储：所有会话共享的能力知识
- 示例：wechat_send_fix=已验证, paste_throttle_workaround=重启微信

### 接口

```python
class MemoryLayer:
    # Session
    def session_set(key, value)
    def session_get(key)
    
    # User
    def user_set(key, value) 
    def user_get(key)
    
    # Knowledge
    def knowledge_set(key, value)
    def knowledge_get(key)
    def knowledge_search(query)  # 语义搜索
```

---

## 集成到现有 graph.py

Phase 1（本次P0）：最小侵入式集成

```python
# graph.py triage_node 改为：
from src.intent.router import IntentRouter
router = IntentRouter()
intent = router.classify(task)
# 根据 intent.intent 路由到对应 agent
# 替代现有的 classify_task() + keyword matching

# graph.py verify_aggregate_node 改为：
from src.verification.verifier import Verifier
verifier = Verifier()
result = await verifier.verify(intent, execution_result, context)
```

---

## 文件结构

```
src/
├── intent/
│   ├── __init__.py
│   └── router.py          # IntentRouter + IntentResult
├── capability/
│   ├── __init__.py
│   └── registry.py        # CapabilityRegistry + Capability（重写）
├── verification/
│   ├── __init__.py
│   └── verifier.py        # Verifier + VerifyResult
├── workflow/
│   ├── __init__.py
│   ├── planner.py         # WorkflowPlanner（增强）
│   ├── registry.py        # Capability + Agent注册
│   └── reviewer.py        # Dynamic Reviewer
├── memory/
│   ├── __init__.py
│   └── layer.py           # MemoryLayer（三级统一）
├── agents/
│   ├── __init__.py
│   └── pool.py            # AgentPool（动态agent管理）
├── ceo/
│   └── graph.py           # 集成入口（最小改动）
```

---

## 不兼容旧代码的说明

以下旧代码将被替代：

| 旧模块 | 替代 |
|--------|------|
| graph.py classify_task() | intent/router.py IntentRouter |
| departments/agents.py ROLE_CAPABILITIES | capability/registry.py CapabilityRegistry |
| departments/roles.py ROLE_REGISTRY | agents/pool.py AgentPool |
| graph.py verify_aggregate_node | verification/verifier.py Verifier |
| workflow/registry.py (旧版) | capability/registry.py (新版) |
| graph.py triage_node 关键词快路 | intent/router.py keyword fast-path |

## 测试策略

每次改动后必须执行：
1. AST语法检查
2. COMMAND测试 (pwd)
3. SIMPLE_QUERY测试 (查金价)
4. WECHAT_CANARY测试 (给小号发微信)
5. CODING测试 (写代码)
