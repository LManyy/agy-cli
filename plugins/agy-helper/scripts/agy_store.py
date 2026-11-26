#!/usr/bin/env python3
"""Antigravity (agy) 本地配置与会话数据库只读访问模块。

注意：本模块对 agy 配置文件与 SQLite 数据库仅执行只读操作，禁止任何写入。
如 agy 内部存储结构变更，只需集中排查并更新本文件即可。
"""

import glob
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

PROJECTS_DIR = Path.home() / ".gemini" / "config" / "projects"
DB_PATH = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
DEFAULT_PROJECT_ID = "default-cli-project"


def resolve_canonical_path(raw_path: str | Path | None = None) -> tuple[Path, str]:
    """解析并还原软链接，返回 (真实物理路径, 最后一级目录名)。"""
    target = Path.cwd() if raw_path is None else Path(raw_path)
    real_path = Path(os.path.realpath(target))
    return real_path, real_path.name


def parse_file_uri(uri: str) -> str:
    """将 file:// URI 解码为本地路径，非 file URI 原样返回。"""
    if not uri.startswith("file://"):
        return uri
    return unquote(urlparse(uri).path)


def open_readonly_db(db_path: Path) -> sqlite3.Connection:
    """以只读 URI 模式打开 SQLite，避免对运行中的 agy 产生写锁或修改。"""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def load_projects(warnings: list[str] | None = None) -> dict[str, dict]:
    """只读加载所有本地项目配置（~/.gemini/config/projects/*.json）。"""
    projects: dict[str, dict] = {}
    if not PROJECTS_DIR.is_dir():
        return projects

    for file_path in sorted(glob.glob(str(PROJECTS_DIR / "*.json"))):
        try:
            with open(file_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"[警告] 读取项目配置失败: {file_path} ({exc})")
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
    """按 project_id 分组只读加载所有顶层会话。"""
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


def get_project_id_for_conversation(conversation_id: str) -> str:
    """根据 conversation_id 从会话数据库只读查询所属 project_id。"""
    if not conversation_id or not DB_PATH.is_file():
        return ""
    try:
        with closing(open_readonly_db(DB_PATH)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT project_id FROM conversation_summaries WHERE conversation_id = ? LIMIT 1",
                (conversation_id,),
            )
            row = cur.fetchone()
            return (row[0] or "") if row else ""
    except Exception:
        return ""


def get_current_project_id(payload: dict) -> str:
    """获取当前会话选择的 project_id（优先按 conversationId 查库，其次环境变量兜底）。"""
    cid = str(payload.get("conversationId") or "").strip()
    db_pid = get_project_id_for_conversation(cid)
    if db_pid:
        return db_pid
    return os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()
