# Codex MCP 接入说明

`mcp/` 目录提供将本机 `codex` CLI 作为 MCP 工具接入 `agy` 的方案。**根目录 `install.sh` 默认不安装此部分**，需按需使用本目录下的独立脚本安装或手动配置。

前置条件：本机已安装官方 `codex` CLI 并完成登录认证（`codex --version` 可用）。

---

## 一键安装 / 卸载（带旁路日志版）

```bash
./mcp/install_codex_mcp.sh             # 注册 codex_mcp.py 到 ~/.gemini/config/mcp_config.json
./mcp/install_codex_mcp.sh --dry-run   # 预览并检查配置冲突
./mcp/install_codex_mcp.sh --uninstall # 卸载本脚本写入的 codex 配置
```

> 若 `~/.gemini/config/mcp_config.json` 中已存在不同的 `codex` 配置，脚本会拒绝覆盖。

协同规范参见 [CODEX_AGENTS.md](./CODEX_AGENTS.md)（约定 `agy` 调度 `codex` 的高密度派单格式、独立验收原则与越界审批回传机制）。

---

## 会话物理隔离与目录约束

- **独立 `CODEX_HOME`**：`codex_mcp.py` 默认将子进程 `CODEX_HOME` 设为 `~/.codex-mcp`（可通过 `CODEX_MCP_HOME` 自定义），自动软链接 `~/.codex/` 的 `auth.json`、`config.toml` 共享登录态，而 `sessions/` 完全独立，避免污染你日常手动使用的 `codex` 会话列表。
- **目录约束**：仅允许在 `plugins/agy-helper/config.json` 配置的 `projectRoots` 下运行（拒绝 `~`、`/tmp`、`Projects` 根目录及已绑定项目的子目录）。

---

## 多会话旁路日志隔离机制

`codex_mcp.py` 会在 `/tmp/agy-codex-mcp/`（可通过环境变量 `CODEX_MCP_LOG_DIR` 自定义）下为**每个 `codex` 会话**生成独立的日志文件：

- 新会话命名格式：`<项目名>_<会话名>_<session_id>.log`（如 `agy-cli_auth-fix_sess-123.log`；未指定 `name` 时为 `<项目名>_<session_id>.log`）
- 同一会话调用 `codex_reply` 多轮返工时，会**追加写入同一个会话日志文件**，不同并发会话互不干扰且不会互相阻塞（默认最多 4 并发，可通过 `CODEX_MCP_MAX_WORKERS` 调整）。
- 同时维护软链接 `/tmp/agy-codex-mcp/latest.log` 指向最近一次会话日志，且每次工具返回文本末尾会附带本次日志完整路径：
  ```bash
  tail -f /tmp/agy-codex-mcp/latest.log
  ```

---

## 极简替代：直接接入官方 `codex mcp-server`（无旁路日志）

若不需要旁路日志与 `projectRoots` 目录保护，可直接手动配置官方内置服务：

```json
{
  "mcpServers": {
    "codex": {
      "command": "codex",
      "args": ["mcp-server"]
    }
  }
}
```
