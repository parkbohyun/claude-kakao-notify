"""신규 가입 / 코드 재요청 / 관리자 승인 흐름의 데이터 모델.

저장소:
  /data/registrations.json   ← 신규 가입 신청 (장기, 7일 보존 후 만료)
  /data/code_requests.json   ← 페어링 코드 재요청 (단기, 30분 보존)

상태 머신 (registrations):
  CREATED        — /가입 으로 챗봇이 만들었으나 사용자가 web form 미진입
  OAUTH_PENDING  — web form 으로 client_id/secret 입력 완료, kakao auth 진행 중
  OAUTH_DONE     — 콜백으로 토큰 교환 성공, 관리자 승인 대기
  APPROVED       — 관리자 승인 완료 → tenant 등록 + 자동 페어링까지 끝
  DENIED         — 관리자 거부
  EXPIRED        — 만료 (7일 동안 OAUTH_DONE 미해소 또는 1시간 동안 CREATED 미해소)

코드 재요청 (code_requests):
  PENDING / APPROVED / DENIED / FULFILLED / EXPIRED
"""
from __future__ import annotations

import json
import os
import secrets
import time
from typing import Optional

DATA_DIR = "/data"
REGISTRATIONS_FILE = os.path.join(DATA_DIR, "registrations.json")
CODE_REQUESTS_FILE = os.path.join(DATA_DIR, "code_requests.json")

# Status constants
ST_CREATED = "created"
ST_OAUTH_PENDING = "oauth_pending"
ST_OAUTH_DONE = "oauth_done"
ST_APPROVED = "approved"
ST_DENIED = "denied"
ST_EXPIRED = "expired"
ST_FULFILLED = "fulfilled"
ST_PENDING = "pending"

# TTL settings (seconds)
REGISTRATION_CREATED_TTL = 60 * 60          # 1 hour to enter web form
REGISTRATION_OAUTH_DONE_TTL = 7 * 24 * 3600 # 7 days for admin to approve
CODE_REQUEST_TTL = 30 * 60                  # 30 minutes


# ─── Atomic JSON IO (chmod 0640 + chown 1026:100 for cross-container reads) ──

def _save_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o640)
    except OSError:
        pass
    try:
        os.chown(tmp, 1026, 100)
    except (OSError, PermissionError):
        pass
    os.replace(tmp, path)


def _load_json(path: str, default: dict) -> dict:
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


# ─── ID / token generation ───────────────────────────────────────────────────

def new_request_id() -> str:
    """22-char URL-safe random ID (122 bits of entropy)."""
    return secrets.token_urlsafe(16)


def new_approver_token() -> str:
    """40-char URL-safe random token, used to gate /approve URL."""
    return secrets.token_urlsafe(30)


def now_ts() -> int:
    return int(time.time())


def iso(ts: Optional[int] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts if ts else now_ts()))


# ─── Registrations ───────────────────────────────────────────────────────────

def load_registrations() -> dict:
    return _load_json(REGISTRATIONS_FILE, {"registrations": {}})


def save_registrations(data: dict) -> None:
    _save_atomic(REGISTRATIONS_FILE, data)


def sweep_registrations(data: dict) -> bool:
    """Mark expired entries as ST_EXPIRED. Returns True if any change."""
    changed = False
    now = now_ts()
    for r in data.get("registrations", {}).values():
        if r.get("status") in (ST_APPROVED, ST_DENIED, ST_EXPIRED):
            continue
        if (r.get("expires_ts") or 0) < now:
            r["status"] = ST_EXPIRED
            r["expired_at"] = iso(now)
            changed = True
    return changed


def find_registration_by_name(data: dict, name: str) -> Optional[dict]:
    """Return the latest non-expired registration with the given name."""
    candidates = [
        r for r in data.get("registrations", {}).values()
        if r.get("name") == name and r.get("status") != ST_EXPIRED
    ]
    if not candidates:
        # also return expired if that's all there is (for status messaging)
        candidates = [
            r for r in data.get("registrations", {}).values()
            if r.get("name") == name
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("created_ts", 0))


def find_registration_by_request_id(data: dict, req_id: str) -> Optional[dict]:
    return data.get("registrations", {}).get(req_id)


def create_registration(name: str, bot_user_id: str) -> dict:
    """Create a new registration in CREATED state."""
    data = load_registrations()
    sweep_registrations(data)

    # If a non-terminal registration with this name exists, expire it
    for r in data.get("registrations", {}).values():
        if r.get("name") == name and r.get("status") in (
                ST_CREATED, ST_OAUTH_PENDING, ST_OAUTH_DONE):
            r["status"] = ST_EXPIRED
            r["expired_at"] = iso()
            r["expired_reason"] = "superseded by new /가입"

    req_id = new_request_id()
    now = now_ts()
    entry = {
        "request_id": req_id,
        "name": name,
        "bot_user_id": bot_user_id,
        "status": ST_CREATED,
        "created_at": iso(now),
        "created_ts": now,
        "expires_at": iso(now + REGISTRATION_CREATED_TTL),
        "expires_ts": now + REGISTRATION_CREATED_TTL,
        "approver_token": new_approver_token(),
        # filled in later
        "client_id": None,
        "client_secret": None,
        "kakao_access_token": None,
        "kakao_refresh_token": None,
        "approved_at": None,
        "approved_api_key": None,
        "approved_pair_code": None,
        "denied_at": None,
        "denied_reason": None,
    }
    data.setdefault("registrations", {})[req_id] = entry
    save_registrations(data)
    return entry


def update_registration(req_id: str, **kwargs) -> Optional[dict]:
    data = load_registrations()
    entry = data.get("registrations", {}).get(req_id)
    if not entry:
        return None
    entry.update(kwargs)
    data["registrations"][req_id] = entry
    save_registrations(data)
    return entry


def transition_registration(req_id: str, new_status: str, **extra) -> Optional[dict]:
    """Update status + extend expiry per state."""
    now = now_ts()
    extras = {"status": new_status, **extra}
    if new_status == ST_OAUTH_PENDING:
        extras["expires_ts"] = now + REGISTRATION_CREATED_TTL  # still need to finish OAuth
        extras["expires_at"] = iso(now + REGISTRATION_CREATED_TTL)
    elif new_status == ST_OAUTH_DONE:
        extras["oauth_done_at"] = iso(now)
        extras["expires_ts"] = now + REGISTRATION_OAUTH_DONE_TTL
        extras["expires_at"] = iso(now + REGISTRATION_OAUTH_DONE_TTL)
    elif new_status == ST_APPROVED:
        extras["approved_at"] = iso(now)
        # keep entry forever (no further expiry)
        extras["expires_ts"] = 0
    elif new_status == ST_DENIED:
        extras["denied_at"] = iso(now)
        extras["expires_ts"] = 0
    return update_registration(req_id, **extras)


# ─── Code requests (재발급) ──────────────────────────────────────────────────

def load_code_requests() -> dict:
    return _load_json(CODE_REQUESTS_FILE, {"requests": {}})


def save_code_requests(data: dict) -> None:
    _save_atomic(CODE_REQUESTS_FILE, data)


def sweep_code_requests(data: dict) -> bool:
    changed = False
    now = now_ts()
    for r in data.get("requests", {}).values():
        if r.get("status") in (ST_APPROVED, ST_DENIED, ST_FULFILLED, ST_EXPIRED):
            continue
        if (r.get("expires_ts") or 0) < now:
            r["status"] = ST_EXPIRED
            changed = True
    return changed


def create_code_request(tenant_id: str, bot_user_id: str) -> dict:
    data = load_code_requests()
    sweep_code_requests(data)

    # Cancel any pending requests for this tenant by this user
    for r in data.get("requests", {}).values():
        if (r.get("tenant_id") == tenant_id and
                r.get("bot_user_id") == bot_user_id and
                r.get("status") == ST_PENDING):
            r["status"] = ST_EXPIRED
            r["expired_reason"] = "superseded by new /코드요청"

    req_id = new_request_id()
    now = now_ts()
    entry = {
        "request_id": req_id,
        "tenant_id": tenant_id,
        "bot_user_id": bot_user_id,
        "status": ST_PENDING,
        "created_at": iso(now),
        "created_ts": now,
        "expires_at": iso(now + CODE_REQUEST_TTL),
        "expires_ts": now + CODE_REQUEST_TTL,
        "approver_token": new_approver_token(),
        "approved_code": None,
        "approved_at": None,
    }
    data.setdefault("requests", {})[req_id] = entry
    save_code_requests(data)
    return entry


def find_code_request_by_id(req_id: str) -> Optional[dict]:
    data = load_code_requests()
    return data.get("requests", {}).get(req_id)


def find_latest_code_request(tenant_id: str, bot_user_id: str) -> Optional[dict]:
    data = load_code_requests()
    candidates = [
        r for r in data.get("requests", {}).values()
        if r.get("tenant_id") == tenant_id and r.get("bot_user_id") == bot_user_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("created_ts", 0))


def update_code_request(req_id: str, **kwargs) -> Optional[dict]:
    data = load_code_requests()
    entry = data.get("requests", {}).get(req_id)
    if not entry:
        return None
    entry.update(kwargs)
    save_code_requests(data)
    return entry
