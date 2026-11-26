#!/usr/bin/env python3
"""轻量级 Codex MCP Server（纯 Python 标准库 stdio JSON-RPC 2.0 实现）。

功能：
1. 暴露 `codex_run` 与 `codex_reply` 工具，将编码任务委派给本机官方 `codex` CLI。
2. 使用 ThreadPoolExecutor 并发处理多个工具调用，各会话互不阻塞，退出时清理全部子进程组。
3. 支持为会话指定 `name` 命名，按 `<项目名>_<会话名>_<session_id>.log` 隔离写入旁路日志。
4. 独立 `CODEX_HOME=~/.codex-mcp` 隔离会话索引，隔离 `stdin=DEVNULL`，并发消费 `stdout/stderr`。
"""

import sys

sys.dont_write_bytecode = True

import glob
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agy-codex-mcp", "version": "1.4.0"}
DEFAULT_LOG_DIR = Path(os.environ.get("CODEX_MCP_LOG_DIR", "/tmp/agy-codex-mcp"))
DEFAULT_PROJECT_ROOTS = ["/work/Projects", "/vault/Projects"]
PROJECTS_DIR = Path.home() / ".gemini" / "config" / "projects"
MAX_WORKERS = max(1, int(os.environ.get("CODEX_MCP_MAX_WORKERS", "4")))
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_STDOUT_LOCK = threading.Lock()
_LATEST_LINK_LOCK = threading.Lock()
_PROCS_LOCK = threading.Lock()
_ACTIVE_PROCS: set[subprocess.Popen] = set()


def _load_project_roots() -> list[Path]:
    candidates = [
        Path(__file__).resolve().parent.parent / "plugins" / "agy-helper" / "config.json",
        Path.home() / ".gemini" / "config" / "plugins" / "agy-helper" / "config.json",
        Path.home() / ".config" / "agy-helper" / "config.json",
    ]
    raw_roots = DEFAULT_PROJECT_ROOTS
    for cfg in candidates:
        if cfg.is_file():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("projectRoots"), list):
                    raw_roots = [str(x) for x in data["projectRoots"] if x]
                    break
            except Exception:
                pass
    return [Path(os.path.realpath(os.path.expanduser(r))) for r in raw_roots if r]


def _sanitize_label(raw: str, fallback: str = "session") -> str:
    cleaned = SAFE_NAME_RE.sub("_", raw.strip()).strip("._-")
    return cleaned or fallback


class SessionLogger:
    """为单次会话维护实时落盘的独立日志文件，支持项目名 + 会话命名 + session_id。"""

    def __init__(
        self,
        log_dir: Path,
        project_label: str,
        session_name: str = "",
        session_id: str = "",
    ) -> None:
        self.log_dir = log_dir
        proj_part = _sanitize_label(project_label, "project")
        name_part = _sanitize_label(session_name, "") if session_name.strip() else ""
        self.prefix_label = f"{proj_part}_{name_part}" if name_part else proj_part
        self.session_name = name_part
        self.session_id = session_id
        self._lock = threading.Lock()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.bound_to_session = False

        if session_id:
            existing = self._find_existing_by_session(session_id)
            if existing is not None:
                self.log_path = existing
            else:
                self.log_path = self.log_dir / f"{self.prefix_label}_{_sanitize_label(session_id)}.log"
            self.bound_to_session = True
        else:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            tid = threading.get_ident() % 10000
            self.log_path = self.log_dir / f"{self.prefix_label}_pending_{ts}_{os.getpid()}_{tid}.log"

        self.log_path.touch(exist_ok=True)
        self._update_latest_symlink()

    def _find_existing_by_session(self, session_id: str) -> Path | None:
        safe_sid = _sanitize_label(session_id)
        matches = sorted(glob.glob(str(self.log_dir / f"*_{safe_sid}.log")))
        if matches:
            return Path(matches[0])
        direct = self.log_dir / f"{safe_sid}.log"
        if direct.is_file():
            return direct
        return None

    def _update_latest_symlink(self) -> None:
        latest = self.log_dir / "latest.log"
        with _LATEST_LINK_LOCK:
            try:
                if latest.is_symlink() or latest.exists():
                    latest.unlink()
                latest.symlink_to(self.log_path.name)
            except Exception:
                pass

    def bind_session(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            if self.bound_to_session:
                return
            self.session_id = session_id
            existing = self._find_existing_by_session(session_id)
            target = existing or (self.log_dir / f"{self.prefix_label}_{_sanitize_label(session_id)}.log")
            if target != self.log_path:
                try:
                    if existing is None:
                        os.replace(self.log_path, target)
                    else:
                        if self.log_path.is_file():
                            content = self.log_path.read_text(encoding="utf-8", errors="replace")
                            if content:
                                with open(target, "a", encoding="utf-8") as fp:
                                    fp.write(content)
                            self.log_path.unlink(missing_ok=True)
                    self.log_path = target
                    self._update_latest_symlink()
                except Exception:
                    pass
            self.bound_to_session = True

    def log(self, line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{ts}] {line}\n"
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as fp:
                    fp.write(formatted)
                    fp.flush()
            except Exception:
                pass


def _load_registered_project_paths() -> list[tuple[Path, str]]:
    """只读加载已注册项目的绑定物理路径，返回 [(物理路径, 项目名)]。"""
    result: list[tuple[Path, str]] = []
    if not PROJECTS_DIR.is_dir():
        return result
    for fp_path in sorted(glob.glob(str(PROJECTS_DIR / "*.json"))):
        try:
            data = json.loads(Path(fp_path).read_text(encoding="utf-8"))
            name = str(data.get("name") or Path(fp_path).stem)
            resources = (data.get("projectResources") or {}).get("resources") or []
            for r in resources:
                uri = str(r.get("folderUri") or "")
                if uri.startswith("file://"):
                    raw_path = unquote(urlparse(uri).path)
                    if raw_path:
                        result.append((Path(os.path.realpath(raw_path)), name))
        except Exception:
            continue
    return result


def _prepare_codex_home() -> Path:
    """准备独立的 CODEX_HOME（默认 ~/.codex-mcp），共享登录态但隔离会话存储。"""
    mcp_home = Path(os.path.realpath(os.path.expanduser(os.environ.get("CODEX_MCP_HOME", "~/.codex-mcp"))))
    mcp_home.mkdir(parents=True, exist_ok=True)
    src_home = Path(os.path.expanduser(os.environ.get("CODEX_ORIG_HOME", "~/.codex")))
    if src_home.is_dir() and src_home != mcp_home:
        for fname in ("auth.json", "config.toml", ".credentials.json"):
            src = src_home / fname
            dst = mcp_home / fname
            if src.exists() and not dst.exists() and not dst.is_symlink():
                try:
                    dst.symlink_to(src)
                except Exception:
                    pass
    return mcp_home


def _resolve_codex_bin() -> str:
    env_bin = os.environ.get("CODEX_BIN", "").strip()
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    return shutil.which("codex") or ""


def _validate_cwd(raw_cwd: str) -> tuple[str | None, str | None]:
    if not raw_cwd.strip():
        return None, "错误: 必须提供有效的 cwd 工作目录。"
    real_cwd = Path(os.path.realpath(os.path.expanduser(raw_cwd)))
    if not real_cwd.is_dir():
        return None, f"错误: 目录不存在: {real_cwd}"

    roots = _load_project_roots()
    matched_root = next((r for r in roots if real_cwd == r or r in real_cwd.parents), None)
    if matched_root is None:
        return None, f"错误: 仅允许在 Projects 目录（{', '.join(str(r) for r in roots)}）下运行 codex，拒绝: {real_cwd}"
    if real_cwd == matched_root:
        return None, f"错误: 不能直接在 Projects 根目录 ({real_cwd}) 运行 codex，请指定具体项目目录。"

    reg_paths = _load_registered_project_paths()
    for bound_path, proj_name in reg_paths:
        if bound_path in real_cwd.parents:
            return None, f"错误: 当前目录 {real_cwd} 是项目 '{proj_name}' 的子目录，请在项目根目录 {bound_path} 运行。"

    return str(real_cwd), None


def _parse_codex_event(raw_line: str, state: dict, state_lock: threading.Lock, logger: SessionLogger) -> None:
    try:
        ev = json.loads(raw_line)
    except Exception:
        logger.log(raw_line)
        return
    if not isinstance(ev, dict):
        return

    ev_type = str(ev.get("type") or "")
    tid = ev.get("thread_id") or ev.get("session_id")
    if isinstance(tid, str) and tid.strip():
        sid = tid.strip()
        with state_lock:
            state["session_id"] = sid
        logger.bind_session(sid)

    item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
    if ev_type in ("item.completed", "item.updated"):
        itype = str(item.get("type") or "")
        if itype == "agent_message" and isinstance(item.get("text"), str):
            with state_lock:
                state["messages"].append(item["text"])
            logger.log(f"[agent] {item['text']}")
        elif itype == "command_execution":
            cmd = item.get("command") or ""
            exit_code = item.get("exit_code")
            out = item.get("aggregated_output") or ""
            logger.log(f"[cmd exit={exit_code}] {cmd}")
            if out:
                logger.log(str(out).rstrip())
        elif itype == "reasoning" and isinstance(item.get("text"), str):
            logger.log(f"[thinking] {item['text']}")
        elif itype == "file_change":
            logger.log(f"[file_change] {json.dumps(item.get('changes', []), ensure_ascii=False)}")
        else:
            logger.log(f"[{ev_type}:{itype}] {raw_line}")
    elif ev_type == "error":
        msg = ev.get("message") or raw_line
        with state_lock:
            state["errors"].append(str(msg))
        logger.log(f"[error] {msg}")
    else:
        logger.log(f"[{ev_type or 'event'}] {raw_line}")


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _cleanup_all_active_procs() -> None:
    with _PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)
    for proc in procs:
        _terminate_process_group(proc)


def _run_codex_exec(
    cmd: list[str],
    cwd: str | None,
    timeout_sec: int,
    logger: SessionLogger,
    preset_session_id: str = "",
) -> dict:
    logger.log("=" * 60)
    logger.log(f"RUN name={logger.session_name or '-'} cwd={cwd or '(default)'} cmd={' '.join(cmd)}")
    state: dict = {"session_id": preset_session_id, "messages": [], "errors": [], "stderr": []}
    state_lock = threading.Lock()

    try:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(_prepare_codex_home())
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except Exception as exc:
        logger.log(f"[spawn_error] {exc}")
        return {"isError": True, "text": f"启动 codex 失败: {exc}\n[log] {logger.log_path}"}

    with _PROCS_LOCK:
        _ACTIVE_PROCS.add(proc)

    def _read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip("\r\n")
            if text:
                _parse_codex_event(text, state, state_lock, logger)

    def _read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            text = line.rstrip("\r\n")
            if text:
                with state_lock:
                    state["stderr"].append(text)
                logger.log(f"[stderr] {text}")

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    effective_timeout = max(5, timeout_sec)
    timed_out = False
    try:
        proc.wait(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc)
        proc.wait()
        logger.log(f"[timeout] 超过 {effective_timeout}s 已强制终止进程组")
    finally:
        with _PROCS_LOCK:
            _ACTIVE_PROCS.discard(proc)

    t_out.join(timeout=2)
    t_err.join(timeout=2)

    if timed_out:
        return {"isError": True, "text": f"codex 执行超时（>{effective_timeout}s）\n[log] {logger.log_path}"}

    with state_lock:
        sid = state["session_id"]
        messages = list(state["messages"])
        errors = list(state["errors"])
        stderr_text = "\n".join(state["stderr"]).strip()

    summary_parts: list[str] = []
    if logger.session_name:
        summary_parts.append(f"session_name: {logger.session_name}")
    if sid:
        summary_parts.append(f"session_id: {sid}")
    if messages:
        summary_parts.append(messages[-1])
    elif errors:
        summary_parts.append("\n".join(errors))
    elif stderr_text:
        summary_parts.append(stderr_text)
    else:
        summary_parts.append(f"执行结束（exit={proc.returncode}）")

    summary_parts.append(f"\n[log] {logger.log_path}")
    is_err = proc.returncode != 0 and not messages
    return {"isError": is_err, "text": "\n".join(summary_parts)}


def _handle_tool_call(name: str, args: dict) -> dict:
    codex_bin = _resolve_codex_bin()
    if not codex_bin:
        return {"isError": True, "text": "未在 PATH 中找到 codex 可执行文件，请先安装并登录 codex。"}

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"isError": True, "text": "错误: prompt 不能为空。"}

    timeout_sec = int(args.get("timeout_sec") or 600)
    sandbox = str(args.get("sandbox") or "workspace-write").strip()
    session_name = str(args.get("name") or "").strip()

    if name == "codex_run":
        cwd, err = _validate_cwd(str(args.get("cwd") or ""))
        if err:
            return {"isError": True, "text": err}
        label = Path(cwd).name or "project"
        logger = SessionLogger(DEFAULT_LOG_DIR, project_label=label, session_name=session_name)
        cmd = [codex_bin, "exec", "--json", "-C", cwd, "-s", sandbox, prompt]
        return _run_codex_exec(cmd, cwd=cwd, timeout_sec=timeout_sec, logger=logger)

    if name == "codex_reply":
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            return {"isError": True, "text": "错误: codex_reply 必须提供 session_id。"}
        raw_cwd = str(args.get("cwd") or "").strip()
        cwd = None
        if raw_cwd:
            cwd, err = _validate_cwd(raw_cwd)
            if err:
                return {"isError": True, "text": err}
        label = Path(cwd).name if cwd else "resume"
        logger = SessionLogger(
            DEFAULT_LOG_DIR,
            project_label=label,
            session_name=session_name,
            session_id=session_id,
        )
        cmd = [codex_bin, "exec", "resume", "--json"]
        if cwd:
            cmd.extend(["-C", cwd])
        cmd.extend([session_id, prompt])
        return _run_codex_exec(
            cmd,
            cwd=cwd,
            timeout_sec=timeout_sec,
            logger=logger,
            preset_session_id=session_id,
        )

    return {"isError": True, "text": f"未知工具: {name}"}


TOOLS = [
    {
        "name": "codex_run",
        "description": "使用本机 codex CLI 在指定项目目录执行编码、重构或只读审查任务，支持并发与会话命名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "交给 codex 的完整任务描述"},
                "cwd": {"type": "string", "description": "目标项目根目录绝对路径"},
                "name": {"type": "string", "description": "可选，会话简短命名（如 auth-fix），用于区分日志"},
                "sandbox": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write", "danger-full-access"],
                    "default": "workspace-write",
                },
                "timeout_sec": {"type": "integer", "default": 600},
            },
            "required": ["prompt", "cwd"],
        },
    },
    {
        "name": "codex_reply",
        "description": "基于 codex_run 返回的 session_id 继续多轮对话返工或追加指令（追加写入同一会话日志）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "codex_run 返回的 session_id"},
                "prompt": {"type": "string", "description": "后续追加指令"},
                "name": {"type": "string", "description": "可选，会话命名"},
                "cwd": {"type": "string", "description": "可选，项目根目录"},
                "timeout_sec": {"type": "integer", "default": 600},
            },
            "required": ["session_id", "prompt"],
        },
    },
]


def _send_response(msg_id: object, result: dict | None = None, error: dict | None = None) -> None:
    payload: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {}
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with _STDOUT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def _dispatch_tool_call(msg_id: object, tool_name: str, tool_args: dict) -> None:
    try:
        res = _handle_tool_call(tool_name, tool_args)
        _send_response(
            msg_id,
            {
                "content": [{"type": "text", "text": res["text"]}],
                "isError": bool(res.get("isError")),
            },
        )
    except Exception as exc:
        _send_response(
            msg_id,
            {
                "content": [{"type": "text", "text": f"内部错误: {exc}"}],
                "isError": True,
            },
        )


def main() -> int:
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="codex-mcp")
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception:
                continue
            if not isinstance(req, dict):
                continue

            method = req.get("method", "")
            msg_id = req.get("id")
            params = req.get("params") if isinstance(req.get("params"), dict) else {}

            if msg_id is None:
                continue

            if method == "initialize":
                _send_response(
                    msg_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                )
            elif method == "ping":
                _send_response(msg_id, {})
            elif method == "tools/list":
                _send_response(msg_id, {"tools": TOOLS})
            elif method == "tools/call":
                tool_name = str(params.get("name") or "")
                tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                executor.submit(_dispatch_tool_call, msg_id, tool_name, tool_args)
            else:
                _send_response(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})
    finally:
        _cleanup_all_active_procs()
        executor.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
