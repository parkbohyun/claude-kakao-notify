"""Claude Code hook -> NAS notify-api -> KakaoTalk.

사용법: python notify.py <event-name>
event-name: SessionStart | Stop | Notification

환경설정 (`~/.claude/notify-api.env`):
  NOTIFY_API_HOST  — NAS host or DDNS hostname or IP
  NOTIFY_API_PORT  — 외부 포트 (기본 8002)
  NOTIFY_API_KEY   — API 키
  NOTIFY_API_SCHEME — http | https (기본 http)
  NOTIFY_API_PATH   — 엔드포인트 경로 (기본 /notify)
  # 또는 위 대신 NOTIFY_API_URL 단일 변수로 전체 URL 직접 지정 가능
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

EVENT_HEADERS = {
    "SessionStart": ("🚀", "작업 시작"),
    "Stop": ("✅", "작업 완료"),
    "Notification": ("⚠️", "사용자 입력 필요"),
}

DEBUG_DIR = os.path.expanduser("~/.claude/hook-debug")
ENV_FILE = os.path.expanduser("~/.claude/notify-api.env")
DEFAULT_URL = "https://claude.ai/code"
NOTIFICATION_THROTTLE_SEC = 30


def load_env() -> None:
    if not os.path.isfile(ENV_FILE):
        return
    with open(ENV_FILE, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def build_api_url() -> str | None:
    direct = os.environ.get("NOTIFY_API_URL")
    if direct:
        return direct
    host = os.environ.get("NOTIFY_API_HOST")
    if not host:
        return None
    port = os.environ.get("NOTIFY_API_PORT", "8002")
    scheme = os.environ.get("NOTIFY_API_SCHEME", "http")
    path = os.environ.get("NOTIFY_API_PATH", "/notify")
    return f"{scheme}://{host}:{port}{path}"


def dump_stdin(event: str, data) -> None:
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = os.path.join(DEBUG_DIR, f"{event}_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def throttle_ok(event: str, session_id: str) -> bool:
    if event != "Notification":
        return True
    cache = os.path.join(DEBUG_DIR, f".last_notify_{session_id or 'nosid'}")
    now = time.time()
    last = 0.0
    try:
        with open(cache, encoding="utf-8") as f:
            last = float(f.read().strip())
    except Exception:
        pass
    if now - last < NOTIFICATION_THROTTLE_SEC:
        return False
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            f.write(str(now))
    except Exception:
        pass
    return True


def build_message(event: str, data: dict) -> tuple[str, str]:
    icon, title = EVENT_HEADERS.get(event, ("ℹ️", event))
    cwd = data.get("cwd") or ""
    sid = (data.get("session_id") or "")[:8]
    mode = data.get("permission_mode") or ""
    lines = [f"{icon} [Claude Code] {title}"]

    if event == "SessionStart":
        if cwd:
            lines.append(f"📁 {cwd}")
        if sid:
            lines.append(f"🔖 {sid}")
        if mode:
            lines.append(f"🛡 {mode}")
    elif event == "Stop":
        if cwd:
            lines.append(f"📁 {cwd}")
        if sid:
            lines.append(f"🔖 {sid}")
    elif event == "Notification":
        msg = data.get("message") or data.get("notification") or ""
        if msg:
            lines.append(f"💬 {msg}")
        ntype = data.get("type") or ""
        if ntype:
            lines.append(f"🏷 {ntype}")
        if sid:
            lines.append(f"🔖 {sid}")

    text = "\n".join(line for line in lines if line)[:900]
    return text, DEFAULT_URL


def post_notify(message: str, url: str) -> None:
    api_url = build_api_url()
    api_key = os.environ.get("NOTIFY_API_KEY")
    if not (api_url and api_key):
        return
    payload = {"message": message, "url": url, "button": "원격 접속"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        api_url, data=body, method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-API-Key": api_key,
        },
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"notify-hook: {e}", file=sys.stderr)


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw = sys.stdin.read() or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        data = {"_raw_stdin": raw}

    dump_stdin(event, data)

    sid = data.get("session_id") or ""
    if not throttle_ok(event, sid):
        return 0

    load_env()

    text, link = build_message(event, data)
    post_notify(text, link)
    return 0


if __name__ == "__main__":
    sys.exit(main())
