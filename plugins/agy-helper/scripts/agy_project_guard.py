#!/usr/bin/env python3
"""Projects 目录与所选项目一致性校验模块。"""

import json
import os
from pathlib import Path

import agy_store

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PLUGIN_ROOT / "config.json"


def load_plugin_config() -> dict:
    """读取插件根目录的 config.json。"""
    if not CONFIG_PATH.is_file():
        return {"projectRoots": ["/work/Projects", "/vault/Projects"]}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"projectRoots": []}


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def validate_project_cwd_consistency(payload: dict) -> str | None:
    """校验 Projects 目录下的当前路径与所选项目是否一致；一致或无需校验时返回 None。"""
    config = load_plugin_config()
    raw_roots = config.get("projectRoots") or []
    if not raw_roots:
        return None

    raw_cwd = str(payload.get("cwd") or "").strip()
    if not raw_cwd:
        ws = payload.get("workspacePaths") or []
        raw_cwd = str(ws[0]).strip() if ws else ""
    if not raw_cwd:
        return None

    real_cwd, dir_name = agy_store.resolve_canonical_path(raw_cwd)

    launch_cwd_raw = os.environ.get("AGY_LAUNCH_CWD", "").strip()
    real_launch_cwd: Path | None = None
    if launch_cwd_raw:
        real_launch_cwd, _ = agy_store.resolve_canonical_path(launch_cwd_raw)

    roots: list[Path] = []
    for raw_root in raw_roots:
        if not raw_root:
            continue
        real_root, _ = agy_store.resolve_canonical_path(os.path.expanduser(str(raw_root)))
        roots.append(real_root)

    cwd_root = next((r for r in roots if _is_same_or_descendant(real_cwd, r)), None)
    launch_root = (
        next((r for r in roots if _is_same_or_descendant(real_launch_cwd, r)), None)
        if real_launch_cwd is not None
        else None
    )

    if cwd_root is None and launch_root is None:
        return None

    if real_launch_cwd is not None and real_launch_cwd != real_cwd:
        return (
            f"项目校验失败: 会话目录与启动目录不一致\n"
            f"启动目录: {real_launch_cwd}\n"
            f"当前会话: {real_cwd}\n"
            f"请勿跨项目切换或恢复会话，请在对应项目根目录启动 agy"
        )

    matched_root = cwd_root or launch_root
    if real_cwd == matched_root:
        return (
            f"项目校验失败: 当前位于 Projects 根目录\n"
            f"路径: {real_cwd}\n"
            f"请进入具体项目根目录后启动 agy"
        )

    cid = str(payload.get("conversationId") or "").strip()
    db_pid = agy_store.get_project_id_for_conversation(cid)

    # 仅在会话尚未入库时使用 workspacePaths 辅助拦截跨项目切换，避免历史残留多路径误伤当前会话
    if not db_pid:
        ws_list = [str(w).strip() for w in (payload.get("workspacePaths") or []) if str(w).strip()]
        if ws_list:
            ws_real_paths = list(dict.fromkeys(agy_store.resolve_canonical_path(w)[0] for w in ws_list))
            conflict_paths = [p for p in ws_real_paths if p != real_cwd]
            if conflict_paths:
                conflict_str = ", ".join(str(p) for p in conflict_paths)
                return (
                    f"项目校验失败: 会话工作区与当前目录不一致\n"
                    f"当前目录: {real_cwd}\n"
                    f"冲突工作区: {conflict_str}\n"
                    f"请勿跨项目切换，请在对应项目根目录启动 agy"
                )

    pid = (
        db_pid
        or os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()
        or os.environ.get("AGY_LAUNCH_PROJECT_ID", "").strip()
    )
    if not pid:
        return (
            f"项目校验失败: 未选择项目\n"
            f"当前目录: {real_cwd}\n"
            f"请用 agy / agy proj 选择项目（或 agy proj new 新建），输入 $proj 查看列表"
        )

    projects = agy_store.load_projects()
    proj = projects.get(pid)
    if pid == agy_store.DEFAULT_PROJECT_ID or proj is None:
        proj_label = proj["name"] if proj else pid
        return (
            f"项目校验失败: 未匹配有效项目（当前: {proj_label}）\n"
            f"当前目录: {real_cwd}\n"
            f"请在项目根目录用 agy 选择项目，或输入 $proj 查看列表"
        )

    proj_name = proj["name"]
    if proj_name != dir_name:
        return (
            f"项目校验失败: 目录名与所选项目不一致\n"
            f"当前目录: {real_cwd}\n"
            f"所选项目: {proj_name}\n"
            f"请确认启动目录，或输入 $proj 查看列表"
        )

    bound_real_paths = {
        agy_store.resolve_canonical_path(p)[0] for p in proj.get("paths", []) if p
    }
    if bound_real_paths and real_cwd not in bound_real_paths:
        bound_str = ", ".join(str(p) for p in sorted(bound_real_paths))
        return (
            f"项目校验失败: 当前不在项目绑定根目录\n"
            f"当前目录: {real_cwd}\n"
            f"绑定根目录: {bound_str}\n"
            f"请切换到绑定根目录后启动 agy"
        )

    return None
