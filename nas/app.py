"""
Claude Code 작업 알림 → 카카오톡 '나에게 보내기' 전달용 작은 HTTP 래퍼.

호스트의 kakao_notify.sh와 같은 토큰 파일을 공유하므로,
토큰 갱신 race 회피를 위해 flock 파일 락을 사용한다.
컨테이너는 --user 1026:100 (parkbohyun:users) 로 실행되어
토큰 파일 atomic replace 시 호스트 소유권을 보존한다.
"""

import os
import json
import time
import fcntl
import logging
from contextlib import contextmanager
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

DATA_DIR = "/data"
CONFIG_PATH = f"{DATA_DIR}/kakao_config.json"
TOKEN_PATH = f"{DATA_DIR}/kakao_token.json"
LOCK_PATH = f"{DATA_DIR}/kakao_token.lock"

API_KEY = os.environ.get("NOTIFY_API_KEY", "").strip()

KAUTH_URL = "https://kauth.kakao.com/oauth/token"
KAPI_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notify-api")

app = FastAPI(title="notify-api", version="1.0.0")


@contextmanager
def token_lock():
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_token_atomic(data: dict) -> None:
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_PATH)


def refresh_access_token(cfg: dict, refresh_token: str) -> dict:
    r = requests.post(
        KAUTH_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "refresh_token": refresh_token,
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def send_kakao(message: str, link_url: Optional[str], button_title: str) -> None:
    cfg = load_json(CONFIG_PATH)
    tok = load_json(TOKEN_PATH)

    template = {
        "object_type": "text",
        "text": message[:1000],
    }
    if link_url:
        template["link"] = {"web_url": link_url, "mobile_web_url": link_url}
        template["button_title"] = (button_title or "열기")[:14]
    else:
        template["link"] = {"web_url": "https://claude.ai/code", "mobile_web_url": "https://claude.ai/code"}

    def _post(access: str) -> requests.Response:
        return requests.post(
            KAPI_SEND_URL,
            headers={"Authorization": f"Bearer {access}"},
            data={"template_object": json.dumps(template, ensure_ascii=False)},
            timeout=10,
        )

    resp = _post(tok["access_token"])
    if resp.status_code == 200:
        return

    log.warning("send failed status=%s body=%s", resp.status_code, resp.text[:300])
    if resp.status_code != 401:
        raise HTTPException(502, f"Kakao send failed: HTTP {resp.status_code} {resp.text[:200]}")

    with token_lock():
        tok_now = load_json(TOKEN_PATH)
        try:
            new = refresh_access_token(cfg, tok_now["refresh_token"])
        except Exception as e:
            raise HTTPException(502, f"Token refresh error: {e}")
        new_access = new.get("access_token")
        if not new_access:
            raise HTTPException(502, f"No access_token in refresh response: {new}")
        tok_now["access_token"] = new_access
        if new.get("refresh_token"):
            tok_now["refresh_token"] = new["refresh_token"]
        tok_now["refreshed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S")
        save_token_atomic(tok_now)
        log.info("access token refreshed")
        access = new_access

    resp2 = _post(access)
    if resp2.status_code == 200:
        return
    raise HTTPException(502, f"Kakao send failed after refresh: HTTP {resp2.status_code} {resp2.text[:200]}")


class NotifyReq(BaseModel):
    message: str = Field(..., min_length=1)
    url: Optional[str] = None
    button: Optional[str] = None


def _check_key(provided: Optional[str]) -> None:
    if not API_KEY:
        return
    if provided != API_KEY:
        raise HTTPException(401, "Invalid API key")


@app.get("/health")
def health():
    ok_cfg = os.path.isfile(CONFIG_PATH)
    ok_tok = os.path.isfile(TOKEN_PATH)
    return {"ok": ok_cfg and ok_tok, "config": ok_cfg, "token": ok_tok}


@app.post("/notify")
def notify(req: NotifyReq, x_api_key: Optional[str] = Header(default=None)):
    _check_key(x_api_key)
    send_kakao(req.message, req.url, req.button or "열기")
    return {"ok": True}
