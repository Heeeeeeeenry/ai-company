#!/usr/bin/env python3
"""Test script for IntentRouter — validates all 11 intent types via keyword matching.

Usage:
    cd ~/.openclaw/workspace/ai-company
    python3 test/test_intent_router.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.intent.router import IntentRouter, classify_task, ALL_INTENTS


# ─── Test cases: (input, expected_intent, expected_task_type) ─────

TEST_CASES = [
    # ── COMMAND ───────────────────────────────────────────────
    ("pwd",                              "COMMAND",      "COMMAND_EXECUTION"),
    ("ls -la",                           "COMMAND",      "COMMAND_EXECUTION"),
    ("cat ~/.zshrc",                     "COMMAND",      "COMMAND_EXECUTION"),
    ("cd /tmp",                          "COMMAND",      "COMMAND_EXECUTION"),
    ("cp file1 file2",                   "COMMAND",      "COMMAND_EXECUTION"),
    ("mv old new",                       "COMMAND",      "COMMAND_EXECUTION"),
    ("grep -r 'TODO' .",                 "COMMAND",      "COMMAND_EXECUTION"),
    ("curl https://example.com",         "COMMAND",      "COMMAND_EXECUTION"),
    ("wget https://file.zip",            "COMMAND",      "COMMAND_EXECUTION"),
    ("git status",                       "COMMAND",      "COMMAND_EXECUTION"),
    ("ps aux",                           "COMMAND",      "COMMAND_EXECUTION"),
    ("kill 1234",                        "COMMAND",      "COMMAND_EXECUTION"),
    ("echo hello",                       "COMMAND",      "COMMAND_EXECUTION"),
    ("mkdir newdir",                     "COMMAND",      "COMMAND_EXECUTION"),
    ("rm -rf /tmp/test",                 "COMMAND",      "COMMAND_EXECUTION"),
    ("chmod +x script.sh",               "COMMAND",      "COMMAND_EXECUTION"),
    ("chown user:group file",            "COMMAND",      "COMMAND_EXECUTION"),
    ("df -h",                            "COMMAND",      "COMMAND_EXECUTION"),
    ("du -sh *",                         "COMMAND",      "COMMAND_EXECUTION"),
    ("top",                              "COMMAND",      "COMMAND_EXECUTION"),
    ("docker ps",                        "COMMAND",      "COMMAND_EXECUTION"),
    ("brew install python",              "COMMAND",      "COMMAND_EXECUTION"),
    ("systemctl status nginx",           "COMMAND",      "COMMAND_EXECUTION"),
    ("launchctl list",                   "COMMAND",      "COMMAND_EXECUTION"),

    # ── SYSTEM ────────────────────────────────────────────────
    ("打开微信",                          "SYSTEM",        "LOCAL_SYSTEM"),
    ("关闭QQ",                            "SYSTEM",        "LOCAL_SYSTEM"),
    ("启动浏览器",                        "SYSTEM",        "LOCAL_SYSTEM"),
    ("检查进程",                          "SYSTEM",        "LOCAL_SYSTEM"),
    ("打开Chrome",                        "SYSTEM",        "LOCAL_SYSTEM"),
    ("打开VS Code",                       "SYSTEM",        "LOCAL_SYSTEM"),
    ("查看应用状态",                      "SYSTEM",        "LOCAL_SYSTEM"),

    # ── SOCIAL ────────────────────────────────────────────────
    ("给小号发微信",                      "SOCIAL",        "LOCAL_SYSTEM"),
    ("发消息给张三",                      "SOCIAL",        "LOCAL_SYSTEM"),
    ("微信聊天",                          "SOCIAL",        "LOCAL_SYSTEM"),
    ("回复微信消息",                      "SOCIAL",        "LOCAL_SYSTEM"),
    ("代聊",                              "SOCIAL",        "LOCAL_SYSTEM"),
    ("发微信",                            "SOCIAL",        "LOCAL_SYSTEM"),

    # ── SEARCH ────────────────────────────────────────────────
    ("查金价",                            "SEARCH",        "SIMPLE_QUERY"),
    ("搜索天气",                          "SEARCH",        "SIMPLE_QUERY"),
    ("查询股价",                          "SEARCH",        "SIMPLE_QUERY"),
    ("今天天气怎么样",                    "SEARCH",        "SIMPLE_QUERY"),
    ("比特币多少钱",                      "SEARCH",        "SIMPLE_QUERY"),
    ("什么是Python",                      "SEARCH",        "SIMPLE_QUERY"),
    ("怎么安装docker",                    "SEARCH",        "SIMPLE_QUERY"),
    ("汇率多少",                          "SEARCH",        "SIMPLE_QUERY"),
    ("最新新闻",                          "SEARCH",        "SIMPLE_QUERY"),
    ("what is AI",                        "SEARCH",        "SIMPLE_QUERY"),
    ("who is Elon Musk",                  "SEARCH",        "SIMPLE_QUERY"),
    ("where is Shanghai",                 "SEARCH",        "SIMPLE_QUERY"),

    # ── CODING ────────────────────────────────────────────────
    ("写代码",                            "CODING",        "DEVELOPMENT"),
    ("开发一个API",                       "CODING",        "DEVELOPMENT"),
    ("实现登录功能",                      "CODING",        "DEVELOPMENT"),
    ("修复bug",                           "CODING",        "DEVELOPMENT"),
    ("fix the crash",                     "CODING",        "DEVELOPMENT"),
    ("重构代码",                          "CODING",        "DEVELOPMENT"),
    ("refactor this module",              "CODING",        "DEVELOPMENT"),
    ("测试这个函数",                      "CODING",        "DEVELOPMENT"),
    ("test the endpoint",                 "CODING",        "DEVELOPMENT"),
    ("write a Python script",             "CODING",        "DEVELOPMENT"),
    ("implement a REST API",              "CODING",        "DEVELOPMENT"),
    ("build a web app",                   "CODING",        "DEVELOPMENT"),
    ("create a new module",               "CODING",        "DEVELOPMENT"),
    ("检查代码",                           "CODE_REVIEW",   "CODE_REVIEW"),

    # ── CODE_REVIEW ───────────────────────────────────────────
    ("代码审查",                           "CODE_REVIEW",   "CODE_REVIEW"),
    ("代码审计",                           "CODE_REVIEW",   "CODE_REVIEW"),
    ("code review this PR",               "CODE_REVIEW",   "CODE_REVIEW"),
    ("review this code",                  "CODE_REVIEW",   "CODE_REVIEW"),
    ("审查代码质量",                        "CODE_REVIEW",   "CODE_REVIEW"),

    # ── CREATIVE ──────────────────────────────────────────────
    ("写文案",                             "CREATIVE",      "CREATIVE"),
    ("营销方案",                           "CREATIVE",      "CREATIVE"),
    ("SEO优化",                            "CREATIVE",      "CREATIVE"),
    ("广告语",                             "CREATIVE",      "CREATIVE"),
    ("creative copywriting",              "CREATIVE",      "CREATIVE"),

    # ── FILE ──────────────────────────────────────────────────
    ("读取文件",                          "FILE",          "DOCUMENT"),
    ("查看文档",                          "SEARCH",        "SIMPLE_QUERY"),   # SEARCH > FILE priority
    ("打开report.pdf",                    "FILE",          "DOCUMENT"),
    ("生成PDF报告",                       "FILE",          "DOCUMENT"),
    ("创建文档",                          "FILE",          "DOCUMENT"),
    ("写周报",                            "FILE",          "DOCUMENT"),
    ("导出会议纪要",                      "FILE",          "DOCUMENT"),

    # ── MEMORY ────────────────────────────────────────────────
    ("记住我的生日是1月1日",              "MEMORY",        "GENERAL"),
    ("回忆之前聊过什么",                  "MEMORY",        "GENERAL"),
    ("上次你说了什么",                    "MEMORY",        "GENERAL"),
    ("存储这个信息",                      "MEMORY",        "GENERAL"),
    ("帮我记一下",                        "MEMORY",        "GENERAL"),

    # ── AUTOMATION ────────────────────────────────────────────
    ("批量处理文件",                      "AUTOMATION",    "GENERAL"),
    ("定时检查邮件",                      "AUTOMATION",    "GENERAL"),        # 查 fix: 检查 no longer triggers SEARCH
    ("自动化部署",                        "CODING",        "DEVELOPMENT"),     # CODING > AUTOMATION
    ("每天自动备份",                      "AUTOMATION",    "GENERAL"),
    ("每周生成报告",                      "FILE",          "DOCUMENT"),        # FILE > AUTOMATION

    # ── RESEARCH ──────────────────────────────────────────────
    ("研究一下微服务架构",                "RESEARCH",      "SIMPLE_QUERY"),
    ("分析竞品",                          "RESEARCH",      "SIMPLE_QUERY"),
    ("调研AI框架",                        "RESEARCH",      "SIMPLE_QUERY"),
    ("对比React和Vue",                    "RESEARCH",      "SIMPLE_QUERY"),
    ("深度分析市场趋势",                  "RESEARCH",      "SIMPLE_QUERY"),

    # ── VISION ────────────────────────────────────────────────
    ("截图分析",                          "VISION",        "GENERAL"),
    ("识别这张图片",                      "VISION",        "GENERAL"),
    ("OCR文字识别",                       "VISION",        "GENERAL"),
    ("看图回答问题",                      "VISION",        "GENERAL"),

    # ── GENERAL_CHAT ──────────────────────────────────────────
    ("你好",                              "GENERAL_CHAT",  "GENERAL"),
    ("今天心情不好",                      "GENERAL_CHAT",  "GENERAL"),
    ("谢谢",                              "GENERAL_CHAT",  "GENERAL"),
    ("讲个笑话",                          "GENERAL_CHAT",  "GENERAL"),
    ("emmm让我想想",                      "GENERAL_CHAT",  "GENERAL"),
]


def main():
    router = IntentRouter()

    total = 0
    passed = 0
    failed = 0

    # Track coverage: which intents were tested
    intents_tested: set[str] = set()

    for text, expected_intent, expected_task_type in TEST_CASES:
        total += 1
        result = router.classify(text)
        task_type = classify_task(text)

        intent_ok = result.intent == expected_intent
        task_ok = task_type == expected_task_type
        all_ok = intent_ok and task_ok

        intents_tested.add(result.intent)

        if all_ok:
            passed += 1
            status = "✓"
        else:
            failed += 1
            status = "✗"

        if not all_ok:
            print(f"{status} '{text}'")
            if not intent_ok:
                print(f"     intent:     got={result.intent} expected={expected_intent}")
            if not task_ok:
                print(f"     task_type:  got={task_type} expected={expected_task_type}")
            print(f"     matched_by: {result.matched_by} confidence={result.confidence}")
            print()

    # ── Summary ──────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print(f"Intents covered: {len(intents_tested)}/{len(ALL_INTENTS)}")
    print(f"  {sorted(intents_tested)}")

    # Check all 11 intents are covered
    missing = set(ALL_INTENTS) - intents_tested
    if missing:
        print(f"  MISSING: {sorted(missing)}")
    else:
        print(f"  All 13 intents covered ✓")

    # ── Edge cases ───────────────────────────────────────────
    print(f"\nEdge cases:")
    for edge_input, desc in [
        ("", "empty string"),
        ("   ", "whitespace only"),
    ]:
        r = router.classify(edge_input)
        print(f"  '{desc}' → {r.intent} (matched_by={r.matched_by})")

    # ── Layer 1 efficiency ───────────────────────────────────
    keyword_matches = sum(
        1 for text, _, _ in TEST_CASES
        if router.classify(text).matched_by == "keyword"
    )
    print(f"\nLayer 1 (keyword) coverage: {keyword_matches}/{total} ({100*keyword_matches/total:.0f}%)")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
