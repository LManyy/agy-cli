#!/usr/bin/env python3
"""`$proj` 命令解析与项目树状渲染模块。"""

import os
import shlex
from pathlib import Path

import agy_store

PREFIX = "$proj"


class Ansi:
    """可选的 ANSI 颜色包装器，插件默认启用以保持彩色树状输出。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def wrap(self, code: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\033[{code}m{text}\033[0m"

    def red(self, text: str) -> str:
        return self.wrap("31", text)

    def green(self, text: str) -> str:
        return self.wrap("32", text)

    def codex_gray(self, text: str) -> str:
        return self.wrap("38;5;246", text)

    def yellow(self, text: str) -> str:
        return self.wrap("33", text)

    def dim(self, text: str) -> str:
        return self.wrap("2;90", text)


def _format_conv_line(c: Ansi, conv: dict, prefix: str, branch: str) -> str:
    """格式化单条会话输出行。"""
    if conv["steps"] == 0:
        return c.dim(f"{prefix}{branch}{conv['title']}  {conv['time']}")
    return (
        f"{c.codex_gray(prefix + branch)}"
        f"{c.red(conv['title'])}  "
        f"{c.codex_gray(conv['time'])}"
    )


def _group_cli_convs_by_workspace(convs: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """将 CLI Project 下的会话按工作区目录分组，返回 [(组名, 目录路径, 会话列表)]。"""
    groups: dict[str, dict] = {}
    for conv in convs:
        ws_path = conv["workspaces"][0] if conv["workspaces"] else ""
        if ws_path:
            real_ws, ws_name = agy_store.resolve_canonical_path(ws_path)
            key = str(real_ws)
            label = ws_name or key
        else:
            key = ""
            label = "(未知工作区)"

        if key not in groups:
            groups[key] = {"label": label, "path": key, "convs": []}
        groups[key]["convs"].append(conv)

    home_str = str(agy_store.resolve_canonical_path(Path.home())[0])
    ordered = sorted(
        groups.values(),
        key=lambda g: (g["path"] != home_str, g["path"] == "", g["label"].lower()),
    )
    return [(g["label"], g["path"], g["convs"]) for g in ordered]


def _append_cli_project_lines(
    lines: list[str],
    c: Ansi,
    convs: list[dict],
    show_all: bool,
) -> None:
    """生成 CLI Project 下按工作区目录分组的二级树状结构。"""
    groups = _group_cli_convs_by_workspace(convs)
    visible_groups: list[tuple[str, str, list[dict]]] = []
    for label, path, g_convs in groups:
        filtered = g_convs if show_all else [item for item in g_convs if item["steps"] > 0]
        if filtered:
            visible_groups.append((label, path, filtered))

    if not visible_groups:
        lines.append(c.dim("└── (无有效会话)"))
        return

    for g_idx, (label, path, g_convs) in enumerate(visible_groups):
        is_last_group = g_idx == len(visible_groups) - 1
        g_branch = "└── " if is_last_group else "├── "
        child_prefix = "    " if is_last_group else "│   "

        exists = bool(path) and os.path.isdir(path)
        missing_part = f" {c.yellow('[不存在]')}" if (path and not exists) else ""
        path_part = f" {c.green(f'({path})')}" if path else ""
        lines.append(f"{c.codex_gray(f'{g_branch}{label}')}{path_part}{missing_part}")

        for c_idx, conv in enumerate(g_convs):
            c_branch = "└── " if c_idx == len(g_convs) - 1 else "├── "
            lines.append(_format_conv_line(c, conv, child_prefix, c_branch))


def render_projects_tree(
    keyword: str = "",
    show_all: bool = False,
    force_color: bool = True,
) -> str:
    """渲染项目、工作区分组与会话树状列表，返回完整输出文本。"""
    c = Ansi(enabled=force_color)
    warnings: list[str] = []
    projects = agy_store.load_projects(warnings)
    convs_by_proj, db_err = agy_store.load_conversations(max_title_len=48)

    if not projects and not convs_by_proj:
        out = warnings + ["未找到任何项目配置或会话记录。"]
        return "\n".join(out)

    ordered_pids = sorted(
        projects.keys(),
        key=lambda k: (k == agy_store.DEFAULT_PROJECT_ID, projects[k]["name"].lower()),
    )
    orphan_pids = sorted(pid for pid in convs_by_proj if pid not in projects)
    all_pids = ordered_pids + orphan_pids

    if keyword:
        kw = keyword.lower()
        all_pids = [
            pid
            for pid in all_pids
            if kw in pid.lower()
            or (pid in projects and kw in projects[pid]["name"].lower())
        ]
        if not all_pids:
            return f"未找到匹配 '{keyword}' 的项目。"

    lines: list[str] = []
    if warnings:
        lines.extend(warnings)
        lines.append("")
    if db_err:
        lines.append(c.yellow(f"[提示] {db_err}"))
        lines.append("")

    for idx, pid in enumerate(all_pids):
        if idx > 0:
            lines.append("")

        all_convs = convs_by_proj.get(pid, [])
        valid_convs = [item for item in all_convs if item["steps"] > 0]

        if pid in projects:
            proj = projects[pid]
            lines.append(f"{c.codex_gray(proj['name'])}  {c.dim(proj['id'])}")
            path_items = proj["paths"] or ["(无绑定路径)"]
        else:
            label = pid or "(空项目ID)"
            lines.append(f"{c.yellow('(未找到配置的项目)')}  {c.dim(label)}")
            path_items = []

        if pid == agy_store.DEFAULT_PROJECT_ID:
            _append_cli_project_lines(lines, c, all_convs, show_all=show_all)
            continue

        visible_convs = all_convs if show_all else valid_convs
        nodes = [("path", p) for p in path_items] + [("conv", item) for item in visible_convs]
        if not nodes:
            lines.append(c.codex_gray("└── (无内容)"))
            continue

        for n_idx, (kind, payload) in enumerate(nodes):
            branch = "└── " if n_idx == len(nodes) - 1 else "├── "
            if kind == "path":
                if payload == "(无绑定路径)":
                    lines.append(c.dim(f"{branch}{payload}"))
                else:
                    real_path, _ = agy_store.resolve_canonical_path(payload)
                    exists = os.path.isdir(str(real_path))
                    missing_part = "" if exists else f" {c.yellow('[不存在]')}"
                    lines.append(f"{c.codex_gray(branch)}{c.green(payload)}{missing_part}")
            else:
                lines.append(_format_conv_line(c, payload, "", branch))

    return "\n".join(lines)


def format_help() -> str:
    return "\n".join(
        [
            "agy-helper 插件用法:",
            f"  {PREFIX} [选项] [关键字]        以树状结构查看本地项目、工作区与会话",
            "",
            "选项:",
            "  -a, --all                 显示所有会话（包含 0 steps 的空会话）",
            "  --list, --no-interactive  仅输出项目树状列表（默认）",
            "  --color                   输出 ANSI 颜色码（默认）",
            "  --no-color                禁用颜色输出",
            "  -h, --help                显示此帮助信息",
        ]
    )


def handle_proj_args(args: list[str]) -> str:
    """解析并执行 `$proj` 命令，返回展示给用户的文本。"""
    if args and args[0] == "new":
        return "提示: `$proj new` 需要交互式终端启动原生 `agy --new-project`，请在外部终端中使用 `agy proj new`。"

    show_all = False
    force_color = True
    keywords: list[str] = []

    for arg in args:
        if arg in ("-h", "--help", "help") and not keywords and not show_all:
            return format_help()
        if arg in ("-h", "--help"):
            return format_help()
        if arg in ("-a", "--all"):
            show_all = True
        elif arg in ("--list", "--no-interactive"):
            continue
        elif arg == "--color":
            force_color = True
        elif arg == "--no-color":
            force_color = False
        elif arg.startswith("-"):
            return f"错误: 未知选项 `{arg}`。\n\n{format_help()}"
        else:
            keywords.append(arg)

    if len(keywords) > 1:
        return f"错误: `{PREFIX}` 最多接受一个过滤关键字。\n\n{format_help()}"

    keyword = keywords[0] if keywords else ""
    return render_projects_tree(keyword=keyword, show_all=show_all, force_color=force_color)


def parse_trigger(user_input: str) -> list[str] | None:
    """若用户输入以 `$proj` 开头（完整命令词匹配），返回参数列表；否则返回 None。"""
    text = user_input.strip()
    if text != PREFIX and not (text.startswith(PREFIX) and text[len(PREFIX) : len(PREFIX) + 1].isspace()):
        return None
    rest = text[len(PREFIX) :].strip()
    if not rest:
        return []
    try:
        return shlex.split(rest)
    except ValueError as exc:
        raise ValueError(f"参数解析失败: {exc}") from exc
