#!/usr/bin/env python3
"""
engram-router 效果自测脚本
模拟真实多话题对话 → 存储记忆 → 检索验证

用法:
  cd ~/.openclaw/workspace/ai-company
  python tests/manual_engram_smoke.py

场景设计（4个主题、20+轮对话交叉存储）:
  张三线: 同事送键盘 → 品牌、原因、公司
  妈妈线: 年龄、职业、做饭、性格
  车线: 品牌、原因、通勤
  猫线: 名字、品种、性格
  负样本: 不该知道的信息 → 应正确拒答
"""
import os, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 确保使用 engram 后端 ──
# (engram is now the ONLY backend — no env var needed)

from importlib import reload
import src.memory.hermes as hm

reload(hm)
mem = hm.hermes_memory

RESULTS = []
def check(name, condition, detail=""):
    ok = bool(condition)
    mark = "✅" if ok else "❌"
    msg = f"  {mark} {name}"
    if detail and not ok:
        msg += f"  ← 期望: {detail}"
    RESULTS.append((name, ok, detail))
    print(msg)

# ═══════════════════════════════════════════
# PHASE 1: 喂入多话题记忆（模拟 20+ 轮对话）
# ═══════════════════════════════════════════
print("=" * 60)
print("PHASE 1: 喂入多话题数据")
print("=" * 60)

conversations = [
    # 张三线 (同事)
    ("张三是我前同事，现在在腾讯做后端开发。", "memory"),
    ("他前两天送了我一把 HHKB 键盘，说是生日礼物。", "memory"),
    ("他说是因为我生日，知道我一直喜欢机械键盘。", "memory"),
    ("张三平时话不多，但是很靠谱。", "memory"),

    # 妈妈线
    ("我妈妈今年55岁，是个退休教师。", "memory"),
    ("妈妈脾气很温和，从来不发火。", "memory"),
    ("妈妈做的红烧肉特别好吃，肥而不腻。", "memory"),
    ("昨天妈妈给我做了一桌子菜。", "memory"),

    # 车线
    ("我上个月买了一辆特斯拉 Model 3。", "memory"),
    ("买车主要是因为通勤太远，每天来回80公里。", "memory"),
    ("这车开起来很安静，加速也快。", "memory"),

    # 猫线
    ("我养了一只猫，叫咪咪，是英短。", "memory"),
    ("咪咪很黏人，喜欢趴在我腿上。", "memory"),
    ("咪咪是蓝灰色的，特别好看。", "memory"),

    # 混杂
    ("我朋友李四也想买电动车，但还没下手。", "memory"),
    ("我现在租住在北京朝阳区，月租六千。", "memory"),
    ("我室友王五是做 UI 设计的。", "memory"),
    ("搬家是因为离公司近，通勤方便。", "memory"),
    ("我新换了一部 iPhone 15 Pro。", "memory"),
    ("换手机是因为旧手机电池不行了。", "memory"),
]

for text, target in conversations:
    mem.add(text, target)
print(f"  喂入 {len(conversations)} 条记忆\n")

# ═══════════════════════════════════════════
# PHASE 2: 正样本检索 —— 精确召回
# ═══════════════════════════════════════════
print("=" * 60)
print("PHASE 2: 正样本 —— 应该命中")
print("=" * 60)

tests_positive = [
    # (查询, 答案关键词, 说明)
    ("我同事送我的键盘是什么牌子？", "HHKB", "张三→品牌"),
    ("张三送了我什么？", "HHKB", "张三→礼物"),
    ("他为什么送我这个键盘？", "生日", "因果链"),
    ("张三现在在哪家公司？", "腾讯", "张三→公司"),
    ("我妈妈多大年纪？", "55", "妈妈→年龄"),
    ("妈妈什么职业？", "退休教师", "妈妈→职业"),
    ("妈妈做饭好吃吗？", "好吃", "妈妈→评价"),
    ("妈妈最拿手的菜？", "红烧肉", "妈妈→菜品"),
    ("我的车什么牌子？", "特斯拉", "车→品牌"),
    ("为什么买车？", "通勤", "车→原因"),
    ("每天通勤多远？", "80", "车→距离"),
    ("我的猫叫什么？", "咪咪", "猫→名字"),
    ("咪咪什么品种？", "英短", "猫→品种"),
    ("咪咪什么颜色？", "蓝灰", "猫→颜色"),
    ("咪咪性格怎么样？", "黏人", "猫→性格"),
    ("我房租多少？", "六千", "租房→价格"),
    ("住在哪个区？", "朝阳", "租房→位置"),
    ("室友叫什么？", "王五", "室友→名字"),
    ("室友做什么的？", "设计", "室友→职业"),
    ("为什么搬家？", "离公司近", "搬家→原因"),
    ("新手机什么型号？", "iPhone", "手机→型号"),
    ("为什么换手机？", "电池", "手机→原因"),
]

for query, keyword, desc in tests_positive:
    results = mem.search(query, "memory")
    top_texts = " ".join(r.get("text", "") for r in results[:3])
    hit = keyword in top_texts
    check(f"{desc} ('{keyword}')", hit, f"top-3 应包含 '{keyword}'")

# ═══════════════════════════════════════════
# PHASE 3: 跨主题隔离 —— 不应污染
# ═══════════════════════════════════════════
print()
print("=" * 60)
print("PHASE 3: 跨主题隔离 —— 不应串线")
print("=" * 60)

tests_isolation = [
    ("张三多大年纪？", ["55"], "张三不应召回妈妈的年龄"),
    ("妈妈在哪家公司？", ["腾讯"], "妈妈不应召回张三的公司"),
    ("李四买的什么车？", ["特斯拉"], "李四没车，不应召回我的特斯拉"),
    ("我的车多少钱买的？", ["六千"], "车价未知，不应串到房租"),
    ("咪咪几岁了？", ["55"], "猫年龄未知，不应串到妈妈年龄"),
    ("室友多大年纪？", ["55"], "室友年龄未知，不应串到妈妈年龄"),
    ("张三脾气怎么样？", ["温和", "不发火"], "张三无性格描述，不应串到妈妈"),
    ("妈妈在公司做什么？", ["腾讯", "后端"], "妈妈是退休教师，不是程序员"),
]

for query, forbidden, desc in tests_isolation:
    results = mem.search(query, "memory")
    top_texts = " ".join(r.get("text", "") for r in results[:3])
    leaked = [w for w in forbidden if w in top_texts]
    ok = len(leaked) == 0
    detail = f"不应含 {leaked}" if leaked else ""
    check(desc, ok, detail)

# ═══════════════════════════════════════════
# PHASE 4: 负样本 —— 真实不知道
# ═══════════════════════════════════════════
print()
print("=" * 60)
print("PHASE 4: 负样本 —— 应正确 '不知道'")
print("=" * 60)

tests_negative = [
    ("我女朋友叫什么？", ["张三", "王五", "李四"], "没有女朋友"),
    ("我家狗叫什么？", ["咪咪"], "没有狗，不应召回猫"),
    ("我在哪家公司上班？", ["腾讯"], "我的公司未知，不应串张三的"),
    ("春节去哪玩？", ["朝阳", "通勤"], "无旅行计划，不应瞎匹配"),
    ("张三开什么车？", ["特斯拉", "Model"], "张三的车未知，不应串我的"),
]

for query, forbidden, desc in tests_negative:
    results = mem.search(query, "memory")
    top_texts = " ".join(r.get("text", "") for r in results[:3])
    leaked = [w for w in forbidden if w in top_texts]
    ok = len(leaked) == 0
    detail = f"top-3 不应含 {leaked}" if leaked else ""
    check(desc, ok, detail)

# ═══════════════════════════════════════════
# PHASE 5: 基础功能
# ═══════════════════════════════════════════
print()
print("=" * 60)
print("PHASE 5: 基础功能")
print("=" * 60)

# stats
s = mem.stats()
check(f"backend=engram (got: {s.get('backend')})", 
      s.get("backend") == "engram")
check(f"memory_entries ≥ 20 (got: {s.get('memory_entries')})",
      s.get("memory_entries", 0) >= 20)

# context
ctx = mem.get_full_context()
check(f"get_full_context 含 MEMORY 块 ({len(ctx)} chars)",
      "MEMORY" in ctx)
check(f"get_full_context 含 USER PROFILE 块",
      "USER PROFILE" in ctx)

# replace
mem.add("测试替换：我的旧手机是华为", "memory")
mem.replace("我的旧手机是华为", "测试替换：我的旧手机是小米", "memory")
r = mem.search("旧手机", "memory")
check("replace: '小米' 已替换 '华为'",
      any("小米" in x.get("text","") for x in r) and 
      not any("华为" in x.get("text","") for x in r))

# remove
mem.remove("测试替换", "memory")
r2 = mem.search("旧手机", "memory")
check("remove: '旧手机' 已删除",
      not any("旧手机" in x.get("text","") for x in r2))

# ═══════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════
print()
print("=" * 60)
passed = sum(1 for _, ok, _ in RESULTS if ok)
failed = sum(1 for _, ok, _ in RESULTS if not ok)
total = len(RESULTS)
print(f"结果: {passed}/{total} 通过" + (f", {failed} 失败 ❌" if failed else " ✅ ALL PASS"))
print()

if failed:
    print("失败项:")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ❌ {name}")
            if detail:
                print(f"     {detail}")
    print()

# 效果总结
print("=" * 60)
print("效果简要分析")
print("=" * 60)
phases = {
    "正样本召回": ("PHASE 2", lambda r: r[0].startswith("PHASE 2") is False),
}
p2_tests = RESULTS[22:22+22]  # Hmm, better to count properly

# 重新组织统计
total_tests = len(RESULTS)
phase_names = ["P2 正样本", "P3 跨主题隔离", "P4 负样本拒答", "P5 基础功能"]
# 手动数：P2有22个, P3有8个, P4有5个, P5有5个 = 40 total
if total >= 38:
    p2_pass = sum(1 for _, ok, _ in RESULTS[0:22] if ok)
    p3_pass = sum(1 for _, ok, _ in RESULTS[22:30] if ok)
    p4_pass = sum(1 for _, ok, _ in RESULTS[30:35] if ok)
    p5_pass = sum(1 for _, ok, _ in RESULTS[35:40] if ok)
    
    print(f"  正样本召回:    {p2_pass}/22")
    print(f"  跨主题隔离:    {p3_pass}/8")
    print(f"  负样本拒答:    {p4_pass}/5")
    print(f"  基础功能:      {p5_pass}/5")
    
    star = "⭐" if passed >= total * 0.9 else "⚠️" if passed >= total * 0.7 else "❌"
    print(f"\n  综合评级: {star}  ({passed}/{total})")
