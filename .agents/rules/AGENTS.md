# agy-cli 项目开发规范

- 当修改了 `scripts/`（`agy_helper.py`、`.bash_agy`）或 `plugins/agy-helper/` 下的代码后，在完成校验与测试后，应自动执行 `./install.sh` 将变更同步到部署目录（`~/bin/agy_helper.py`、`~/.bash_agy` 及全局 `agy-helper` 插件目录）。
- 若沙箱权限限制导致无法写入 `~/` 或 `~/.gemini/`，需明确告知用户或请求授权后再同步，不要静默跳过。
- 插件内对 `agy` 本地配置与会话数据库仅限只读访问，禁止写入操作。
- Telegram Bot 桥接项目已独立位于同级目录 `/work/Projects/agy-telegram`，本仓库仅在 `install.sh --with-telegram` 与 `README.md` 中保留路径关联。
- 仅当明确需要通过 MCP（`codex_run` / `codex_reply`）调用 `codex` 时，先按需阅读 [CODEX_AGENTS.md](../../mcp/CODEX_AGENTS.md) 并遵循其中的派单与独立验收规范。
