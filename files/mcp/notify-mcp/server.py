"""
notify MCP server (stdio).

NAS의 notify-api 컨테이너로 HTTP POST 하여 카카오톡 '나에게 보내기'를 발송한다.
환경변수는 ~/.claude/notify-api.env 에서 자동 로드한다.

지원 환경변수:
  NOTIFY_API_HOST + NOTIFY_API_PORT (+ optional NOTIFY_API_SCHEME, NOTIFY_API_PATH)
  또는 NOTIFY_API_URL 단일 변수
  NOTIFY_API_KEY (필수)
"""

import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP


def _load_env() -> None:
    path = os.path.expanduser("~/.claude/notify-api.env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _build_api_url() -> str | None:
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


_load_env()
mcp = FastMCP("notify")


@mcp.tool()
def notify(message: str, url: str | None = None, button: str | None = None) -> str:
    """Send a KakaoTalk message to the user via the NAS notify-api gateway.

    Use this when the user has asked to be notified about progress/completion,
    or for important checkpoints during long-running work.

    Args:
        message: 본문. 한글 가능. 최대 1000자.
        url: 메시지에 첨부할 링크. 미지정 시 기본 링크.
        button: CTA 버튼 라벨. 미지정 시 '열기'.

    Returns:
        NAS API의 JSON 응답.
    """
    api_url = _build_api_url()
    api_key = os.environ.get("NOTIFY_API_KEY")
    if not api_url or not api_key:
        raise RuntimeError(
            "NOTIFY_API_HOST/PORT (또는 NOTIFY_API_URL) / NOTIFY_API_KEY 미설정. "
            "~/.claude/notify-api.env 확인."
        )

    payload: dict = {"message": message}
    if url:
        payload["url"] = url
    if button:
        payload["button"] = button

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"


if __name__ == "__main__":
    mcp.run()
