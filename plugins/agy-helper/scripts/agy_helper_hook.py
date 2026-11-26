#!/usr/bin/env python3
"""agy-helper 插件的 PreInvocation 钩子入口，负责分发各功能模块。"""

import sys

sys.dont_write_bytecode = True

import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_project_guard  # noqa: E402
import agy_show_projects  # noqa: E402


def _terminate_with_message(text: str) -> None:
    print(
        json.dumps(
            {
                "injectSteps": [{"errorMessage": {"userMessage": text}}],
                "terminationBehavior": "terminate",
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        print("{}")
        return 0

    if payload.get("invocationNum", 0) != 0:
        print("{}")
        return 0

    last_input = payload.get("lastUserInput") or ""
    try:
        args = agy_show_projects.parse_trigger(last_input)
    except ValueError as exc:
        _terminate_with_message(f"错误: {exc}\n\n{agy_show_projects.format_help()}")
        return 0

    if args is not None:
        _terminate_with_message(agy_show_projects.handle_proj_args(args))
        return 0

    err = agy_project_guard.validate_project_cwd_consistency(payload)
    if err:
        _terminate_with_message(err)
        return 0

    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
