#!/usr/bin/env python3
"""Antigravity CLI (agy) 辅助工具。

功能:
  1. `agy`               无参数启动时，按当前真实路径自动匹配项目并恢复上次会话。
  2. `agy proj [-a] [关键词]` 以彩色树状结构查看项目、路径、CLI 工作区分组及关联会话。
  3. `agy proj new`      在当前真实路径下调用原生 `agy --new-project` 创建项目（拒绝在 ~ 创建）。
  4. 其余参数原样透传给原生 agy 可执行文件。
"""

import sys

# 避免在用户目录或脚本目录产生 __pycache__
sys.dont_write_bytecode = True

import glob
import json
import os
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import termios
import tty
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

PROJECTS_DIR = Path.home() / ".gemini" / "config" / "projects"
DB_PATH = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
DEFAULT_PROJECT_ID = "default-cli-project"
DEFAULT_PROJECT_ROOTS = ["/work/Projects", "/vault/Projects"]


class Ansi:
    """终端 ANSI 颜色包装，非 TTY 或设置 NO_COLOR 时自动禁用。"""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def wrap(self, code: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\033[{code}m{text}\033[0m"

    def red(self, text: str) -> str:
        # Codex 风格的柔和红色，用于会话标题
        return self.wrap("31", text)

    def green(self, text: str) -> str:
        # Codex 风格的柔和绿色，用于项目路径
        return self.wrap("32", text)

    def codex_gray(self, text: str) -> str:
        # Codex 风格的柔和中灰，用于项目名、路径与时间
        return self.wrap("38;5;246", text)

    def yellow(self, text: str) -> str:
        return self.wrap("33", text)

    def dim(self, text: str) -> str:
        return self.wrap("2;90", text)


def _is_inside_agy_shell() -> bool:
    """检测当前进程是否由 agy（如 TUI 内的 `!` 子命令）拉起。"""
    if os.environ.get("ANTIGRAVITY_LS_VERSION") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID"):
        return True
    pid = os.getpid()
    for _ in range(6):
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="ignore") as fp:
                stat_text = fp.read()
            # /proc/<pid>/stat 格式: pid (comm) state ppid ...
            rpar = stat_text.rfind(")")
            lpar = stat_text.find("(")
            if lpar == -1 or rpar == -1:
                break
            comm = stat_text[lpar + 1 : rpar]
            if comm == "agy" or comm.startswith("agy."):
                return True
            rest = stat_text[rpar + 2 :].split()
            if len(rest) < 2:
                break
            ppid = int(rest[1])
            if ppid <= 1 or ppid == pid:
                break
            pid = ppid
        except Exception:
            break
    return False


def make_ansi(force_color: bool | None = None) -> Ansi:
    """根据参数、环境变量与 TTY 状态构造颜色器。"""
    if force_color is not None:
        return Ansi(force_color)
    if os.environ.get("NO_COLOR") is not None:
        return Ansi(False)
    if (
        os.environ.get("FORCE_COLOR", "0") not in ("", "0")
        or os.environ.get("CLICOLOR_FORCE", "0") not in ("", "0")
        or _is_inside_agy_shell()
    ):
        return Ansi(True)
    return Ansi(sys.stdout.isatty())


def resolve_canonical_path(raw_path: str | Path | None = None) -> tuple[Path, str]:
    """解析并还原软链接，返回 (真实物理路径, 提取的默认项目名)。

    用于统一处理从 ~/Projects/... 等软链接路径进入的场景，确保打开与新建项目
    都绑定到同一个底层物理路径。
    """
    target = Path.cwd() if raw_path is None else Path(raw_path)
    real_path = Path(os.path.realpath(target))
    return real_path, real_path.name


def parse_file_uri(uri: str) -> str:
    """将 file:// URI 解码为本地路径，非 file URI 原样返回。"""
    if not uri.startswith("file://"):
        return uri
    return unquote(urlparse(uri).path)


def open_readonly_db(db_path: Path) -> sqlite3.Connection:
    """以只读 URI 模式打开 SQLite，避免与运行中的 agy 或桌面端发生写锁冲突。"""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def load_projects() -> dict[str, dict]:
    """读取所有本地项目配置。"""
    projects: dict[str, dict] = {}
    if not PROJECTS_DIR.is_dir():
        return projects

    for file_path in sorted(glob.glob(str(PROJECTS_DIR / "*.json"))):
        try:
            with open(file_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception as exc:
            print(f"[警告] 读取项目配置失败: {file_path} ({exc})", file=sys.stderr)
            continue

        pid = data.get("id") or Path(file_path).stem
        name = data.get("name") or pid
        resources = (data.get("projectResources") or {}).get("resources") or []
        paths = [parse_file_uri(r.get("folderUri", "")) for r in resources if r.get("folderUri")]
        projects[pid] = {"id": pid, "name": name, "paths": paths}

    return projects


def _read_varint(buf: bytes, idx: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while idx < len(buf):
        b = buf[idx]
        idx += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, idx
        shift += 7
    return val, idx


def extract_proto_title(raw: bytes | None) -> str:
    """从 raw_summary protobuf 二进制中提取 field 1（会话标题/首句摘要）。"""
    if not raw:
        return ""
    idx = 0
    length = len(raw)
    while idx < length:
        tag, idx = _read_varint(raw, idx)
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            _, idx = _read_varint(raw, idx)
        elif wire == 2:
            ln, idx = _read_varint(raw, idx)
            data = raw[idx : idx + ln]
            idx += ln
            if field == 1:
                return data.decode("utf-8", errors="replace").strip()
        elif wire == 1:
            idx += 8
        elif wire == 5:
            idx += 4
        else:
            break
    return ""


def format_local_time(ts_str: str) -> str:
    """将数据库 UTC/RFC3339Nano 时间转换为本地时间字符串。"""
    if not ts_str or ts_str.startswith("0001-01-01"):
        return "未知时间"
    try:
        s = ts_str.replace("Z", "+00:00")
        if "." in s:
            head, tail = s.split(".", 1)
            tz_pos = next((i for i, c in enumerate(tail) if c in "+-"), len(tail))
            frac = tail[:tz_pos][:6].ljust(6, "0")
            s = f"{head}.{frac}{tail[tz_pos:]}"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts_str[:16]


def load_conversations(max_title_len: int = 48) -> tuple[dict[str, list[dict]], str | None]:
    """按 project_id 分组读取所有顶层会话。"""
    grouped: dict[str, list[dict]] = {}
    if not DB_PATH.is_file():
        return grouped, "未找到会话数据库"

    try:
        with closing(open_readonly_db(DB_PATH)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT conversation_id, title, step_count, project_id,
                       workspace_uris, last_modified_time, raw_summary
                FROM conversation_summaries
                WHERE parent_conversation_id = ''
                ORDER BY last_modified_time DESC
                """
            )
            for cid, title, steps, pid, ws_json, mtime, raw in cur.fetchall():
                display_title = (title or "").strip() or extract_proto_title(raw) or "(无标题)"
                display_title = " ".join(display_title.splitlines())
                if len(display_title) > max_title_len:
                    display_title = display_title[: max_title_len - 3] + "..."

                workspaces = []
                try:
                    for uri in json.loads(ws_json or "[]"):
                        workspaces.append(parse_file_uri(uri))
                except Exception:
                    pass

                grouped.setdefault(pid or "", []).append(
                    {
                        "id": cid,
                        "title": display_title,
                        "steps": int(steps or 0),
                        "time": format_local_time(mtime or ""),
                        "workspaces": workspaces,
                    }
                )
        return grouped, None
    except Exception as exc:
        return grouped, f"读取会话数据库失败: {exc}"


def match_project_for_cwd(projects: dict[str, dict]) -> tuple[str, bool]:
    """先还原软链接得到真实路径，再按最长路径前缀匹配所属项目。"""
    real_cwd, _ = resolve_canonical_path()
    cwd_str = str(real_cwd)
    best_id = ""
    best_len = -1

    for pid, proj in projects.items():
        for raw_path in proj["paths"]:
            if not raw_path:
                continue
            folder_path, _ = resolve_canonical_path(raw_path)
            folder = str(folder_path)
            if (cwd_str == folder or cwd_str.startswith(folder + os.sep)) and len(folder) > best_len:
                best_len = len(folder)
                best_id = pid

    if not best_id:
        return "", False

    has_conv = True
    if DB_PATH.is_file():
        try:
            with closing(open_readonly_db(DB_PATH)) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT 1 FROM conversation_summaries "
                    "WHERE project_id = ? AND parent_conversation_id = '' AND step_count > 0 "
                    "LIMIT 1",
                    (best_id,),
                )
                has_conv = cur.fetchone() is not None
        except Exception:
            pass

    return best_id, has_conv


def load_helper_config() -> dict:
    """加载插件配置（projectRoots 与 supportedAgyVersion）。"""
    candidates = [
        Path.home() / ".config" / "agy-helper" / "config.json",
        Path(__file__).resolve().parent.parent / "plugins" / "agy-helper" / "config.json",
        Path.home() / ".gemini" / "config" / "plugins" / "agy-helper" / "config.json",
    ]
    for cfg_path in candidates:
        if cfg_path.is_file():
            try:
                with open(cfg_path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {"projectRoots": list(DEFAULT_PROJECT_ROOTS)}


def load_project_roots() -> list[str]:
    """加载受控的 Projects 根目录列表。"""
    raw_roots = load_helper_config().get("projectRoots")
    if isinstance(raw_roots, list):
        return [str(x) for x in raw_roots if x]
    return list(DEFAULT_PROJECT_ROOTS)


def check_agy_version_notice() -> None:
    """在启动 agy 前检查版本是否已适配；若不一致仅打印提醒，不阻断启动。"""
    expected = str(load_helper_config().get("supportedAgyVersion") or "").strip()
    if not expected:
        return
    agy_bin = os.environ.get("AGY_BIN", "").strip()
    if not agy_bin or not os.path.isfile(agy_bin) or not os.access(agy_bin, os.X_OK):
        agy_bin = shutil.which("agy") or ""
    if not agy_bin:
        return
    current = ""
    try:
        res = subprocess.run(
            [agy_bin, "--version"],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        if res.returncode == 0:
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if lines:
                current = lines[0]
    except Exception:
        return
    if current and current != expected:
        c = make_ansi()
        print(
            c.yellow(
                f"[提示] agy 版本已更新（当前: {current}，已适配: {expected}），建议由 AI 检查插件兼容性。"
            ),
            file=sys.stderr,
        )


def validate_projects_root_launch(real_cwd: Path, projects: dict[str, dict]) -> str | None:
    """当无参数启动 agy 且处于 Projects 目录下时，校验是否在匹配的项目根目录。"""
    matched_root: Path | None = None
    for raw_root in load_project_roots():
        real_root, _ = resolve_canonical_path(os.path.expanduser(raw_root))
        if real_cwd == real_root or real_root in real_cwd.parents:
            matched_root = real_root
            break

    if matched_root is None:
        return None

    if real_cwd == matched_root:
        return (
            f"错误: 当前路径 `{real_cwd}` 是 Projects 根目录，不能直接启动 agy。\n"
            f"请进入具体项目根目录后再执行 `agy`，或使用 `agy proj` 选择项目。"
        )

    cwd_str = str(real_cwd)
    dir_name = real_cwd.name
    exact_matches: list[dict] = []
    parent_match: tuple[str, dict] | None = None
    best_parent_len = -1

    for proj in projects.values():
        for raw_path in proj.get("paths", []):
            if not raw_path:
                continue
            folder_path, _ = resolve_canonical_path(raw_path)
            folder = str(folder_path)
            if cwd_str == folder:
                exact_matches.append(proj)
            elif cwd_str.startswith(folder + os.sep) and len(folder) > best_parent_len:
                best_parent_len = len(folder)
                parent_match = (folder, proj)

    if not exact_matches:
        if parent_match is not None:
            parent_dir, parent_proj = parent_match
            return (
                f"错误: 当前位于项目 `{parent_proj['name']}` 的子目录 `{real_cwd}`。\n"
                f"请在项目根目录 `{parent_dir}` 下启动 `agy`。"
            )
        return (
            f"错误: 当前路径 `{real_cwd}` 未绑定任何项目。\n"
            f"请切换到已绑定项目根目录，或使用 `agy proj new` 创建项目。"
        )

    if not any(p.get("name") == dir_name for p in exact_matches):
        names = ", ".join(p.get("name", p.get("id", "")) for p in exact_matches)
        return (
            f"错误: 当前目录名 `{dir_name}` 与绑定项目名 `{names}` 不一致。\n"
            f"请确认项目配置或使用 `agy proj` 选择正确项目。"
        )

    return None


def _format_conv_line(c: Ansi, conv: dict, prefix: str, branch: str) -> str:
    """格式化单条会话输出行（简洁风格：树枝 + 红色会话名 + 灰色时间）。"""
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
            real_ws, ws_name = resolve_canonical_path(ws_path)
            key = str(real_ws)
            label = ws_name or key
        else:
            key = ""
            label = "(未知工作区)"

        if key not in groups:
            groups[key] = {"label": label, "path": key, "convs": []}
        groups[key]["convs"].append(conv)

    # 默认把用户主目录分组（如 manyy）排在前面，其余按目录名排序
    home_str = str(resolve_canonical_path(Path.home())[0])
    ordered = sorted(
        groups.values(),
        key=lambda g: (g["path"] != home_str, g["path"] == "", g["label"].lower()),
    )
    return [(g["label"], g["path"], g["convs"]) for g in ordered]


ANSI_ESCAPE_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


@dataclass
class TreeLine:
    """项目树中的单行渲染数据与可选路径元信息。"""

    rendered: str
    plain_text: str
    selectable: bool = False
    target_path: str = ""
    project_id: str = ""
    has_conv: bool = False
    path_exists: bool = True


def _strip_ansi(text: str) -> str:
    """移除字符串中的 ANSI 转义序列。"""
    return ANSI_ESCAPE_RE.sub("", text)


def _char_display_width(ch: str) -> int:
    """计算单个字符在终端中的显示列宽。"""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_width(text: str) -> int:
    """计算纯文本在终端中的显示列宽。"""
    return sum(_char_display_width(ch) for ch in text)


def _truncate_plain(text: str, max_width: int) -> str:
    """按终端显示列宽截断纯文本，超出部分以省略号收尾。"""
    if max_width <= 0:
        return ""
    if _display_width(text) <= max_width:
        return text
    if max_width <= 3:
        out: list[str] = []
        w = 0
        for ch in text:
            cw = _char_display_width(ch)
            if w + cw > max_width:
                break
            out.append(ch)
            w += cw
        return "".join(out)

    limit = max_width - 3
    out = []
    w = 0
    for ch in text:
        cw = _char_display_width(ch)
        if w + cw > limit:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "..."


def _truncate_ansi(text: str, max_width: int) -> str:
    """按终端显示列宽截断含 ANSI 颜色码的文本，并保证颜色重置。"""
    if max_width <= 0:
        return ""
    plain = _strip_ansi(text)
    if _display_width(plain) <= max_width:
        return text

    limit = max(0, max_width - 3) if max_width > 3 else max_width
    out: list[str] = []
    w = 0
    idx = 0
    has_ansi = False
    while idx < len(text):
        match = ANSI_ESCAPE_RE.match(text, idx)
        if match:
            out.append(match.group(0))
            has_ansi = True
            idx = match.end()
            continue
        ch = text[idx]
        cw = _char_display_width(ch)
        if w + cw > limit:
            break
        out.append(ch)
        w += cw
        idx += 1

    if max_width > 3:
        out.append("...")
    if has_ansi:
        out.append("\033[0m")
    return "".join(out)


def _append_cli_project_lines(
    lines: list[TreeLine],
    c: Ansi,
    convs: list[dict],
    show_all: bool,
) -> None:
    """在 CLI Project 下以二级树状结构生成按工作区目录分组的渲染行。"""
    groups = _group_cli_convs_by_workspace(convs)
    visible_groups: list[tuple[str, str, list[dict]]] = []
    for label, path, g_convs in groups:
        filtered = g_convs if show_all else [item for item in g_convs if item["steps"] > 0]
        if filtered:
            visible_groups.append((label, path, filtered))

    if not visible_groups:
        plain = "└── (无有效会话)"
        lines.append(TreeLine(rendered=c.dim(plain), plain_text=plain))
        return

    for g_idx, (label, path, g_convs) in enumerate(visible_groups):
        is_last_group = g_idx == len(visible_groups) - 1
        g_branch = "└── " if is_last_group else "├── "
        child_prefix = "    " if is_last_group else "│   "

        exists = bool(path) and os.path.isdir(path)
        missing_suffix = "" if (not path or exists) else " [不存在]"
        path_part = f" {c.green(f'({path})')}" if path else ""
        missing_part = f" {c.yellow('[不存在]')}" if missing_suffix else ""
        rendered_group = f"{c.codex_gray(f'{g_branch}{label}')}{path_part}{missing_part}"
        plain_group = f"{g_branch}{label}" + (f" ({path})" if path else "") + missing_suffix
        lines.append(
            TreeLine(
                rendered=rendered_group,
                plain_text=plain_group,
                selectable=bool(path),
                target_path=path,
                project_id=DEFAULT_PROJECT_ID,
                has_conv=any(item["steps"] > 0 for item in g_convs),
                path_exists=exists,
            )
        )

        for c_idx, conv in enumerate(g_convs):
            c_branch = "└── " if c_idx == len(g_convs) - 1 else "├── "
            rendered_conv = _format_conv_line(c, conv, child_prefix, c_branch)
            lines.append(TreeLine(rendered=rendered_conv, plain_text=_strip_ansi(rendered_conv)))


def build_projects_tree_lines(
    c: Ansi,
    keyword: str = "",
    show_all: bool = False,
    max_title_len: int = 48,
) -> tuple[list[TreeLine], str | None, str | None]:
    """构建项目树状渲染行，返回 (行列表, 提示信息, 空结果提示)。"""
    projects = load_projects()
    convs_by_proj, db_err = load_conversations(max_title_len=max_title_len)

    if not projects and not convs_by_proj:
        return [], None, "未找到任何项目配置或会话记录。"

    ordered_pids = sorted(
        projects.keys(),
        key=lambda k: (k == DEFAULT_PROJECT_ID, projects[k]["name"].lower()),
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
            return [], None, f"未找到匹配 '{keyword}' 的项目。"

    lines: list[TreeLine] = []
    notice = c.yellow(f"[提示] {db_err}") if db_err else None

    for idx, pid in enumerate(all_pids):
        if idx > 0:
            lines.append(TreeLine(rendered="", plain_text=""))

        all_convs = convs_by_proj.get(pid, [])
        valid_convs = [item for item in all_convs if item["steps"] > 0]
        has_valid_conv = bool(valid_convs)

        if pid in projects:
            proj = projects[pid]
            header_rendered = f"{c.codex_gray(proj['name'])}  {c.dim(proj['id'])}"
            header_plain = f"{proj['name']}  {proj['id']}"
            lines.append(TreeLine(rendered=header_rendered, plain_text=header_plain))
            path_items = proj["paths"] or ["(无绑定路径)"]
        else:
            label = pid or "(空项目ID)"
            header_rendered = f"{c.yellow('(未找到配置的项目)')}  {c.dim(label)}"
            header_plain = f"(未找到配置的项目)  {label}"
            lines.append(TreeLine(rendered=header_rendered, plain_text=header_plain))
            path_items = []

        if pid == DEFAULT_PROJECT_ID:
            _append_cli_project_lines(lines, c, all_convs, show_all=show_all)
            continue

        visible_convs = all_convs if show_all else valid_convs
        nodes = [("path", p) for p in path_items] + [("conv", item) for item in visible_convs]
        if not nodes:
            empty_node = "└── (无内容)"
            lines.append(TreeLine(rendered=c.codex_gray(empty_node), plain_text=empty_node))
            continue

        for n_idx, (kind, payload) in enumerate(nodes):
            branch = "└── " if n_idx == len(nodes) - 1 else "├── "
            if kind == "path":
                if payload == "(无绑定路径)":
                    plain = f"{branch}{payload}"
                    lines.append(TreeLine(rendered=c.dim(plain), plain_text=plain))
                else:
                    real_path, _ = resolve_canonical_path(payload)
                    real_str = str(real_path)
                    exists = os.path.isdir(real_str)
                    missing_suffix = "" if exists else " [不存在]"
                    missing_part = f" {c.yellow('[不存在]')}" if missing_suffix else ""
                    rendered = f"{c.codex_gray(branch)}{c.green(payload)}{missing_part}"
                    plain = f"{branch}{payload}{missing_suffix}"
                    lines.append(
                        TreeLine(
                            rendered=rendered,
                            plain_text=plain,
                            selectable=True,
                            target_path=real_str,
                            project_id=pid,
                            has_conv=has_valid_conv,
                            path_exists=exists,
                        )
                    )
            else:
                rendered_conv = _format_conv_line(c, payload, "", branch)
                lines.append(TreeLine(rendered=rendered_conv, plain_text=_strip_ansi(rendered_conv)))

    return lines, notice, None


def render_projects_tree(
    keyword: str = "",
    show_all: bool = False,
    force_color: bool | None = None,
) -> int:
    """以彩色树状结构打印项目、目录分组与会话。"""
    c = make_ansi(force_color)
    # 在 agy 内部 ! 面板中缩短标题上限，避免含控制符的行长度触发 TUI 错误折行
    max_title_len = 28 if _is_inside_agy_shell() else 48
    lines, notice, empty_msg = build_projects_tree_lines(
        c,
        keyword=keyword,
        show_all=show_all,
        max_title_len=max_title_len,
    )
    if empty_msg:
        print(empty_msg)
        return 0
    if notice:
        print(f"{notice}\n")
    for line in lines:
        print(line.rendered)
    return 0


def _read_tui_key(fd: int) -> str:
    """从原始终端文件描述符读取一次按键事件。"""
    ch = os.read(fd, 1)
    if not ch:
        return "QUIT"
    if ch in (b"\r", b"\n"):
        return "ENTER"
    if ch == b"\x03":
        return "QUIT"
    if ch == b"\x0e":
        return "DOWN"
    if ch == b"\x10":
        return "UP"
    if ch in (b"q", b"Q"):
        return "QUIT"
    if ch == b"j":
        return "DOWN"
    if ch == b"k":
        return "UP"
    if ch == b"\x1b":
        # 区分单独 Esc 与方向键等 ANSI 转义序列
        ready, _, _ = select.select([fd], [], [], 0.03)
        if not ready:
            return "QUIT"
        seq = os.read(fd, 1)
        if seq in (b"[", b"O"):
            ready2, _, _ = select.select([fd], [], [], 0.03)
            if ready2:
                code = os.read(fd, 1)
                if code == b"A":
                    return "UP"
                if code == b"B":
                    return "DOWN"
                # 消耗其余扩展序列字节（如 PageUp/Home 等），避免残留
                while select.select([fd], [], [], 0.01)[0]:
                    extra = os.read(fd, 1)
                    if extra.isalpha() or extra == b"~":
                        break
        return "OTHER"
    return "OTHER"


def _draw_tui_screen(
    lines: list[TreeLine],
    selected_line_idx: int,
    scroll_offset: int,
    notice: str | None,
    c: Ansi,
) -> int:
    """在备用屏幕中绘制项目树与高亮选中行，返回更新后的 scroll_offset。"""
    cols, rows = shutil.get_terminal_size((80, 24))
    header_rows = 2 if notice else 0
    footer_rows = 2
    body_height = max(1, rows - header_rows - footer_rows)

    # 尽量让选中路径上方的项目标题行也保留在视野内
    context_top = max(0, selected_line_idx - 1)
    if context_top < scroll_offset:
        scroll_offset = context_top
    elif selected_line_idx >= scroll_offset + body_height:
        scroll_offset = selected_line_idx - body_height + 1
    max_scroll = max(0, len(lines) - body_height)
    scroll_offset = max(0, min(scroll_offset, max_scroll))

    buf: list[str] = ["\033[H"]
    if notice:
        buf.append(_truncate_ansi(notice, cols) + "\033[K\r\n\033[K\r\n")

    visible_slice = lines[scroll_offset : scroll_offset + body_height]
    content_width = max(1, cols - 2)
    for offset, item in enumerate(visible_slice):
        actual_idx = scroll_offset + offset
        if actual_idx == selected_line_idx:
            plain = _truncate_plain(item.plain_text, content_width)
            pad_len = max(0, content_width - _display_width(plain))
            line_text = f"▶ {plain}{' ' * pad_len}"
            if c.enabled:
                buf.append(f"\033[1;97;48;5;24m{line_text}\033[0m\033[K\r\n")
            else:
                buf.append(f"{line_text}\033[K\r\n")
        else:
            truncated = _truncate_ansi(item.rendered, content_width)
            buf.append(f"  {truncated}\033[K\r\n")

    for _ in range(body_height - len(visible_slice)):
        buf.append("\033[K\r\n")

    selected_item = lines[selected_line_idx]
    status_hint = (
        f"↑/↓ 或 Ctrl-P/N 切换路径 | Enter 切换并启动 agy | q / Esc 退出"
        f"  [{selected_item.target_path}]"
    )
    footer_plain = _truncate_plain(status_hint, cols)
    footer_rendered = c.dim(footer_plain) if c.enabled else footer_plain
    buf.append(f"\033[K\r\n{footer_rendered}\033[K\033[J")

    os.write(sys.stdout.fileno(), "".join(buf).encode("utf-8", errors="replace"))
    return scroll_offset


def select_project_path_interactive(
    lines: list[TreeLine],
    notice: str | None,
    c: Ansi,
) -> TreeLine | None:
    """在终端上以交互式高亮列表选择目标项目路径。"""
    selectable_indices = [idx for idx, item in enumerate(lines) if item.selectable]
    if not selectable_indices:
        return None

    real_cwd, _ = resolve_canonical_path()
    cwd_str = str(real_cwd)
    cursor = 0
    for pos, line_idx in enumerate(selectable_indices):
        if lines[line_idx].target_path == cwd_str:
            cursor = pos
            break

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    scroll_offset = 0
    resized = False

    def _on_winch(_signum: int, _frame: object) -> None:
        nonlocal resized
        resized = True

    old_winch = signal.signal(signal.SIGWINCH, _on_winch)
    try:
        tty.setcbreak(fd)
        # 进入备用屏幕并隐藏光标，退出时完整还原主屏幕
        os.write(sys.stdout.fileno(), b"\033[?1049h\033[?25l")
        while True:
            scroll_offset = _draw_tui_screen(
                lines,
                selectable_indices[cursor],
                scroll_offset,
                notice,
                c,
            )
            resized = False
            try:
                key = _read_tui_key(fd)
            except InterruptedError:
                if resized:
                    continue
                return None

            if key == "UP":
                cursor = (cursor - 1) % len(selectable_indices)
            elif key == "DOWN":
                cursor = (cursor + 1) % len(selectable_indices)
            elif key == "ENTER":
                return lines[selectable_indices[cursor]]
            elif key == "QUIT":
                return None
    finally:
        signal.signal(signal.SIGWINCH, old_winch)
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        os.write(sys.stdout.fileno(), b"\033[?25h\033[?1049l")


def create_project(force_color: bool | None = None) -> int:
    """在当前真实物理路径下调用原生 `agy --new-project` 创建项目，拒绝在主目录 (~) 下创建。"""
    c = make_ansi(force_color)
    if _is_inside_agy_shell() or not sys.stdin.isatty():
        print(
            c.yellow("错误: `agy proj new` 需要交互式终端，请在外部终端中执行。"),
            file=sys.stderr,
        )
        return 1

    real_cwd, _ = resolve_canonical_path()
    real_home, _ = resolve_canonical_path(Path.home())

    if real_cwd == real_home:
        print(
            c.yellow("错误: 不能在用户主目录 (~) 下直接创建项目，请先 cd 进入具体的项目目录。"),
            file=sys.stderr,
        )
        return 1

    cwd_str = str(real_cwd)
    projects = load_projects()
    for proj in projects.values():
        for bound in proj["paths"]:
            real_bound, _ = resolve_canonical_path(bound)
            if str(real_bound) == cwd_str:
                print(
                    c.yellow(
                        f"提示: 当前目录已绑定到项目 '{proj['name']}' [id: {proj['id']}]，无需重复创建。"
                    ),
                    file=sys.stderr,
                )
                return 1

    # 先切换到解析软链接后的真实物理路径，再交由原生 agy --new-project 处理名称与配置
    os.chdir(real_cwd)
    exec_agy(["--new-project", "--add-dir", cwd_str])
    return 0


def resolve_agy_binary() -> str:
    """获取原生 agy 可执行文件路径，避免与 shell 包装函数产生歧义。"""
    env_bin = os.environ.get("AGY_BIN", "")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    found = shutil.which("agy")
    if found:
        return found
    print("错误: 未找到 agy 可执行文件。", file=sys.stderr)
    sys.exit(127)


def report_osc7_cwd() -> None:
    """在交互式终端中向 Kitty 等终端上报当前工作目录（OSC 7）。"""
    if not sys.stdout.isatty() or _is_inside_agy_shell():
        return
    try:
        cwd = os.getcwd()
        host = os.uname().nodename
        seq = f"\033]7;file://{host}{quote(cwd)}\007"
        os.write(sys.stdout.fileno(), seq.encode("utf-8", errors="ignore"))
    except Exception:
        pass


def exec_agy(extra_args: list[str]) -> None:
    """原地替换为原生 agy 进程，保证信号与终端控制权完整透传。"""
    check_agy_version_notice()
    report_osc7_cwd()
    real_cwd, _ = resolve_canonical_path()
    os.environ["AGY_LAUNCH_CWD"] = str(real_cwd)
    agy_bin = resolve_agy_binary()
    os.execv(agy_bin, [agy_bin, *extra_args])


def print_proj_help() -> None:
    print("用法:")
    print("  agy proj [选项] [关键字]   在终端交互选择项目路径并启动 agy（非交互环境输出树状列表）")
    print("  agy proj new               在当前真实路径下调用原生 agy --new-project 创建项目")
    print("选项:")
    print("  -a, --all            显示所有会话（包含 0 steps 的空会话）")
    print("  --list, --no-interactive  仅输出项目树状列表，不进入交互选择")
    print("  --color              强制输出 ANSI 颜色（适用于管道或 ! 子命令）")
    print("  --no-color           禁用颜色输出")


def _handle_proj_command(sub_args: list[str]) -> int:
    if sub_args and sub_args[0] == "new":
        new_args = sub_args[1:]
        force_color: bool | None = None
        for arg in new_args:
            if arg in ("-h", "--help"):
                print("用法: agy proj new")
                print("在当前工作目录（自动解析软链接为原始路径）调用 `agy --new-project --add-dir <真实路径>`。")
                print("项目名称交由原生 agy 界面处理；禁止在主目录 (~) 下创建。")
                return 0
            if arg == "--color":
                force_color = True
            elif arg == "--no-color":
                force_color = False
            else:
                print(f"错误: `agy proj new` 不接受参数 `{arg}`（项目名称由原生 agy 处理）。", file=sys.stderr)
                return 2
        return create_project(force_color=force_color)

    show_all = False
    force_color = None
    no_interactive = False
    keywords: list[str] = []

    for arg in sub_args:
        if arg in ("-h", "--help"):
            print_proj_help()
            return 0
        if arg in ("-a", "--all"):
            show_all = True
        elif arg in ("--list", "--no-interactive"):
            no_interactive = True
        elif arg == "--color":
            force_color = True
        elif arg == "--no-color":
            force_color = False
        elif arg.startswith("-"):
            print(f"错误: 未知选项 `{arg}`。", file=sys.stderr)
            print_proj_help()
            return 2
        else:
            keywords.append(arg)

    if len(keywords) > 1:
        print("错误: `agy proj` 最多接受一个过滤关键字。", file=sys.stderr)
        print_proj_help()
        return 2

    keyword = keywords[0] if keywords else ""
    can_interact = (
        not no_interactive
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and not _is_inside_agy_shell()
    )
    if not can_interact:
        return render_projects_tree(
            keyword=keyword,
            show_all=show_all,
            force_color=force_color,
        )

    c = make_ansi(force_color)
    lines, notice, empty_msg = build_projects_tree_lines(
        c,
        keyword=keyword,
        show_all=show_all,
        max_title_len=48,
    )
    if empty_msg:
        print(empty_msg)
        return 0

    if not any(item.selectable for item in lines):
        if notice:
            print(f"{notice}\n")
        for line in lines:
            print(line.rendered)
        return 0

    chosen = select_project_path_interactive(lines, notice, c)
    if chosen is None:
        return 0

    if not chosen.path_exists or not os.path.isdir(chosen.target_path):
        print(
            c.yellow(f"错误: 目标路径不存在: {chosen.target_path}"),
            file=sys.stderr,
        )
        return 1

    os.chdir(chosen.target_path)
    os.environ["AGY_LAUNCH_PROJECT_ID"] = chosen.project_id
    args = ["--project", chosen.project_id, "-c"] if chosen.has_conv else ["--project", chosen.project_id]
    exec_agy(args)
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "proj":
        return _handle_proj_command(argv[1:])

    if not argv:
        real_cwd, _ = resolve_canonical_path()
        os.chdir(real_cwd)
        projects = load_projects()
        err = validate_projects_root_launch(real_cwd, projects)
        if err:
            c = make_ansi()
            print(c.yellow(err), file=sys.stderr)
            return 1
        pid, has_conv = match_project_for_cwd(projects)
        if pid:
            os.environ["AGY_LAUNCH_PROJECT_ID"] = pid
            args = ["-c", "--project", pid] if has_conv else ["--project", pid]
            exec_agy(args)

    exec_agy(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
