# agy-cli 插件开发项目

本项目用于开发和测试 Antigravity (`agy`) 自定义插件，所有插件统一存放在 `plugins/` 目录下。

## 目录结构

```text
/work/Projects/agy-cli/
├── .agents/
│   └── plugins.json      # 将工作区 plugins/ 目录注册为插件发现路径
├── plugins/              # 自定义插件目录（每个插件一个子目录）
│   └── agy-helper/       # 会话内插件：提供 $proj 查看命令与项目路径一致性校验
├── scripts/              # 外部终端辅助脚本与 Shell 入口
│   ├── agy_helper.py     # 终端 CLI 包装（自动恢复会话、交互式 agy proj、agy proj new）
│   ├── .bash_agy         # Bash 函数入口，转发 agy 命令至 ~/bin/agy_helper.py
│   ├── statusline.sh     # TUI 状态栏脚本（安装至 ~/.gemini/antigravity-cli/statusline.sh）
│   └── keybindings.json  # TUI 快捷键配置（添加 Ctrl-n/Ctrl-p 列表上下移动）
├── mcp/                  # 可选 MCP 接入（如 Codex MCP 封装与接入文档，install.sh 默认不安装）
├── install.sh            # 一键安装/卸载脚本（终端脚本 + statusLine + 全局插件 + 可选关联同级 ../agy-telegram 服务）
└── README.md
```

## 一键安装（终端脚本 + 全局插件）

运行项目根目录的 `install.sh` 即可同时安装外部终端脚本（`~/bin/agy_helper.py`、`~/.bash_agy` 及 `~/.bashrc` 引用）、状态栏脚本（`~/.gemini/antigravity-cli/statusline.sh` 与 `~/.gemini/antigravity-cli/config.json` 中的 `statusLine` 配置）和全局 `agy-helper` 插件（若 `config.json` 已存在不同的 `statusLine` 配置，将拒绝安装以防覆盖）：

```bash
./install.sh            # 安装终端脚本与全局插件
./install.sh --dry-run  # 预览将执行的操作
./install.sh --uninstall # 卸载终端脚本与全局插件
```

## 插件开发与本地运行

1. 在 `plugins/<plugin_name>/` 下创建插件（至少包含 `plugin.json`，按需添加 `hooks.json`、`skills/`、`rules/`、`mcp_config.json`）。
2. 校验插件清单：
   ```bash
   agy plugin validate /work/Projects/agy-cli/plugins/<plugin_name>
   ```
3. 在本项目目录启动 `agy` 时，`.agents/plugins.json` 会自动加载 `plugins/` 下的插件。

## 全局安装、启停与卸载命令

> 注意：通过 `.agents/plugins.json` 加载的插件属于**工作区本地插件**，不能直接用 `/plugin uninstall` 卸载；如需跨项目全局使用，可单独安装到全局目录。

* **安装到全局**：
  ```bash
  agy plugin install /work/Projects/agy-cli/plugins/<plugin_name>
  ```
* **查看已安装插件**：
  ```bash
  agy plugin list
  ```
* **启用 / 禁用插件**（工作区插件与全局插件均适用）：
  ```bash
  agy plugin enable <plugin_name>
  agy plugin disable <plugin_name>
  ```
* **从全局卸载**：
  ```bash
  agy plugin uninstall <plugin_name>
  ```

## 已有插件

### `agy-helper`

通过 `PreInvocation` 钩子在本地执行（不经过模型）：

- `$proj [-a] [关键字]`：查看项目、绑定路径、CLI 工作区分组及关联会话（`-a`/`--all` 包含 0 steps 空会话）
- `$proj -h`：显示帮助信息
- **路径与项目一致性校验**：当工作目录位于 `plugins/agy-helper/config.json` 配置的 `projectRoots`（默认 `/work/Projects`、`/vault/Projects`）下时，发送消息前会校验当前目录最后一级名称与所选项目名是否一致、且是否处于项目根目录；不一致时直接拦截并报错提示。
- **版本校验**：`agy_helper.py` 启动前与 `statusline.sh` 状态栏会校验当前 `agy --version` 是否与 `config.json` 中的 `supportedAgyVersion` 一致；版本变更后仅作提醒（不阻断对话），方便直接让 AI 检查适配并更新配置。
- **只读数据层**：对 `agy` 配置与会话数据库的只读访问集中在 `plugins/agy-helper/scripts/agy_store.py`。
- **功能拆分**：`agy_helper_hook.py` 作为入口路由，`$proj` 树状查看在 `agy_show_projects.py`，路径一致性校验在 `agy_project_guard.py`。
