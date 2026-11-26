#!/usr/bin/env bash
set -euo pipefail

# agy-cli 统一安装脚本：
# 1. 安装 scripts/agy_helper.py -> ~/bin/agy_helper.py
# 2. 安装 scripts/.bash_agy -> ~/.bash_agy，并在 ~/.bashrc 中启用
# 3. 安装 scripts/statusline.sh -> ~/.gemini/antigravity-cli/statusline.sh 并配置 statusLine
# 4. 安装 plugins/agy-helper 到全局 agy 插件目录（供会话内 $proj 命令使用）

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_SRC="$REPO_DIR/scripts/agy_helper.py"
BASH_RC_SRC="$REPO_DIR/scripts/.bash_agy"
STATUSLINE_SRC="$REPO_DIR/scripts/statusline.sh"
KEYBINDINGS_SRC="$REPO_DIR/scripts/keybindings.json"
PLUGIN_SRC="$REPO_DIR/plugins/agy-helper"

BIN_TARGET="${HOME}/bin/agy_helper.py"
BASH_AGY_TARGET="${HOME}/.bash_agy"
BASHRC_FILE="${HOME}/.bashrc"
SOURCE_LINE='[ -f ~/.bash_agy ] && source ~/.bash_agy'
AGY_CLI_DIR="${HOME}/.gemini/antigravity-cli"
STATUSLINE_TARGET="${AGY_CLI_DIR}/statusline.sh"
AGY_CLI_CONFIG="${AGY_CLI_DIR}/config.json"
KEYBINDINGS_TARGET="${AGY_CLI_DIR}/keybindings.json"

DRY_RUN=0
UNINSTALL=0
WITH_TELEGRAM=0
TG_BOT_DIR="${AGY_TELEGRAM_DIR:-$(dirname "$REPO_DIR")/agy-telegram}"
TG_SERVICE_SRC="$TG_BOT_DIR/deploy/agy-telegram.service"
TG_SERVICE_TARGET="${HOME}/.config/systemd/user/agy-telegram.service"
TG_CONFIG_TARGET="${HOME}/.config/agy-telegram/config.json"

usage() {
  cat <<EOF
用法: $(basename "$0") [选项]

选项:
  --with-telegram  同时安装/更新 systemd --user 服务 (agy-telegram.service)
  --uninstall      卸载 ~/bin/agy_helper.py、~/.bash_agy、statusline.sh、全局插件及 telegram 服务
  --dry-run        仅打印将执行的操作（仍执行配置冲突检查），不实际修改文件或配置
  -h, --help       显示此帮助信息
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-telegram)
      WITH_TELEGRAM=1
      shift
      ;;
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

run_cmd() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

sync_statusline_config() {
  local mode="$1" # check | apply | remove
  PYTHONDONTWRITEBYTECODE=1 python3 - "$AGY_CLI_CONFIG" "$STATUSLINE_TARGET" "$mode" <<'PYEOF'
import json
import os
import sys

config_path, cmd_path, mode = sys.argv[1:4]
expected = {
    "type": "command",
    "command": cmd_path,
    "enabled": True,
    "stack_with_default": True,
}

data = {}
exists = os.path.exists(config_path)
if exists:
    try:
        with open(config_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as exc:
        print(f"错误: 读取 {config_path} 失败: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"错误: {config_path} 根节点必须是 JSON 对象。", file=sys.stderr)
        sys.exit(1)

if mode == "remove":
    if not exists or "statusLine" not in data:
        print(f"未发现 statusLine 配置: {config_path}")
        sys.exit(0)
    if data["statusLine"] == expected:
        del data["statusLine"]
        with open(config_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        print(f"已从 {config_path} 移除 statusLine 配置")
    else:
        print(f"跳过: {config_path} 中的 statusLine 配置非本脚本写入，已保留。")
    sys.exit(0)

if "statusLine" in data:
    current = data["statusLine"]
    if current == expected:
        print(f"已存在相同的 statusLine 配置，跳过修改 {config_path}")
        sys.exit(0)
    print(f"错误: {config_path} 已存在不同的 statusLine 配置，拒绝继续。", file=sys.stderr)
    print("当前配置:", json.dumps(current, ensure_ascii=False), file=sys.stderr)
    print("期望配置:", json.dumps(expected, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)

if mode == "check":
    print(f"[dry-run] 将写入 statusLine 配置到 {config_path}")
    sys.exit(0)

os.makedirs(os.path.dirname(config_path), exist_ok=True)
data["statusLine"] = expected
with open(config_path, "w", encoding="utf-8") as fp:
    json.dump(data, fp, ensure_ascii=False, indent=2)
    fp.write("\n")
print(f"已写入 statusLine 配置: {config_path}")
PYEOF
}

sync_keybindings_config() {
  local mode="$1" # check | apply | remove
  PYTHONDONTWRITEBYTECODE=1 python3 - "$KEYBINDINGS_SRC" "$KEYBINDINGS_TARGET" "$mode" <<'PYEOF'
import json
import os
import sys

src_path, target_path, mode = sys.argv[1:4]
with open(src_path, "r", encoding="utf-8") as fp:
    desired = json.load(fp)

exists = os.path.exists(target_path)
current = {}
if exists:
    try:
        with open(target_path, "r", encoding="utf-8") as fp:
            current = json.load(fp)
    except Exception as exc:
        print(f"错误: 读取 {target_path} 失败: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(current, dict):
        print(f"错误: {target_path} 根节点必须是 JSON 对象。", file=sys.stderr)
        sys.exit(1)

if mode == "remove":
    if not exists:
        print(f"未发现: {target_path}")
        sys.exit(0)
    changed = False
    for k, v in desired.items():
        if current.get(k) == v:
            del current[k]
            changed = True
    if changed:
        with open(target_path, "w", encoding="utf-8") as fp:
            json.dump(current, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        print(f"已从 {target_path} 清理本项目写入的快捷键字段")
    else:
        print(f"跳过: {target_path} 未包含需清理的字段")
    sys.exit(0)

need_update = any(current.get(k) != v for k, v in desired.items()) or "cli.exit" in current and current.get("cli.exit") != ["ctrl+d"]
if not exists:
    if mode == "check":
        print(f"[dry-run] 将写入 keybindings 配置到 {target_path}")
        sys.exit(0)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as fp:
        json.dump(desired, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    print(f"已写入 keybindings 配置: {target_path}")
    sys.exit(0)

if not need_update:
    print(f"keybindings 配置已是最新，跳过修改 {target_path}")
    sys.exit(0)

if mode == "check":
    print(f"[dry-run] 将合并更新 keybindings 字段到 {target_path}")
    sys.exit(0)

merged = dict(current)
merged.update(desired)
if "cli.exit" in merged and merged["cli.exit"] != ["ctrl+d"]:
    merged["cli.exit"] = ["ctrl+d"]
with open(target_path, "w", encoding="utf-8") as fp:
    json.dump(merged, fp, ensure_ascii=False, indent=2)
    fp.write("\n")
print(f"已合并更新 keybindings 配置: {target_path}")
PYEOF
}

if [[ "$UNINSTALL" -eq 1 ]]; then
  echo "==> 卸载 agy-cli"
  for target in "$BIN_TARGET" "$BASH_AGY_TARGET" "$STATUSLINE_TARGET"; do
    if [[ -e "$target" || -L "$target" ]]; then
      run_cmd rm -f "$target"
      echo "已移除: $target"
    else
      echo "未发现: $target"
    fi
  done

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] 检查并清理 $AGY_CLI_CONFIG 中的 statusLine"
    echo "[dry-run] 检查并清理 $KEYBINDINGS_TARGET"
  else
    sync_statusline_config remove
    sync_keybindings_config remove
  fi

  if command -v agy >/dev/null 2>&1; then
    run_cmd agy plugin uninstall agy-helper || true
  else
    echo "未找到 agy 命令，跳过插件卸载。"
  fi
  if [[ -e "$TG_SERVICE_TARGET" || -L "$TG_SERVICE_TARGET" ]]; then
    if command -v systemctl >/dev/null 2>&1 && [[ "$DRY_RUN" -eq 0 ]]; then
      systemctl --user disable --now agy-telegram.service >/dev/null 2>&1 || true
    fi
    run_cmd rm -f "$TG_SERVICE_TARGET"
    echo "已移除: $TG_SERVICE_TARGET"
  fi
  echo "提示: 如需清理 ~/.bashrc 中的 source 行，请手动检查 $BASHRC_FILE。"
  exit 0
fi

for f in "$SCRIPT_SRC" "$BASH_RC_SRC" "$STATUSLINE_SRC" "$KEYBINDINGS_SRC"; do
  if [[ ! -f "$f" ]]; then
    echo "错误: 未找到文件 $f" >&2
    exit 1
  fi
done

if [[ ! -d "$PLUGIN_SRC" ]]; then
  echo "错误: 未找到插件目录 $PLUGIN_SRC" >&2
  exit 1
fi

echo "==> 1. 检查 statusLine 与 keybindings 配置"
sync_statusline_config check
sync_keybindings_config check

echo "==> 2. 安装终端脚本与 Shell 入口"
run_cmd install -D -m 0755 -- "$SCRIPT_SRC" "$BIN_TARGET"
echo "已安装: $BIN_TARGET"
run_cmd install -D -m 0644 -- "$BASH_RC_SRC" "$BASH_AGY_TARGET"
echo "已安装: $BASH_AGY_TARGET"

echo "==> 3. 检查 ~/.bashrc 配置"
if [[ -f "$BASHRC_FILE" ]] && grep -Fq ".bash_agy" "$BASHRC_FILE"; then
  echo "已存在 .bash_agy 引用，跳过修改 $BASHRC_FILE"
else
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] 追加到 $BASHRC_FILE: $SOURCE_LINE"
  else
    touch "$BASHRC_FILE"
    printf '\n%s\n' "$SOURCE_LINE" >> "$BASHRC_FILE"
    echo "已追加到 $BASHRC_FILE: $SOURCE_LINE"
  fi
fi

echo "==> 4. 安装 statusline.sh、keybindings.json 与配置"
run_cmd install -D -m 0755 -- "$STATUSLINE_SRC" "$STATUSLINE_TARGET"
echo "已安装: $STATUSLINE_TARGET"
if [[ "$DRY_RUN" -eq 0 ]]; then
  sync_statusline_config apply
  sync_keybindings_config apply
fi

echo "==> 5. 安装 agy-helper 插件到全局"
if command -v agy >/dev/null 2>&1; then
  run_cmd agy plugin validate "$PLUGIN_SRC"
  run_cmd agy plugin install "$PLUGIN_SRC"
else
  echo "警告: 未在 PATH 中找到 agy 可执行文件，跳过插件安装。" >&2
  echo "请稍后手动执行: agy plugin install \"$PLUGIN_SRC\"" >&2
fi

if [[ "$WITH_TELEGRAM" -eq 1 ]]; then
  echo "==> 6. 安装 agy-telegram systemd --user 服务"
  if [[ ! -f "$TG_SERVICE_SRC" ]]; then
    echo "错误: 未找到服务模板 $TG_SERVICE_SRC" >&2
    exit 1
  fi
  py_bin="$(command -v python3 || echo /usr/bin/python3)"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] 渲染并安装服务模板 $TG_SERVICE_SRC -> $TG_SERVICE_TARGET (BOT_DIR=$TG_BOT_DIR, PYTHON=$py_bin)"
  else
    mkdir -p "$(dirname "$TG_SERVICE_TARGET")"
    PYTHONDONTWRITEBYTECODE=1 python3 - "$TG_SERVICE_SRC" "$TG_SERVICE_TARGET" "$TG_BOT_DIR" "$py_bin" <<'PYEOF'
import sys
from pathlib import Path
src, dst, bot_dir, py_bin = sys.argv[1:5]
content = Path(src).read_text(encoding="utf-8")
content = content.replace("__BOT_DIR__", bot_dir).replace("__REPO_DIR__", bot_dir).replace("__PYTHON_BIN__", py_bin)
Path(dst).write_text(content, encoding="utf-8")
PYEOF
    echo "已安装服务文件: $TG_SERVICE_TARGET"
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
  fi
  if [[ ! -f "$TG_CONFIG_TARGET" ]]; then
    echo "提示: 尚未检测到配置 $TG_CONFIG_TARGET，请先从 $TG_BOT_DIR/config.example.json 复制并填写 botToken。"
  else
    echo "提示: 可执行 'systemctl --user enable --now agy-telegram.service' 启动常驻服务。"
  fi
fi

echo
echo "安装完成！"
echo "- 终端使用: 执行 'source ~/.bashrc' 后即可使用 agy / agy proj / agy proj new"
echo "- 会话内使用: 重启 agy 会话后，直接输入 \$proj [-a] [关键字]"
