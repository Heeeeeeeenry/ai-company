#!/usr/bin/env python3
"""模型配置层单测：ai-company 自有 DeepSeek 配置 + 端点级凭证解析。

跑法：python3 scripts/test_model_config.py
不联网、不改用户配置（用临时 config 文件 + 临时 env）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def fresh(env):
    """清掉相关 env 后重新导入 model_config。"""
    for k in list(os.environ):
        if k.startswith("AI_COMPANY_") or k in (
            "ONEAPI_API_KEY", "ONEAPI_BASE_URL", "ONEAPI_MODEL",
            "DEEPSEEK_API_KEY", "ENGRA_LLM_BASE_URL", "ENGRAM_LLM_MODEL",
        ):
            del os.environ[k]
    os.environ.update(env)
    for m in ("src.model_config", "src.llm_factory"):
        sys.modules.pop(m, None)
    import src.model_config as mc
    return mc


tmpdir = tempfile.mkdtemp()

# ── 1. 缺省（无 config 文件、无 env）= 老行为不变 ──────────────────────
mc = fresh({})
empty = os.path.join(tmpdir, "none.json")
rm = mc.load_runtime_model(__import__("pathlib").Path(empty))
check("缺省 provider=oneapi", rm.provider == "oneapi", f"{rm.provider}")
check("缺省 base_url 仍为内网 OneAPI", mc.is_oneapi_endpoint(rm.base_url), rm.base_url)
check("缺省 model=DeepSeek-V4-Flash", rm.model == "DeepSeek-V4-Flash", rm.model)

# ── 2. 现有线上 config.json（oneapi / DeepSeek-V4-Flash）不回归 ────────
p = os.path.join(tmpdir, "model_config.json")
with open(p, "w") as f:
    json.dump({"provider": "oneapi", "model": "DeepSeek-V4-Flash",
               "base_url": "https://oneapi-comate.baidu-int.com/v1"}, f)
rm = mc.load_runtime_model(__import__("pathlib").Path(p))
check("存量 config 原样加载", (rm.provider, rm.model) == ("oneapi", "DeepSeek-V4-Flash"),
      f"{rm.provider}/{rm.model}")
check("存量 config 端点仍是 oneapi", mc.is_oneapi_endpoint(rm.base_url), rm.base_url)

# ── 3. provider=deepseek 未给 base_url → 官方端点，且不改模型名 ─────────
mc = fresh({"AI_COMPANY_MODEL_PROVIDER": "deepseek"})
rm = mc.load_runtime_model(__import__("pathlib").Path(empty))
check("deepseek 无 base_url → 官方端点",
      rm.base_url == "https://api.deepseek.com/v1", rm.base_url)
check("官方端点不是 oneapi", not mc.is_oneapi_endpoint(rm.base_url), rm.base_url)

# ── 4. deepseek-chat 在官方端点上必须原样保留（不重定向） ──────────────
mc = fresh({"AI_COMPANY_DEEPSEEK_PROVIDER": "deepseek",
            "AI_COMPANY_DEEPSEEK_MODEL": "deepseek-chat"})
rm = mc.load_runtime_model(__import__("pathlib").Path(empty))
check("官方端点保留 deepseek-chat", rm.model == "deepseek-chat", rm.model)

# ── 5. deepseek-chat 在内网端点上仍重定向为可用模型（老行为） ──────────
mc = fresh({"AI_COMPANY_MODEL_PROVIDER": "deepseek",
            "AI_COMPANY_MODEL": "deepseek-chat",
            "AI_COMPANY_MODEL_BASE_URL": "https://oneapi-comate.baidu-int.com/v1"})
rm = mc.load_runtime_model(__import__("pathlib").Path(empty))
check("内网端点重定向 deepseek-chat → DeepSeek-V4-Flash",
      rm.model == "DeepSeek-V4-Flash", rm.model)

# ── 6. 凭证按端点解析，绝不串用 ───────────────────────────────────────
mc = fresh({"AI_COMPANY_DEEPSEEK_API_KEY": "sk-DEEPSEEK-own",
            "AI_COMPANY_ONEAPI_API_KEY": "sk-ONEAPI"})
check("内网端点用 OneAPI key",
      mc.resolve_api_key("https://oneapi-comate.baidu-int.com/v1") == "sk-ONEAPI")
check("官方端点用自有 DeepSeek key",
      mc.resolve_api_key("https://api.deepseek.com/v1") == "sk-DEEPSEEK-own")

# ── 7. 只配了自有 key 时，内网端点回退（不炸） ────────────────────────
mc = fresh({"AI_COMPANY_DEEPSEEK_API_KEY": "sk-only-own"})
check("只有自有 key 时内网端点回退",
      mc.resolve_api_key("https://oneapi-comate.baidu-int.com/v1") == "sk-only-own")

# ── 8. AI_COMPANY_* 优先级高于通用的 DEEPSEEK_API_KEY ─────────────────
mc = fresh({"DEEPSEEK_API_KEY": "sk-legacy", "AI_COMPANY_DEEPSEEK_API_KEY": "sk-own"})
check("AI_COMPANY_* 覆盖 DEEPSEEK_API_KEY", mc.get_deepseek_key() == "sk-own")

# ── 9. 会话作用域：artifact 存储按会话分目录 ──────────────────────────
for m in ("src.memory.artifacts", "src.session.scope"):
    sys.modules.pop(m, None)
from src.session.scope import scope

ws = tempfile.mkdtemp()
from src.memory.artifacts import ArtifactStore
store = ArtifactStore(workspace=ws)
store.put("k_global", {"v": "unscoped"})
with scope("conv-A"):
    store.put("k_a", {"v": "A"})
    check("会话A 只看到自己的 artifact", store.keys() == ["k_a"], f"{store.keys()}")
with scope("conv-B"):
    check("会话B 看不到会话A 的 artifact",
          "k_a" not in store.keys() and store.get("k_a") is None, f"{store.keys()}")
check("无作用域时仍读老目录（CLI 不回归）",
      "k_global" in store.keys(), f"{store.keys()}")

print("\n" + "=" * 46)
if FAIL:
    print(f"结果: {len(FAIL)} 项失败 -> {FAIL}")
    sys.exit(1)
print("结果: 全部通过 ✅")
