"""
Multi-tenant Kakao notification gateway.

Tenant resolution:
  1. Read /data/tenants.json on each request (mtime-cached).
  2. Hash the incoming X-API-Key with SHA-256.
  3. Match against tenants.json entries to resolve tenant id and data dir.

Legacy single-tenant fallback (backward compatibility):
  If /data/tenants.json is absent, the container falls back to:
    - env NOTIFY_API_KEY for auth
    - /data/kakao_config.json + /data/kakao_token.json for tokens
  This keeps pre-2.0 deployments working unchanged.

Per-tenant token refresh uses a per-tenant flock to avoid cross-tenant
contention while preserving atomic-replace ownership semantics.
"""

import hashlib
import json
import os
import time
import fcntl
import logging
import threading
from contextlib import contextmanager
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

DATA_DIR = "/data"
TENANTS_FILE = f"{DATA_DIR}/tenants.json"
PREFS_FILE = f"{DATA_DIR}/notify_prefs.json"
LEGACY_CONFIG = f"{DATA_DIR}/kakao_config.json"
LEGACY_TOKEN = f"{DATA_DIR}/kakao_token.json"
LEGACY_LOCK = f"{DATA_DIR}/kakao_token.lock"

KAUTH_URL = "https://kauth.kakao.com/oauth/token"
KAPI_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notify-api")

app = FastAPI(title="notify-api", version="2.0.0")


# ─── Tenant cache (mtime-invalidated) ─────────────────────────────────────────

_tenants_cache: dict = {"mtime": -1.0, "data": None}
_tenants_lock = threading.Lock()
_prefs_cache: dict = {"mtime": -1.0, "data": None}
_prefs_lock = threading.Lock()


def _load_tenants() -> Optional[dict]:
    """Return tenants.json content, or None if file is absent (legacy mode)."""
    if not os.path.isfile(TENANTS_FILE):
        return None
    with _tenants_lock:
        try:
            mt = os.path.getmtime(TENANTS_FILE)
        except OSError:
            return None
        if mt == _tenants_cache["mtime"] and _tenants_cache["data"] is not None:
            return _tenants_cache["data"]
        with open(TENANTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _tenants_cache["mtime"] = mt
        _tenants_cache["data"] = data
        return data


def _load_prefs() -> dict:
    """Return notify_prefs.json content, or empty dict if file is absent."""
    if not os.path.isfile(PREFS_FILE):
        return {"prefs": {}}
    with _prefs_lock:
        try:
            mt = os.path.getmtime(PREFS_FILE)
        except OSError:
            return {"prefs": {}}
        if mt == _prefs_cache["mtime"] and _prefs_cache["data"] is not None:
            return _prefs_cache["data"]
        try:
            with open(PREFS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"prefs": {}}
        _prefs_cache["mtime"] = mt
        _prefs_cache["data"] = data
        return data


def is_notification_enabled(tenant_id: str) -> bool:
    """Pref-gating rule:
      - Tenant has no prefs entry → ON (legacy default — unpaired tenant).
      - Tenant has at least one paired bot user with enabled=true → ON.
      - All paired entries enabled=false → OFF (skip send).
    """
    data = _load_prefs()
    entries = [p for p in data.get("prefs", {}).values()
               if p.get("tenant_id") == tenant_id]
    if not entries:
        return True
    return any(p.get("enabled", True) for p in entries)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _tenant_paths(entry: dict) -> dict:
    base = entry.get("data_dir") or os.path.join(DATA_DIR, "tenants", entry["id"])
    return {
        "id": entry["id"],
        "config_path": os.path.join(base, "kakao_config.json"),
        "token_path": os.path.join(base, "kakao_token.json"),
        "lock_path": os.path.join(base, "kakao_token.lock"),
    }


def resolve_tenant(api_key: Optional[str]) -> dict:
    """Resolve tenant by API key. Raises 401/500 on failure."""
    if not api_key:
        raise HTTPException(401, "Missing API key")

    tenants = _load_tenants()
    if tenants is None:
        # Legacy single-tenant
        env_key = os.environ.get("NOTIFY_API_KEY", "").strip()
        if not env_key:
            raise HTTPException(
                500,
                "Server not configured: no /data/tenants.json and no NOTIFY_API_KEY env",
            )
        if api_key != env_key:
            raise HTTPException(401, "Invalid API key")
        return {
            "id": "default",
            "config_path": LEGACY_CONFIG,
            "token_path": LEGACY_TOKEN,
            "lock_path": LEGACY_LOCK,
        }

    # Multi-tenant
    key_hash = _hash_key(api_key)
    for entry in tenants.get("tenants", []):
        if entry.get("api_key_sha256") == key_hash:
            return _tenant_paths(entry)
    raise HTTPException(401, "Invalid API key")


# ─── Token store helpers ──────────────────────────────────────────────────────

@contextmanager
def token_lock(lock_path: str):
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
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


def save_token_atomic(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def refresh_access_token(cfg: dict, refresh_token: str) -> dict:
    payload = {
        "grant_type": "refresh_token",
        "client_id": cfg["client_id"],
        "refresh_token": refresh_token,
    }
    if cfg.get("client_secret"):
        payload["client_secret"] = cfg["client_secret"]
    r = requests.post(KAUTH_URL, data=payload, timeout=10)
    r.raise_for_status()
    return r.json()


# ─── Kakao send ──────────────────────────────────────────────────────────────

def send_kakao(tenant: dict, message: str, link_url: Optional[str], button_title: str) -> None:
    cfg = load_json(tenant["config_path"])
    tok = load_json(tenant["token_path"])

    template = {
        "object_type": "text",
        "text": message[:1000],
    }
    if link_url:
        template["link"] = {"web_url": link_url, "mobile_web_url": link_url}
        template["button_title"] = (button_title or "열기")[:14]
    else:
        template["link"] = {
            "web_url": "https://claude.ai/code",
            "mobile_web_url": "https://claude.ai/code",
        }

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

    log.warning("[%s] send failed status=%s body=%s",
                tenant["id"], resp.status_code, resp.text[:300])
    if resp.status_code != 401:
        raise HTTPException(502, f"Kakao send failed: HTTP {resp.status_code} {resp.text[:200]}")

    # 401 → refresh and retry once
    with token_lock(tenant["lock_path"]):
        tok_now = load_json(tenant["token_path"])
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
        tok_now["refreshed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_token_atomic(tenant["token_path"], tok_now)
        log.info("[%s] access token refreshed", tenant["id"])
        access = new_access

    resp2 = _post(access)
    if resp2.status_code == 200:
        return
    raise HTTPException(
        502,
        f"Kakao send failed after refresh: HTTP {resp2.status_code} {resp2.text[:200]}",
    )


# ─── HTTP API ────────────────────────────────────────────────────────────────

class NotifyReq(BaseModel):
    message: str = Field(..., min_length=1)
    url: Optional[str] = None
    button: Optional[str] = None


@app.get("/health")
def health():
    """Minimal health that doesn't leak tenant identifiers."""
    tenants = _load_tenants()
    if tenants is None:
        return {
            "ok": os.path.isfile(LEGACY_CONFIG) and os.path.isfile(LEGACY_TOKEN),
            "mode": "legacy",
            "config": os.path.isfile(LEGACY_CONFIG),
            "token": os.path.isfile(LEGACY_TOKEN),
        }
    entries = tenants.get("tenants", [])
    healthy = 0
    for entry in entries:
        paths = _tenant_paths(entry)
        if os.path.isfile(paths["config_path"]) and os.path.isfile(paths["token_path"]):
            healthy += 1
    total = len(entries)
    return {
        "ok": total > 0 and healthy == total,
        "mode": "multi-tenant",
        "tenants_total": total,
        "tenants_healthy": healthy,
    }


@app.post("/notify")
def notify(req: NotifyReq, x_api_key: Optional[str] = Header(default=None)):
    tenant = resolve_tenant(x_api_key)
    if not is_notification_enabled(tenant["id"]):
        log.info("[%s] notification skipped — disabled by user prefs", tenant["id"])
        return {"ok": True, "tenant": tenant["id"], "skipped": "notifications_disabled"}
    send_kakao(tenant, req.message, req.url, req.button or "열기")
    return {"ok": True, "tenant": tenant["id"]}
