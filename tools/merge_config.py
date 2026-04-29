"""Merge/unmerge claude-kakao-notify entries into Claude Code config files.

Usage:
    python merge_config.py settings-add  <settings.json-path>
    python merge_config.py settings-rm   <settings.json-path>
    python merge_config.py mcp-add       <.claude.json-path>  <python-exe-path>
    python merge_config.py mcp-rm        <.claude.json-path>
"""

import json
import os
import sys


HOOK_NAME_MARKER = "notify.py"  # used to identify our hook entries


def load(path: str) -> dict:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(path: str, data: dict) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def hook_command(python_exe: str, notify_py: str, event: str) -> str:
    return f'"{python_exe}" "{notify_py}" {event}'


def settings_add(path: str) -> None:
    home = os.path.expanduser("~")
    notify_py = os.path.join(home, ".claude", "hooks", "notify.py").replace("\\", "/")
    py = sys.executable.replace("\\", "/")

    data = load(path)
    hooks = data.setdefault("hooks", {})
    for event in ("SessionStart", "Stop", "Notification"):
        cmd = hook_command(py, notify_py, event)
        entries = hooks.get(event) or []
        # Drop any pre-existing notify.py entries (re-install replaces).
        cleaned = []
        for entry in entries:
            sub = [h for h in entry.get("hooks", []) if HOOK_NAME_MARKER not in (h.get("command") or "")]
            if sub:
                cleaned.append({"hooks": sub})
        cleaned.append({"hooks": [{"type": "command", "command": cmd}]})
        hooks[event] = cleaned

    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if "mcp__notify__notify" not in allow:
        allow.append("mcp__notify__notify")

    save(path, data)


def settings_rm(path: str) -> None:
    if not os.path.isfile(path):
        return
    data = load(path)

    hooks = data.get("hooks") or {}
    for event in list(hooks.keys()):
        cleaned = []
        for entry in hooks.get(event) or []:
            sub = [h for h in entry.get("hooks", []) if HOOK_NAME_MARKER not in (h.get("command") or "")]
            if sub:
                cleaned.append({"hooks": sub})
        if cleaned:
            hooks[event] = cleaned
        else:
            hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    elif "hooks" in data:
        data.pop("hooks", None)

    perms = data.get("permissions") or {}
    allow = perms.get("allow") or []
    if "mcp__notify__notify" in allow:
        allow = [a for a in allow if a != "mcp__notify__notify"]
        if allow:
            perms["allow"] = allow
        else:
            perms.pop("allow", None)
    if perms:
        data["permissions"] = perms
    elif "permissions" in data:
        data.pop("permissions", None)

    save(path, data)


def mcp_add(path: str, python_exe: str) -> None:
    home = os.path.expanduser("~")
    server_py = os.path.join(home, ".claude", "mcp", "notify-mcp", "server.py")

    data = load(path)
    mcp_servers = data.setdefault("mcpServers", {})
    mcp_servers["notify"] = {
        "type": "stdio",
        "command": python_exe,
        "args": [server_py],
    }
    save(path, data)


def mcp_rm(path: str) -> None:
    if not os.path.isfile(path):
        return
    data = load(path)
    mcp_servers = data.get("mcpServers") or {}
    if "notify" in mcp_servers:
        mcp_servers.pop("notify", None)
        if mcp_servers:
            data["mcpServers"] = mcp_servers
        else:
            data.pop("mcpServers", None)
        save(path, data)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    if cmd == "settings-add":
        settings_add(sys.argv[2])
    elif cmd == "settings-rm":
        settings_rm(sys.argv[2])
    elif cmd == "mcp-add":
        if len(sys.argv) < 4:
            print("mcp-add 는 python-exe 경로 인자 필요", file=sys.stderr)
            return 2
        mcp_add(sys.argv[2], sys.argv[3])
    elif cmd == "mcp-rm":
        mcp_rm(sys.argv[2])
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
