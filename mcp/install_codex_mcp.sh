#!/usr/bin/env bash
set -euo pipefail

# Codex MCP 独立安装脚本（默认不随根目录 install.sh 自动安装）
# 将 mcp/codex_mcp.py 注册到 ~/.gemini/config/mcp_config.json（带冲突检查）

MCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SCRIPT="$MCP_DIR/codex_mcp.py"
MCP_CONFIG="${HOME}/.gemini/config/mcp_config.json"
SERVER_NAME="codex"

DRY_RUN=0
UNINSTALL=0

usage() {
  cat <<EOF
用法: $(basename "$0") [选项]

选项:
  --uninstall   从 $MCP_CONFIG 移除 '${SERVER_NAME}' MCP 配置
  --dry-run     仅预览将执行的操作（仍执行配置冲突检查）
  -h, --help    显示此帮助信息
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "错误: 未知参数 '$1'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

sync_mcp_config() {
  local mode="$1" # check | apply | remove
  PYTHONDONTWRITEBYTECODE=1 python3 - "$MCP_CONFIG" "$SERVER_NAME" "$SERVER_SCRIPT" "$mode" <<'PYEOF'
import json
import os
import sys

config_path, server_name, script_path, mode = sys.argv[1:5]
expected = {
    "command": "python3",
    "args": ["-B", script_path],
}

data = {}
exists = os.path.exists(config_path)
if exists:
    try:
        with open(config_path, "r", encoding="utf-8") as fp:
            raw = fp.read().strip()
        data = json.loads(raw) if raw else {}
    except Exception as exc:
        print(f"错误: 读取 {config_path} 失败: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"错误: {config_path} 根节点必须是 JSON 对象。", file=sys.stderr)
        sys.exit(1)

servers = data.get("mcpServers")
if servers is not None and not isinstance(servers, dict):
    print(f"错误: {config_path} 中的 mcpServers 必须是 JSON 对象。", file=sys.stderr)
    sys.exit(1)
servers = servers or {}

if mode == "remove":
    if not exists or server_name not in servers:
        print(f"未发现 MCP '{server_name}' 配置: {config_path}")
        sys.exit(0)
    if servers[server_name] == expected:
        del servers[server_name]
        data["mcpServers"] = servers
        with open(config_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        print(f"已从 {config_path} 移除 MCP '{server_name}'")
    else:
        print(f"跳过: {config_path} 中的 '{server_name}' 配置非本脚本写入，已保留。")
    sys.exit(0)

if server_name in servers:
    current = servers[server_name]
    if current == expected:
        print(f"已存在相同的 MCP '{server_name}' 配置，跳过修改 {config_path}")
        sys.exit(0)
    print(f"错误: {config_path} 已存在不同的 '{server_name}' 配置，拒绝继续。", file=sys.stderr)
    print("当前配置:", json.dumps(current, ensure_ascii=False), file=sys.stderr)
    print("期望配置:", json.dumps(expected, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)

if mode == "check":
    print(f"[dry-run] 将注册 MCP '{server_name}' 到 {config_path}")
    sys.exit(0)

os.makedirs(os.path.dirname(config_path), exist_ok=True)
servers[server_name] = expected
data["mcpServers"] = servers
with open(config_path, "w", encoding="utf-8") as fp:
    json.dump(data, fp, ensure_ascii=False, indent=2)
    fp.write("\n")
print(f"已注册 MCP '{server_name}': {config_path}")
PYEOF
}

if [[ "$UNINSTALL" -eq 1 ]]; then
  echo "==> 卸载 Codex MCP"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] 检查并移除 $MCP_CONFIG 中的 '$SERVER_NAME'"
  else
    sync_mcp_config remove
  fi
  exit 0
fi

if [[ ! -f "$SERVER_SCRIPT" ]]; then
  echo "错误: 未找到 $SERVER_SCRIPT" >&2
  exit 1
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "警告: 当前 PATH 中未检测到 codex CLI，安装配置后需先安装并登录 codex 方可使用。" >&2
fi

echo "==> 1. 检查 MCP 配置 ($MCP_CONFIG)"
sync_mcp_config check

if [[ "$DRY_RUN" -eq 0 ]]; then
  chmod +x "$SERVER_SCRIPT"
  echo "==> 2. 写入 MCP 配置"
  sync_mcp_config apply
  echo
  echo "安装完成！重启 agy 会话后即可使用 /mcp 查看 'codex'。"
  echo "旁路实时日志目录: /tmp/agy-codex-mcp/ (最新会话软链接: /tmp/agy-codex-mcp/latest.log)"
fi
