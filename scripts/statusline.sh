#!/usr/bin/env python3
from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

RESET = "\033[0m"
DIM = "\033[2m"
RED = "\033[31m"
RED_BLINK = "\033[1;5;31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
BRIGHT_BLUE = "\033[94m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 预留右边距，避免因 TUI 内边距或字符宽窄差异导致超宽折行（产生额外空行）
RIGHT_MARGIN = 2
GIT_TIMEOUT = 0.3
PROJECTS_DIR = Path.home() / ".gemini" / "config" / "projects"
DB_PATH = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
DEFAULT_PROJECT_ID = "default-cli-project"
DEFAULT_PROJECT_ROOTS = ["/work/Projects", "/vault/Projects"]


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_len(s: str) -> int:
    plain = ANSI_RE.sub("", s)
    return sum(char_width(ch) for ch in plain)


def truncate_width(s: str, max_w: int) -> str:
    if visible_len(s) <= max_w:
        return s
    if max_w <= 0:
        return ""
    ellipsis = "..." if max_w >= 4 else "." * max_w
    budget = max_w - len(ellipsis)
    if budget <= 0:
        return ellipsis[:max_w]
    out: list[str] = []
    w = 0
    for ch in s:
        cw = char_width(ch)
        if w + cw > budget:
            break
        out.append(ch)
        w += cw
    return "".join(out) + ellipsis


def shorten_home(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    prefix = home.rstrip(os.sep) + os.sep
    if path.startswith(prefix):
        return "~" + os.sep + path[len(prefix):]
    return path


def compact_path(path: str, max_w: int) -> str:
    """尽量保留路径首尾关键信息，超宽时优先省略中间目录。"""
    if visible_len(path) <= max_w:
        return path
    if max_w <= 3:
        return truncate_width(path, max_w)

    sep = os.sep
    if path.startswith("~" + sep):
        prefix = "~" + sep
        rest = path[len(prefix):]
    elif path.startswith(sep):
        prefix = sep
        rest = path[len(prefix):]
    else:
        prefix = ""
        rest = path

    segs = [s for s in rest.split(sep) if s]
    if len(segs) >= 2:
        for keep in range(len(segs) - 1, 0, -1):
            candidate = f"{prefix}...{sep}{sep.join(segs[-keep:])}"
            if visible_len(candidate) <= max_w:
                return candidate
        candidate = f"...{sep}{segs[-1]}"
        if visible_len(candidate) <= max_w:
            return candidate
        return truncate_width(candidate, max_w)

    return truncate_width(path, max_w)


def get_cwd(data: dict) -> str:
    ws = data.get("workspace")
    if isinstance(ws, dict):
        cur = ws.get("current_dir")
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    try:
        return os.getcwd()
    except OSError:
        return ""


def _parse_file_uri(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    return unquote(urlparse(uri).path)


def _load_project_map() -> dict[str, dict]:
    """只读加载本地项目配置，返回 {project_id: {name, paths}}。"""
    projects: dict[str, dict] = {}
    if not PROJECTS_DIR.is_dir():
        return projects
    for fp_path in sorted(glob.glob(str(PROJECTS_DIR / "*.json"))):
        try:
            with open(fp_path, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
            pid = cfg.get("id") or Path(fp_path).stem
            name = cfg.get("name") or pid
            resources = (cfg.get("projectResources") or {}).get("resources") or []
            paths = [
                os.path.realpath(_parse_file_uri(r.get("folderUri", "")))
                for r in resources
                if r.get("folderUri")
            ]
            projects[pid] = {"name": name, "paths": paths}
        except Exception:
            continue
    return projects


def _get_project_id_from_db(conv_id: str) -> str:
    if not conv_id or not DB_PATH.is_file():
        return ""
    try:
        uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT project_id FROM conversation_summaries WHERE conversation_id = ? LIMIT 1",
                (conv_id,),
            )
            row = cur.fetchone()
            return (row[0] or "").strip() if row else ""
    except Exception:
        return ""


def _load_helper_config() -> dict:
    """只读加载插件 config.json。"""
    candidates = [
        Path.home() / ".gemini" / "config" / "plugins" / "agy-helper" / "config.json",
        Path.home() / ".config" / "agy-helper" / "config.json",
    ]
    for cfg_path in candidates:
        if cfg_path.is_file():
            try:
                with open(cfg_path, "r", encoding="utf-8") as fp:
                    cfg = json.load(fp)
                if isinstance(cfg, dict):
                    return cfg
            except Exception:
                pass
    return {}


def _load_project_roots(cfg: dict) -> list[Path]:
    """只读加载受控的 Projects 根目录列表。"""
    raw_roots = cfg.get("projectRoots") if isinstance(cfg.get("projectRoots"), list) else DEFAULT_PROJECT_ROOTS
    return [Path(os.path.realpath(os.path.expanduser(str(r)))) for r in raw_roots if r]


def _get_version_warning(cfg: dict) -> str:
    """检查当前 agy 版本是否已适配，不匹配时返回警告标签。"""
    expected = str(cfg.get("supportedAgyVersion") or "").strip()
    if not expected:
        return ""
    agy_bin = os.environ.get("AGY_BIN", "").strip()
    if not agy_bin or not os.path.isfile(agy_bin) or not os.access(agy_bin, os.X_OK):
        agy_bin = shutil.which("agy") or ""
    if not agy_bin:
        return ""
    try:
        res = subprocess.run(
            [agy_bin, "--version"],
            text=True,
            capture_output=True,
            timeout=1.5,
            check=False,
        )
        if res.returncode != 0:
            return ""
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        current = lines[0] if lines else ""
    except Exception:
        return ""
    if current and current != expected:
        return f"[! agy {current}未适配]"
    return ""


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def get_project_display(data: dict, cwd: str) -> tuple[str, str]:
    """只读解析当前项目名与校验警告，返回 (项目名, 警告标签)。"""
    projects = _load_project_map()

    explicit_name = ""
    for key in ("project_name", "projectName"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            explicit_name = val.strip()
            break

    pid = ""
    for key in ("project_id", "projectId"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            pid = val.strip()
            break

    if not pid:
        conv_id = ""
        for key in ("conversation_id", "conversationId"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                conv_id = val.strip()
                break
        if conv_id:
            pid = _get_project_id_from_db(conv_id)

    if not pid:
        pid = os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()

    proj = projects.get(pid) if pid else None
    proj_name = explicit_name or (proj["name"] if proj else "")

    if not proj_name and cwd:
        real_cwd_str = os.path.realpath(cwd)
        for p in projects.values():
            if real_cwd_str in p["paths"]:
                proj_name = p["name"]
                proj = p
                break

    cfg = _load_helper_config()
    ver_warn = _get_version_warning(cfg)

    if not cwd:
        return proj_name, ver_warn

    real_cwd = Path(os.path.realpath(cwd))
    roots = _load_project_roots(cfg)
    cwd_root = next((r for r in roots if _is_same_or_descendant(real_cwd, r)), None)

    path_warn = ""
    launch_raw = os.environ.get("AGY_LAUNCH_CWD", "").strip()
    if launch_raw:
        real_launch = Path(os.path.realpath(launch_raw))
        launch_root = next((r for r in roots if _is_same_or_descendant(real_launch, r)), None)
        if (cwd_root is not None or launch_root is not None) and real_launch != real_cwd:
            path_warn = "[! 跨项目会话]"

    if not path_warn and cwd_root is not None:
        if real_cwd == cwd_root:
            path_warn = "[! 非项目根目录]"
        elif not pid or pid == DEFAULT_PROJECT_ID or proj is None:
            path_warn = "[! 未匹配项目]"
        elif proj["name"] != real_cwd.name:
            path_warn = "[! 项目不匹配]"
        elif proj["paths"] and str(real_cwd) not in proj["paths"]:
            path_warn = "[! 非绑定根目录]"

    combined = " ".join(w for w in (ver_warn, path_warn) if w)
    return proj_name, combined


def run_git(cwd: str, args: list[str]) -> str | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout
    except Exception:
        return None
    return None


def parse_numstat(output: str | None) -> tuple[int, int]:
    added = 0
    deleted = 0
    if not output:
        return 0, 0
    for line in output.splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        if cols[0].isdigit():
            added += int(cols[0])
        if cols[1].isdigit():
            deleted += int(cols[1])
    return added, deleted


def get_git_info(data: dict, cwd: str) -> tuple[str | None, int, int]:
    branch: str | None = None
    added: int | None = None
    deleted: int | None = None

    vcs = data.get("vcs")
    if isinstance(vcs, dict):
        b = vcs.get("branch")
        if isinstance(b, str) and b.strip():
            branch = b.strip()
        la = vcs.get("lines_added")
        lr = vcs.get("lines_removed")
        if isinstance(la, int) and not isinstance(la, bool):
            added = max(0, la)
        if isinstance(lr, int) and not isinstance(lr, bool):
            deleted = max(0, lr)

    if not branch:
        raw_b = run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
        if raw_b is not None:
            b_str = raw_b.strip()
            if b_str == "HEAD":
                short_sha = run_git(cwd, ["rev-parse", "--short", "HEAD"])
                branch = f"@{short_sha.strip()}" if short_sha and short_sha.strip() else "HEAD"
            elif b_str:
                branch = b_str

    if not branch:
        return None, 0, 0

    if added is None or deleted is None:
        diff_head = run_git(cwd, ["diff", "--numstat", "HEAD"])
        if diff_head is not None:
            added, deleted = parse_numstat(diff_head)
        else:
            a1, d1 = parse_numstat(run_git(cwd, ["diff", "--numstat"]))
            a2, d2 = parse_numstat(run_git(cwd, ["diff", "--cached", "--numstat"]))
            added, deleted = a1 + a2, d1 + d2

    return branch, added or 0, deleted or 0


def fmt_mode(data: dict) -> str | None:
    mode = None
    for key in ("cycle_mode", "mode", "permission_mode", "approval_mode"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            mode = val.strip()
            break
    if not mode:
        return None

    low = mode.lower()
    if low == "auto":
        color = RED
    elif "accept" in low or "edit" in low:
        color = YELLOW
    elif low == "plan":
        color = BLUE
    else:
        color = DIM
    return f"{color}{mode}{RESET}"


def pick_bucket(quota: dict, preferred: str, keywords: tuple[str, ...]) -> dict | None:
    if preferred in quota and isinstance(quota[preferred], dict):
        return quota[preferred]
    for key, val in quota.items():
        if not isinstance(key, str) or not isinstance(val, dict):
            continue
        low = key.lower()
        if any(kw in low for kw in keywords):
            return val
    return None


def get_reset_seconds(bucket: dict | None) -> int | None:
    if not bucket:
        return None

    sec = bucket.get("reset_in_seconds")
    if sec is not None and not isinstance(sec, bool):
        try:
            sec_val = float(sec)
            if math.isfinite(sec_val):
                return max(0, int(sec_val))
        except (TypeError, ValueError):
            pass

    rt = bucket.get("reset_time")
    if isinstance(rt, str) and rt.strip():
        try:
            ts = rt.strip()
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0, int(delta))
        except ValueError:
            pass

    return None


def fmt_duration(secs: int) -> str:
    if secs < 60:
        return "<1m"
    mins = secs // 60
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{mins}m"
    return f"{mins}m"


def reset_color(secs: int, short_thresh: int, mid_thresh: int) -> str:
    if secs <= short_thresh:
        return GREEN
    if secs <= mid_thresh:
        return YELLOW
    return RED


def fmt_reset(label: str, bucket: dict | None, short_thresh: int, mid_thresh: int) -> str | None:
    secs = get_reset_seconds(bucket)
    if secs is None:
        return None
    color = reset_color(secs, short_thresh, mid_thresh)
    return f"{DIM}{label} reset {RESET}{color}{fmt_duration(secs)}{RESET}"


def pct_color(pct: int) -> str:
    if pct >= 50:
        return GREEN
    if pct >= 20:
        return YELLOW
    return RED


def fmt_quota(label: str, bucket: dict | None) -> str | None:
    if not bucket:
        return None
    frac = bucket.get("remaining_fraction")
    if frac is None or isinstance(frac, bool):
        return None
    try:
        frac_val = float(frac)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(frac_val):
        return None
    pct = max(0, min(100, round(frac_val * 100)))
    color = pct_color(pct)
    return f"{DIM}{label} {RESET}{color}{pct}%{RESET}{DIM} left{RESET}"


def wrap_parts(parts: list[str], sep: str, max_w: int) -> list[str]:
    """按字段边界贪心折行，确保每行不超过 max_w。"""
    sep_w = visible_len(sep)
    lines: list[str] = []
    cur: list[str] = []
    cur_w = 0

    for part in parts:
        pw = visible_len(part)
        if not cur:
            cur = [part]
            cur_w = pw
        elif cur_w + sep_w + pw <= max_w:
            cur.append(part)
            cur_w += sep_w + pw
        else:
            lines.append(sep.join(cur))
            cur = [part]
            cur_w = pw

    if cur:
        lines.append(sep.join(cur))
    return lines


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    if not isinstance(data, dict):
        return

    # 0. 项目名与不匹配警告（最左边）& 1. 工作目录（用户目录用 ~）
    cwd = get_cwd(data)
    cwd_text = shorten_home(cwd) if cwd else ""
    proj_name, proj_warn = get_project_display(data, cwd)
    if proj_warn:
        warn_text = f"{proj_name} {proj_warn}".strip() if proj_name else proj_warn
        proj_part = f"{RED_BLINK}{warn_text}{RESET}"
    elif proj_name:
        proj_part = f"{YELLOW}{proj_name}{RESET}"
    else:
        proj_part = ""

    # 2~7. 其余字段（默认全部完整展开）
    rest_parts: list[str] = []

    # 2. 会话名（不硬截断，尽量展开）
    title = data.get("conversation_title")
    title_text = title.strip() if isinstance(title, str) and title.strip() else ""
    if title_text:
        rest_parts.append(f"{BRIGHT_BLUE}{title_text}{RESET}")

    # 3. 分支名 & 4. 当前分支修改情况（+11,-2，Git 配色）
    branch, added, deleted = get_git_info(data, cwd)
    if branch:
        rest_parts.append(f"{MAGENTA}{branch}{RESET}")
        if added > 0 or deleted > 0:
            rest_parts.append(f"{GREEN}+{added}{RESET}{DIM},{RESET}{RED}-{deleted}{RESET}")

    # 5. 批准模式（accept-edits / auto / plan / default）
    mode_str = fmt_mode(data)
    if mode_str:
        rest_parts.append(mode_str)

    # 6. 5h 重置时间和周重置时间 & 7. 已有配额信息
    quota = data.get("quota")
    if isinstance(quota, dict) and quota:
        b5 = pick_bucket(quota, "gemini-5h", ("5h", "5_h", "5-hour"))
        bw = pick_bucket(quota, "gemini-weekly", ("weekly", "7d"))

        r5 = fmt_reset("5h", b5, short_thresh=3600, mid_thresh=3 * 3600)
        rw = fmt_reset("7d", bw, short_thresh=24 * 3600, mid_thresh=3 * 24 * 3600)
        if r5:
            rest_parts.append(r5)
        if rw:
            rest_parts.append(rw)

        five_h = fmt_quota("5h", b5)
        weekly = fmt_quota("weekly", bw)
        if five_h:
            rest_parts.append(five_h)
        if weekly:
            rest_parts.append(weekly)

    if not proj_part and not cwd_text and not rest_parts:
        return

    sep = f"{DIM} · {RESET}"
    width = data.get("terminal_width")
    avail = (width - RIGHT_MARGIN) if (isinstance(width, int) and not isinstance(width, bool) and width > RIGHT_MARGIN) else None

    prefix_parts = [proj_part] if proj_part else []

    # 主状态信息（尽量展开 -> 优先省略路径 -> 仍满则换行）
    if avail is None:
        all_parts = prefix_parts + ([f"{CYAN}{cwd_text}{RESET}"] if cwd_text else []) + rest_parts
        print(sep.join(all_parts))
        return

    full_parts = prefix_parts + ([f"{CYAN}{cwd_text}{RESET}"] if cwd_text else []) + rest_parts
    full_line = sep.join(full_parts)
    if visible_len(full_line) <= avail:
        pad = max(0, avail - visible_len(full_line))
        print((" " * pad) + full_line)
        return

    if cwd_text:
        fixed_parts = prefix_parts + rest_parts
        fixed_line = sep.join(fixed_parts)
        cwd_budget = avail - (visible_len(fixed_line) + visible_len(sep) if fixed_parts else 0)
        if cwd_budget >= 4:
            short_cwd = compact_path(cwd_text, cwd_budget)
            compact_parts = prefix_parts + [f"{CYAN}{short_cwd}{RESET}", *rest_parts]
            compact_line = sep.join(compact_parts)
            if visible_len(compact_line) <= avail:
                pad = max(0, avail - visible_len(compact_line))
                print((" " * pad) + compact_line)
                return

    final_parts: list[str] = list(prefix_parts)
    if cwd_text:
        c_disp = compact_path(cwd_text, avail) if visible_len(cwd_text) > avail else cwd_text
        final_parts.append(f"{CYAN}{c_disp}{RESET}")
    for p in rest_parts:
        if visible_len(p) > avail and title_text and p == f"{BRIGHT_BLUE}{title_text}{RESET}":
            final_parts.append(f"{BRIGHT_BLUE}{truncate_width(title_text, avail)}{RESET}")
        else:
            final_parts.append(p)

    for row in wrap_parts(final_parts, sep, avail):
        pad = max(0, avail - visible_len(row))
        print((" " * pad) + row)


if __name__ == "__main__":
    main()
