# Release Notes

## 2026-04-20 - Codex SDK adapter with `@CODEX` routing

### 主要改动点
- 新增 `tools/codex_sdk_tool.py`，提供 `codex_run_task` 内部工具、`@CODEX` 前缀解析、Codex SDK 预检和 one-shot 执行封装。
- 新增 `tools/codex_sdk_bridge.mjs`，通过官方 `@openai/codex-sdk` 调用本地 Codex CLI，并把线程/进度/文件变更事件转成 JSONL。
- CLI、Gateway、TUI 都增加了显式 `@CODEX` fast-path：
  - `@CODEX <prompt>` 走 `standard`
  - `@CODEX:computer_use <prompt>` 走 `computer_use`
- `computer_use` 模式只做 Codex 委托，不在 Hermes 内实现桌面 click/type/screenshot loop。
- 新增 `codex` toolset，但没有加入核心默认工具集，避免普通模型自动选择它。

### 影响的接口 / 配置
- 新内部工具：`codex_run_task(prompt, mode="standard", cwd=null)`
- 新用户入口：
  - `@CODEX <prompt>`
  - `@CODEX:computer_use <prompt>`
- 新配置根节点：`codex_sdk`
  - `enabled`
  - `node_command`
  - `codex_command`
  - `timeout_seconds`
  - `allowed_roots`
  - `modes.standard.config_overrides`
  - `modes.standard.env`
  - `modes.computer_use.enabled`
  - `modes.computer_use.config_overrides`
  - `modes.computer_use.env`
- 新增 Node 依赖：`@openai/codex-sdk`

### 测试执行情况
- 通过：`python -m pytest -n 4 tests/tools/test_codex_sdk_tool.py tests/gateway/test_codex_sdk_route.py tests/tui_gateway/test_codex_prompt.py -q`
- 说明：仓库推荐的 `scripts/run_tests.sh` 在当前本地环境里因为 `venv` 缺少 `pip` 无法自动安装 `pytest-split`，因此本次改用激活虚拟环境后的 `pytest -n 4` 跑定向测试。
- 补充检查：
  - `python -m py_compile cli.py gateway/run.py tui_gateway/server.py hermes_cli/config.py hermes_cli/doctor.py toolsets.py tools/codex_sdk_tool.py`
  - `node --check tools/codex_sdk_bridge.mjs`

### 已知限制 / 后续项
- V1 仍然是 one-shot 调用，不持久化 Codex thread，也不支持 `resumeThread`。
- `@CODEX` 路由是显式前缀命令，不会在普通文本里自动触发。
- `computer_use` 是否真正可运行，仍取决于宿主机上的 Codex 自身配置、权限和 profile。
- TUI 会话初始化本身仍依赖现有 Hermes 会话创建流程；这次只改了提交消息时的显式 `@CODEX` 分流。

## 2026-04-20 - Codex adapter delivery validation and coverage follow-up

### 主要改动点
- 追加 Gateway 自动测试，覆盖 `_run_agent(..., codex_route=...)` 的直接分流分支，防止后续 runtime/proxy/cache 调整把显式 `@CODEX` 路由撞坏。
- 完成一次本机交付级验收：
  - 迁移 `~/.hermes/config.yaml` 到 v20
  - 打开 `codex_sdk.modes.computer_use.enabled`
  - 将 `~/.codex/auth.json` 的有效登录态同步到 Hermes 自身 auth store

### 影响的接口 / 配置
- 无新增接口。
- 本机用户配置现已包含有效的 `codex_sdk` 配置与启用状态。
- 备份文件已生成：`~/.hermes/config.yaml.bak-20260420-194034`

### 测试执行情况
- 新增测试覆盖：
  - Gateway `_run_agent` 的 `codex_route` 直接分流分支
- 本机真实验收通过：
  - `python cli.py -q '@CODEX explain the repository entrypoints in 3 bullets'`
  - `python cli.py -q '@CODEX:computer_use open Calculator'`
- `hermes doctor` 现已确认：
  - `OpenAI Codex auth (logged in)`
  - `Codex computer_use mode is enabled in config.`

### 已知限制 / 后续项
- `computer_use` 虽已在当前机器验通，但仍依赖宿主机 GUI 权限与 Codex 自身环境，换机器不保证自动可用。
- 这次同步的是本机真实配置与登录态，不会自动传播到其他 Hermes profile 或其他机器。

## 2026-04-20 - Expanded CLI / Gateway / TUI @CODEX end-to-end coverage

### 主要改动点
- 新增 CLI 高层测试，直接覆盖 `main(query='@CODEX ...', quiet=True)` 和 `main(query='@CODEX:computer_use ...', quiet=True)` 单次查询路径，验证会绕过普通 provider 初始化并直接调用 Codex runner。
- 扩展 TUI `prompt.submit` 测试，覆盖 `standard` 和 `computer_use` 两种 `@CODEX` 模式，并验证模式参数能正确传进 Codex 路由。
- 保留并扩展 Gateway 的显式 `codex_route` 分流测试，使三条入口链路现在都有高层验收覆盖。

### 影响的接口 / 配置
- 无新增接口或配置。
- 主要增强的是测试覆盖面，确保 `@CODEX` 路径在 CLI / Gateway / TUI 三条用户入口都可持续回归验证。

### 测试执行情况
- 通过：
  - `python -m pytest -n 4 tests/cli/test_codex_prefix.py tests/gateway/test_codex_sdk_route.py tests/tui_gateway/test_codex_prompt.py -q`
- 结果：
  - `6 passed`

### 已知限制 / 后续项
- 这批是高层离线验收测试，仍然使用 fake Codex runner，不替代真实账户与宿主机环境的本机 smoke test。
