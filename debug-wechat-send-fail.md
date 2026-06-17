# Debug Session: wechat-send-fail

- Status: OPEN
- User symptom: 在 ai-company 对话中发送“发送消息给微信联系人，小媛儿宝儿，发送消息：多喝水。”后，无法自动完成微信发送。
- Expected: 请求应被稳定识别为微信发送任务，进入 `wechat_send` 工具链，并在微信桌面版中完成真实发送或返回明确失败原因。

## Hypotheses

1. 微信发送请求没有稳定命中 triage 快路径，导致没有直接调用 `send_wechat_message()`。
2. 即使命中了 devops 路径，`CapabilityPlanner` 仍可能覆盖掉 `wechat_send`，让 agent 拿不到消息工具。
3. `wechat_send` 实际执行了，但底层返回 `success=false` 时被执行层误判为成功，掩盖了真实失败状态。
4. 微信 UI 自动化卡在某个状态机步骤，例如 `CHECK_WECHAT`、`ENSURE_SEARCH`、`FIND_CONTACT` 或 `VERIFY_SENT`。
5. 本机环境存在权限/窗口状态问题，例如 `System Events` 权限、微信未前台可见、或搜索框/联系人识别不稳定。

## Plan

1. 启动调试服务器并接入最小埋点。
2. 真实复现用户请求，记录 triage、capability、wechat tool、coordinator 各阶段日志。
3. 根据证据判断失败发生在入口、工具分配、执行层还是微信 UI 自动化层。
4. 基于证据做最小修复并再次真实验证。

## Evidence Notes

- 真实复现 `run_ceo("发送消息给微信联系人，小媛儿宝儿，发送消息：多喝水。")` 时，链路稳定命中 CEO 微信 fast-path，并直接返回 devops 失败结果。
- 修复前最新真实失败为 `Search box not opening`，但独立探测表明：
  - `can_control_ui() == true`
  - `activate() == true`
  - `open_search() == true`
  - 前后截图只显示桌面和微信菜单栏，没有可见聊天窗口
  - `System Events` 查询到 WeChat 进程存在窗口，但窗口没有真正聚焦到当前桌面
- 这说明根因不是入口路由，而是 `CHECK_WECHAT` 阶段把“只有微信菜单栏激活、窗口未真正可交互”误判成成功，随后才在 `ENSURE_SEARCH` 以模糊文案失败。

## Fix Applied

- `src/wechat/action.py`
  - 新增 `reopen + activate` 和窗口状态探测。
  - 新增 `focus_window()` 与 `get_window_state()`，用于区分窗口数量、最小化、主窗口、焦点状态。
- `src/wechat/coordinator.py`
  - 收紧 `CHECK_WECHAT` 成功条件：除了视觉判断外，还要求窗口存在、未最小化、并且是主窗口或已聚焦。
  - 为“微信 app 已激活但聊天窗口未聚焦到当前桌面”返回明确、可操作的错误说明。
  - 在 `CHECK_WECHAT` / `ENSURE_SEARCH` 中追加窗口状态埋点，便于后续继续追查真正发送成功问题。
- `tests/test_wechat_flow.py`
  - 新增窗口状态解析测试。
  - 新增 coordinator 在窗口未聚焦时返回明确错误的回归测试。

## Latest Verification

- `pytest tests/test_wechat_flow.py -q` → `8 passed`
- 真实 `run_ceo(...)` 最新结果：
  - `final_output`: `发送失败: WeChat app is active, but no chat window is focused on the current desktop. Bring a WeChat window to the current Space and retry.`
  - 结论：错误已稳定透传到 ai-company 对话层，但当前宿主环境下仍未完成真实发送；下一步需要解决微信窗口切换到当前 Space / 真正聚焦的问题。

## Additional Runtime Evidence

- 额外做了宿主环境探针，不再只看微信：
  - 使用 AppleScript 打开 `TextEdit` 新文档后，再执行 `screencapture`
  - 截图结果仍然只有桌面壁纸和菜单栏，没有 `TextEdit` 窗口内容
- 说明问题不是单独的微信窗口切换，而是当前宿主的屏幕采集链路本身拿不到前台应用窗口内容。
- 进一步使用系统 API 预检：
  - `CGPreflightScreenCaptureAccess()` 返回 `0`
  - 这与前述 `screencapture` 现象一致，确认当前环境缺少可用的 `Screen Recording` 权限

## Follow-up Fix

- `src/wechat/vision.py`
  - 新增 `can_capture_screen()`，直接调用 macOS `CGPreflightScreenCaptureAccess()` 做屏幕录制前置检查。
- `src/wechat/coordinator.py`
  - 在 `System Events` 检查之后、进入视觉状态机之前增加 `Screen Recording` preflight。
  - 若不可用，直接返回：
    - `Screen Recording unavailable. Enable Screen Recording permission for the host app/terminal and retry.`
- `tests/test_wechat_flow.py`
  - 补充缺少 `Screen Recording` 权限时的回归测试。

## Current Verified Result

- `pytest tests/test_wechat_flow.py -q` → `9 passed`
- 最新真实 `run_ceo(...)` 结果：
  - `final_output`: `发送失败: Screen Recording unavailable. Enable Screen Recording permission for the host app/terminal and retry.`
- 当前结论更新为：
  - 微信发送逻辑已能在对话层稳定暴露真正的宿主前置条件失败
  - 在当前机器环境下，未授予 `Screen Recording` 权限前，视觉引导发送链路无法真正完成发送

## Recheck After User Retry

- 再次复测当前宿主前置条件：
  - `screen_capture_access: false`
  - `can_control_ui: true`
  - `can_capture_screen: false`
- 再次真实运行 `run_ceo(...)`，结果仍为：
  - `发送失败: Screen Recording unavailable. Enable Screen Recording permission for the host app/terminal and retry.`
- 结论：
  - `Accessibility` 已可用
  - `Screen Recording` 仍未对当前实际宿主生效，因此链路没有推进到 `CHECK_WECHAT/ENSURE_SEARCH`

## Final Root Cause

- 权限问题解决后，链路继续卡在发送阶段，新的运行时证据表明根因不是 `VERIFY_SENT` 本身，而是更早的消息输入阶段存在“假成功”。
- 关键证据：
  - 发送前探针显示 `_type_and_verify_message()` 返回成功，但截图中底部聊天输入框并没有出现 `多喝水`
  - 失败后的截图多次显示左上角搜索浮层残留，说明消息可能被输入到搜索框或焦点落点错误
  - 收紧 `check_message_typed()` 提示词后，之前 `Tab` 导航实验里的“成功”全部消失，证明旧逻辑把搜索框文本误判成了聊天输入框文本
- 最终确认的根因有两部分：
  - `check_message_typed()` 会把左上角搜索框误识别成底部聊天输入框
  - 发送前存在搜索浮层/焦点残留脏状态，导致状态机起始条件不稳定
  - 自动化没有真正点击到底部聊天输入框；用户人工点击输入框后，粘贴与发送可以成功完成

## Final Fix

- `src/wechat/vision.py`
  - 收紧 `PROMPT_MESSAGE_TYPED`，只允许识别底部聊天输入框，不再把左上角搜索框当成消息输入框。
- `src/wechat/action.py`
  - 新增 `press_tab()`，为后续焦点切换提供显式动作。
  - `type_text()` 支持 `select_all=False`，避免默认 `Cmd+A` 误伤搜索框内容。
- `src/wechat/coordinator.py`
  - 在 `send()` 前新增 `_prepare_clean_window()`，发送前先做窗口清场，尽量消除搜索浮层和残留焦点。
  - `_type_and_verify_message()` 不再“点了就算成功”，必须经过视觉确认底部输入框真的出现目标消息才算成功。
  - 发送阶段优先使用 `check_chat_open(contact)` 返回的 `input_center_x / input_center_y` 直接点击底部聊天输入框，再做输入与校验。
  - 仅在视觉定位失败时，才退回 `Tab` 焦点切换和粗略坐标点击。
- `tests/test_wechat_flow.py`
  - 补充回归测试，覆盖：
    - 无 `Screen Recording` 权限时的 preflight
    - `type_text(select_all=False)` 行为
    - `_type_and_verify_message()` 必须真实看到消息出现在输入框后才返回成功

## Post-fix Verification

- 回归测试：
  - `pytest tests/test_wechat_flow.py -q` → `11 passed`
- 真实 `WechatCoordinator.send("小媛儿宝儿", "多喝水")`：
  - 返回：
    - `{"success": true, "contact": "小媛儿宝儿", "message": "多喝水", "output": "Sent to 小媛儿宝儿"}`
- 真实 `run_ceo("发送消息给微信联系人，小媛儿宝儿，发送消息：多喝水。")`：
  - 返回：
    - `final_output: 微信消息已发送给 小媛儿宝儿：多喝水`
  - `execution_log`：
    - `[TRIAGE] WeChat fast-path -> sent to 小媛儿宝儿`
    - `[DELIVER] Task complete. Score: 95`
- 用户补充人工复验：
  - 旧逻辑下，`多喝水` 会被输到搜索框；当用户人工点击底部输入框后，消息即可正常发送
  - 与修复方向一致，进一步证明“定位并点击真正的聊天输入框”是正确修复路径

## Current Status

- 调试会话状态建议更新为：`待用户人工确认`
- 当前代码侧与真实运行侧都已显示成功，但仍需用户确认微信联系人实际收到消息。
- 在你确认前，不清理以下调试资产：
  - `debug-wechat-send-fail.md`
  - `.dbg/trae-debug-log-wechat-send-fail.ndjson`
  - 本轮新增的若干 `.dbg/*probe*.py` / `.dbg/*.json` / `.dbg/*.png`
