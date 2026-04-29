"""
카카오톡 i 오픈빌더 Skill 서버.

기능:
  • Docmost 운영 상태 조회 (/status, /info, /version, /backup 등)
  • notify-api 알림 ON/OFF 토글 (/pair, /notify-on, /notify-off, /notify-status)

알림 토글 흐름:
  1) 관리자가 add_tenant.py 로 발급한 페어링 코드를 사용자에게 전달
  2) 사용자가 카카오 채널에서 "/연동 ABC123" 입력 → /pair 핸들러
     → notify_prefs.json 에 { bot_user_id → tenant_id } 저장
     → pair_codes.json 에서 해당 코드 제거
  3) 사용자가 /알림 끄기 → /notify-off → enabled=false
  4) notify-api 가 발송 직전 notify_prefs 체크 → enabled=false 면 스킵

상태 파일 (notify-api 와 공유):
  /data/notify_prefs.json
  /data/pair_codes.json
"""
import datetime
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import onboarding as ob

app = FastAPI()

DATA_DIR = "/data"
PREFS_FILE = os.path.join(DATA_DIR, "notify_prefs.json")
PAIR_FILE = os.path.join(DATA_DIR, "pair_codes.json")
TENANTS_FILE = os.path.join(DATA_DIR, "tenants.json")

DOCMOST_NAME = "docmost_25"

# Public URL for the kakao-skill (used to build OAuth redirect, admin approval, status URLs).
# Set via env. e.g. https://dhub-ds.synology.me:8003
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Notify-api endpoint (in-NAS). Used to push admin notifications.
NOTIFY_API_URL = os.environ.get("NOTIFY_API_URL", "http://172.30.1.77:8002").rstrip("/")
ADMIN_NOTIFY_API_KEY = os.environ.get("ADMIN_NOTIFY_API_KEY", "")
ADMIN_TENANT_ID = os.environ.get("ADMIN_TENANT_ID", "parkbohyun")

# Pair code config (mirrors add_tenant.py)
PAIR_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAIR_CODE_LEN = 6
PAIR_CODE_TTL_SEC = 600

# Username validation (matches add_tenant.py TENANT_ID_RE)
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

KAUTH_AUTHORIZE = "https://kauth.kakao.com/oauth/authorize"
KAUTH_TOKEN = "https://kauth.kakao.com/oauth/token"
OAUTH_SCOPE = "talk_message"

INFO_EXCLUDE_PREFIXES = ()


# ─── Docker helpers ──────────────────────────────────────────────────────────

def docker(*args: str) -> str:
    try:
        out = subprocess.run(
            ["docker", *args],
            capture_output=True, text=True, timeout=6,
        )
        return out.stdout.strip()
    except Exception as e:
        return f"ERR:{e}"


def container_info(name: str) -> dict:
    fmt = "{{.State.Status}}|{{.State.StartedAt}}|{{.Config.Image}}|{{.Image}}"
    raw = docker("inspect", name, "--format", fmt)
    if raw.startswith("ERR") or not raw:
        return {"name": name, "exists": False}
    parts = raw.split("|")
    return {
        "name": name,
        "exists": True,
        "status": parts[0],
        "started_at": parts[1][:19] if len(parts) > 1 else "",
        "image": parts[2] if len(parts) > 2 else "",
        "image_id": parts[3][7:19] if len(parts) > 3 else "",
    }


def list_all_containers() -> list[dict]:
    fmt = "{{.Names}}|{{.State}}|{{.Status}}"
    raw = docker("ps", "-a", "--format", fmt)
    if raw.startswith("ERR") or not raw:
        return []
    items: list[dict] = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) < 2 or not parts[0]:
            continue
        items.append({
            "name": parts[0],
            "state": parts[1],
            "status": parts[2] if len(parts) > 2 else "",
        })
    return items


def state_icon(state: str) -> str:
    if state == "running":
        return "✅"
    if state in ("exited", "dead"):
        return "⏹️"
    if state in ("paused", "restarting", "removing", "created"):
        return "⏸️"
    return "⚠️"


def fetch_docmost_version() -> Optional[str]:
    out = docker("exec", DOCMOST_NAME, "sh", "-c",
                 "cat /app/package.json 2>/dev/null | grep '\"version\"' | head -1")
    if not out or out.startswith("ERR"):
        return None
    try:
        return out.split('"')[3]
    except Exception:
        return None


# ─── Pref / pair-code helpers ───────────────────────────────────────────────

def _load_json(path: str, default: dict) -> dict:
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_atomic(path: str, data: dict) -> None:
    """Write JSON atomically. Mode 0o640 + chown 1026:100 so the notify-api
    container (which runs as 1026:100) can read prefs we write here.
    kakao-skill runs as root because it needs the Docker socket; without the
    explicit chown, notify-api hits PermissionError on the prefs file.
    """
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


def load_prefs() -> dict:
    return _load_json(PREFS_FILE, {"prefs": {}})


def save_prefs(data: dict) -> None:
    _save_atomic(PREFS_FILE, data)


def load_pair_codes() -> dict:
    return _load_json(PAIR_FILE, {"codes": {}})


def save_pair_codes(data: dict) -> None:
    _save_atomic(PAIR_FILE, data)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def get_user_id(payload: dict) -> Optional[str]:
    try:
        return payload["userRequest"]["user"]["id"]
    except (KeyError, TypeError):
        return None


def get_utterance(payload: dict) -> str:
    try:
        return str(payload["userRequest"]["utterance"] or "")
    except (KeyError, TypeError):
        return ""


def get_action_param(payload: dict, key: str) -> Optional[str]:
    """i.kakao 슬롯필링 결과 추출. params 또는 detailParams.<key>.{value|origin} 모두 시도."""
    try:
        action = payload.get("action") or {}
        params = action.get("params") or {}
        v = params.get(key)
        if v:
            return str(v).strip().strip('"')
        detail = action.get("detailParams") or {}
        d = detail.get(key) or {}
        v = d.get("value") or d.get("origin")
        if v:
            return str(v).strip().strip('"')
    except Exception:
        pass
    return None


def extract_pair_code(payload: dict) -> Optional[str]:
    """파라미터 슬롯 우선, 없으면 utterance 에서 정규식 추출."""
    for key in ("code", "sys_text", "페어링코드", "pair_code"):
        v = get_action_param(payload, key)
        if v:
            m = PAIR_CODE_RE.search(v.upper())
            if m:
                return m.group(1)
    m = PAIR_CODE_RE.search(get_utterance(payload).upper())
    if m:
        return m.group(1)
    return None


def find_tenant_for_user(user_id: str) -> Optional[dict]:
    """Return the prefs entry for this bot user, or None."""
    prefs = load_prefs().get("prefs", {})
    return prefs.get(user_id)


# ─── Kakao response builders ─────────────────────────────────────────────────

def kakao_response(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text[:990]}}]},
    }


def kakao_response_card(title: str, description: str, link: Optional[str] = None) -> dict:
    card = {"title": title[:50], "description": description[:400]}
    if link:
        card["buttons"] = [{"label": "열기", "action": "webLink", "webLinkUrl": link}]
    return {"version": "2.0", "template": {"outputs": [{"basicCard": card}]}}


# ─── Health ──────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"ok": True, "ts": datetime.datetime.now().isoformat()}


# ─── Docmost status endpoints ────────────────────────────────────────────────

@app.api_route(methods=["GET", "HEAD", "POST"], path="/status")
async def status(request: Request):
    info = container_info(DOCMOST_NAME)
    if not info["exists"]:
        return kakao_response("⚠️ docmost_25 컨테이너를 찾을 수 없습니다.")

    icon = "✅" if info["status"] == "running" else "⚠️"
    started = info["started_at"].replace("T", " ")
    text = (
        f"{icon} Docmost 상태\n"
        f"━━━━━━━━━━━━━━\n"
        f"상태: {info['status']}\n"
        f"이미지: {info['image']}\n"
        f"이미지ID: {info['image_id']}\n"
        f"시작 시각: {started}"
    )
    return kakao_response(text)


@app.api_route(methods=["GET", "HEAD", "POST"], path="/info")
async def info(request: Request):
    items = list_all_containers()
    if not items:
        return kakao_response("⚠️ 컨테이너 정보를 가져올 수 없습니다.")

    if INFO_EXCLUDE_PREFIXES:
        items = [c for c in items
                 if not c["name"].startswith(INFO_EXCLUDE_PREFIXES)]

    items.sort(key=lambda c: (c["state"] != "running", c["name"].lower()))
    running = sum(1 for c in items if c["state"] == "running")
    total = len(items)

    lines = [
        "📋 컨테이너 상태",
        "━━━━━━━━━━━━━━",
        f"실행 중: {running} / 전체: {total}",
        "",
    ]
    for c in items:
        icon = state_icon(c["state"])
        name = c["name"][:28]
        lines.append(f"{icon} {name}: {c['state']}")

    version = fetch_docmost_version()
    if version:
        lines.append("")
        lines.append(f"📦 docmost ver: {version}")
    return kakao_response("\n".join(lines))


@app.api_route(methods=["GET", "HEAD", "POST"], path="/version")
async def version(request: Request):
    info = container_info(DOCMOST_NAME)
    version = fetch_docmost_version() or "(알 수 없음)"
    text = (
        f"📦 Docmost 버전\n"
        f"━━━━━━━━━━━━━━\n"
        f"버전: {version}\n"
        f"이미지: {info.get('image', '?')}\n"
        f"이미지ID: {info.get('image_id', '?')}"
    )
    return kakao_response(text)


@app.api_route(methods=["GET", "HEAD", "POST"], path="/backup")
async def backup_status(request: Request):
    backup_root = "/data/backups/docmost"
    try:
        entries = sorted(os.listdir(backup_root), reverse=True)
        entries = [e for e in entries if e[0].isdigit()][:5]
        if not entries:
            return kakao_response("📂 백업 기록이 없습니다.")

        lines = ["📂 최근 백업 (최신순)", "━━━━━━━━━━━━━━"]
        for e in entries:
            path = os.path.join(backup_root, e)
            try:
                size = sum(os.path.getsize(os.path.join(path, f))
                           for f in os.listdir(path)) // (1024 * 1024)
                lines.append(f"• {e}  ({size}MB)")
            except Exception:
                lines.append(f"• {e}")
        return kakao_response("\n".join(lines))
    except Exception as e:
        return kakao_response(f"백업 정보 조회 실패: {e}")


# ─── Notification toggle endpoints ───────────────────────────────────────────

PAIR_CODE_RE = re.compile(r"\b([A-Z0-9]{6})\b")


@app.api_route(methods=["GET", "HEAD", "POST"], path="/pair")
async def pair(request: Request):
    """/연동 <CODE> — bot user_id ↔ tenant_id 연결."""
    if request.method == "GET":
        return kakao_response("/연동 <코드> 로 페어링합니다.")
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")

    user_id = get_user_id(payload)
    if not user_id:
        return kakao_response("⚠️ 사용자 식별 실패.")

    code = extract_pair_code(payload)
    if not code:
        return kakao_response(
            "🔑 페어링 코드를 입력해주세요.\n"
            "예: /연동 ABC123\n\n"
            "코드는 NAS 관리자에게서 받은 6자리 영문/숫자입니다."
        )

    codes_data = load_pair_codes()
    codes = codes_data.get("codes", {})
    entry = codes.get(code)
    now = int(time.time())

    if not entry or entry.get("expires_ts", 0) < now:
        # opportunistic sweep of expired entries
        codes_data["codes"] = {
            c: meta for c, meta in codes.items()
            if meta.get("expires_ts", 0) >= now
        }
        save_pair_codes(codes_data)
        return kakao_response(
            "❌ 코드가 만료되었거나 잘못되었습니다.\n"
            "관리자에게 새 코드를 요청해주세요."
        )

    tenant_id = entry["tenant_id"]

    # 연결 등록 (동일 user_id 가 다른 tenant 로 재연결하면 덮어씀)
    prefs_data = load_prefs()
    prefs = prefs_data.setdefault("prefs", {})
    prev = prefs.get(user_id, {})
    prefs[user_id] = {
        "tenant_id": tenant_id,
        "enabled": True,
        "linked_at": prev.get("linked_at") or now_iso(),
        "updated_at": now_iso(),
    }
    save_prefs(prefs_data)

    # 사용한 코드는 제거
    codes.pop(code, None)
    codes_data["codes"] = codes
    save_pair_codes(codes_data)

    return kakao_response(
        f"✅ 연동 완료\n"
        f"━━━━━━━━━━━━━━\n"
        f"테넌트: {tenant_id}\n"
        f"알림 상태: ON\n\n"
        f"명령어:\n"
        f"  /알림 켜기  → 알림 ON\n"
        f"  /알림 끄기  → 알림 OFF\n"
        f"  /알림 상태  → 현재 상태 확인"
    )


def _set_enabled(payload: dict, enabled: bool) -> dict:
    user_id = get_user_id(payload)
    if not user_id:
        return kakao_response("⚠️ 사용자 식별 실패.")

    prefs_data = load_prefs()
    prefs = prefs_data.setdefault("prefs", {})
    entry = prefs.get(user_id)
    if not entry:
        return kakao_response(
            "🔗 먼저 페어링이 필요합니다.\n"
            "관리자에게 받은 코드로:\n"
            "  /연동 <코드>"
        )

    entry["enabled"] = enabled
    entry["updated_at"] = now_iso()
    save_prefs(prefs_data)

    state = "ON ✅" if enabled else "OFF ⏸️"
    return kakao_response(
        f"🔔 알림 {state}\n"
        f"━━━━━━━━━━━━━━\n"
        f"테넌트: {entry['tenant_id']}\n"
        f"변경 시각: {entry['updated_at']}"
    )


@app.api_route(methods=["GET", "HEAD", "POST"], path="/notify-on")
async def notify_on(request: Request):
    if request.method == "GET":
        return kakao_response("/알림 켜기 — 카카오톡 알림을 ON 합니다.")
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")
    return _set_enabled(payload, True)


@app.api_route(methods=["GET", "HEAD", "POST"], path="/notify-off")
async def notify_off(request: Request):
    if request.method == "GET":
        return kakao_response("/알림 끄기 — 카카오톡 알림을 OFF 합니다.")
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")
    return _set_enabled(payload, False)


@app.api_route(methods=["GET", "HEAD", "POST"], path="/notify-status")
async def notify_status(request: Request):
    if request.method == "GET":
        return kakao_response("/알림 상태 — 현재 ON/OFF 상태를 조회합니다.")
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")

    user_id = get_user_id(payload)
    if not user_id:
        return kakao_response("⚠️ 사용자 식별 실패.")

    entry = find_tenant_for_user(user_id)
    if not entry:
        return kakao_response(
            "🔗 아직 페어링되지 않았습니다.\n"
            "관리자에게 받은 코드로:\n"
            "  /연동 <코드>"
        )

    state = "ON ✅" if entry.get("enabled", True) else "OFF ⏸️"
    return kakao_response(
        f"🔔 알림 상태\n"
        f"━━━━━━━━━━━━━━\n"
        f"테넌트: {entry['tenant_id']}\n"
        f"상태: {state}\n"
        f"연동 시각: {entry.get('linked_at', '?')}\n"
        f"최근 변경: {entry.get('updated_at', '?')}"
    )


# ─── Help / welcome ──────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 NAS 알림 봇\n"
    "━━━━━━━━━━━━━━\n"
    "📌 컨테이너 / Docmost\n"
    "  /상태   docmost 작동 여부\n"
    "  /정보   전체 컨테이너 + 버전 요약\n"
    "  /버전   docmost 버전\n"
    "  /백업   최근 백업 목록\n\n"
    "📌 알림 토글 (Claude Code 알림)\n"
    "  /연동 <코드>  최초 1회 페어링\n"
    "  /알림 켜기   알림 ON\n"
    "  /알림 끄기   알림 OFF\n"
    "  /알림 상태   현재 상태\n\n"
    "  /도움   이 도움말 다시 보기"
)


@app.api_route(methods=["GET", "HEAD", "POST"], path="/help")
async def help_(request: Request):
    return kakao_response(HELP_TEXT)


@app.api_route(methods=["GET", "HEAD", "POST"], path="/guide")
async def guide(request: Request):
    return kakao_response(HELP_TEXT)


@app.api_route(methods=["GET", "HEAD", "POST"], path="/welcome")
async def welcome(request: Request):
    text = (
        "👋 안녕하세요! NAS 알림 봇입니다.\n"
        "━━━━━━━━━━━━━━\n"
        "운영 상태 확인 + Claude Code 알림\n"
        "ON/OFF 토글이 가능합니다.\n\n"
        "📌 /상태  /정보  /버전  /백업\n"
        "📌 /연동  /알림 켜기  /알림 끄기  /알림 상태\n"
        "📌 /가입  /가입상태  /코드요청  /코드확인\n\n"
        "처음이라면 /도움 부터 시작하세요 😊"
    )
    return kakao_response(text)


# ═══════════════════════════════════════════════════════════════════════════
#  Onboarding flow — chatbot-driven registration with admin approval
# ═══════════════════════════════════════════════════════════════════════════

# ─── Helper: extract name arg from utterance / params ────────────────────────

NAME_FROM_UTTER_RE = re.compile(r"^\s*/?[가-힣\w]+\s+(\S+)")


def extract_name_arg(payload: dict) -> Optional[str]:
    """Pull a username argument from kakao chatbot payload.
    Tries slot params first, then parses utterance ('/<cmd> <name>')."""
    for key in ("name", "username", "user_name", "tenant", "tenant_id",
                "sys_text", "code"):
        v = get_action_param(payload, key)
        if v and NAME_RE.match(v):
            return v
    utterance = get_utterance(payload).strip()
    m = NAME_FROM_UTTER_RE.match(utterance)
    if m:
        candidate = m.group(1)
        if NAME_RE.match(candidate):
            return candidate
    return None


# ─── Tenant registry helpers (read-only — writes happen in approval) ─────────

def load_tenants() -> dict:
    return _load_json(TENANTS_FILE, {"tenants": []})


def save_tenants(data: dict) -> None:
    _save_atomic(TENANTS_FILE, data)


def tenant_exists(name: str) -> bool:
    for t in load_tenants().get("tenants", []):
        if t.get("id") == name:
            return True
    return False


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ─── Pair code generation (mirrors add_tenant.generate_pair_code) ────────────

def issue_pair_code(tenant_id: str) -> tuple[str, str]:
    """Generate a fresh pair code in pair_codes.json. Returns (code, expires_at)."""
    data = load_pair_codes()
    codes = data.get("codes", {})
    now = int(time.time())
    codes = {
        c: meta for c, meta in codes.items()
        if meta.get("expires_ts", 0) > now and meta.get("tenant_id") != tenant_id
    }
    for _ in range(20):
        code = "".join(secrets.choice(PAIR_CODE_CHARS) for _ in range(PAIR_CODE_LEN))
        if code not in codes:
            break
    else:
        raise RuntimeError("failed to allocate unique pair code")
    expires_ts = now + PAIR_CODE_TTL_SEC
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(expires_ts))
    codes[code] = {
        "tenant_id": tenant_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "expires_at": expires_at,
        "expires_ts": expires_ts,
    }
    data["codes"] = codes
    save_pair_codes(data)
    return code, expires_at


# ─── Admin push notifications (via notify-api) ───────────────────────────────

def send_admin_push(message: str, url: Optional[str] = None,
                    button: str = "열기") -> bool:
    """Push a notification to the admin's KakaoTalk via notify-api.
    Returns True on success. Failures are logged but never raise."""
    if not ADMIN_NOTIFY_API_KEY:
        print(f"[admin-push] ADMIN_NOTIFY_API_KEY not set — skipping. "
              f"would have sent: {message[:80]!r}")
        return False
    body = {"message": message[:990], "button": button}
    if url:
        body["url"] = url
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{NOTIFY_API_URL}/notify",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-API-Key": ADMIN_NOTIFY_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[admin-push] failed: {e}")
        return False


# ─── Chatbot handlers — registration ─────────────────────────────────────────

@app.api_route(methods=["GET", "HEAD", "POST"], path="/register-start")
async def register_start(request: Request):
    """`/가입 <name>` — 신규 가입 신청 시작. Web URL 반환."""
    if request.method == "GET":
        return kakao_response("/가입 <사용자명> 으로 가입 신청을 시작합니다.")
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")

    user_id = get_user_id(payload)
    if not user_id:
        return kakao_response("⚠️ 사용자 식별 실패.")

    name = extract_name_arg(payload)
    if not name:
        return kakao_response(
            "🆕 가입 시작\n"
            "━━━━━━━━━━━━━━\n"
            "사용자 이름을 함께 입력해주세요.\n"
            "예: /가입 jypark\n\n"
            "이름 규칙: 영문/숫자/_/- 1~64자"
        )

    if tenant_exists(name):
        return kakao_response(
            f"⚠️ '{name}' 은(는) 이미 등록된 사용자입니다.\n"
            f"다른 이름을 사용하거나 관리자에게 문의하세요."
        )

    if not PUBLIC_BASE_URL:
        return kakao_response("⚠️ 서버 설정 오류 — 관리자에게 문의 (PUBLIC_BASE_URL).")

    entry = ob.create_registration(name, user_id)
    register_url = f"{PUBLIC_BASE_URL}/register?req={entry['request_id']}"
    return kakao_response(
        f"🆕 가입 시작 — '{name}'\n"
        f"━━━━━━━━━━━━━━\n"
        f"아래 URL 을 브라우저로 여세요:\n\n"
        f"{register_url}\n\n"
        f"카카오 앱 정보 입력 → 카카오 로그인 → 동의 →\n"
        f"관리자 승인 대기.\n\n"
        f"진행 상황: /가입상태 {name}\n"
        f"⏰ 1시간 안에 진행하지 않으면 만료됩니다."
    )


@app.api_route(methods=["GET", "HEAD", "POST"], path="/register-status")
async def register_status(request: Request):
    """`/가입상태 <name>` — 가입 진행/결과 조회. 본인 요청만 응답."""
    if request.method == "GET":
        return kakao_response("/가입상태 <사용자명> 으로 진행 상황을 조회합니다.")
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")

    user_id = get_user_id(payload)
    if not user_id:
        return kakao_response("⚠️ 사용자 식별 실패.")

    name = extract_name_arg(payload)
    if not name:
        return kakao_response(
            "조회할 사용자 이름을 입력해주세요.\n"
            "예: /가입상태 jypark"
        )

    data = ob.load_registrations()
    ob.sweep_registrations(data)
    ob.save_registrations(data)
    entry = ob.find_registration_by_name(data, name)
    if not entry:
        return kakao_response(
            f"📭 '{name}' 에 대한 가입 신청을 찾지 못했습니다.\n"
            f"먼저 /가입 {name} 으로 신청하세요."
        )

    # Caller must be the originator
    if entry.get("bot_user_id") != user_id:
        return kakao_response(
            "⛔ 본인이 신청한 가입만 조회 가능합니다."
        )

    status = entry.get("status")
    base = f"📋 가입 상태 — {name}\n━━━━━━━━━━━━━━\n"
    if status == ob.ST_CREATED:
        return kakao_response(
            base +
            f"상태: 입력 대기\n"
            f"신청 시각: {entry['created_at']}\n\n"
            f"브라우저에서 카카오 앱 정보를 입력하세요:\n"
            f"{PUBLIC_BASE_URL}/register?req={entry['request_id']}"
        )
    if status == ob.ST_OAUTH_PENDING:
        return kakao_response(
            base +
            f"상태: 카카오 로그인 진행 중\n"
            f"브라우저로 돌아가서 카카오 로그인을 완료하세요.\n"
            f"{PUBLIC_BASE_URL}/register?req={entry['request_id']}"
        )
    if status == ob.ST_OAUTH_DONE:
        return kakao_response(
            base +
            f"상태: ⏳ 관리자 승인 대기\n"
            f"OAuth 완료 시각: {entry.get('oauth_done_at', '?')}\n\n"
            f"승인 후 다시 /가입상태 {name} 입력하세요."
        )
    if status == ob.ST_APPROVED:
        api_key = entry.get('approved_api_key', '?')
        pair_code = entry.get('approved_pair_code', '(없음)')
        return kakao_response(
            base +
            f"상태: ✅ 승인 완료\n"
            f"승인 시각: {entry.get('approved_at', '?')}\n\n"
            f"━ API 키 (PC 클라이언트용) ━\n"
            f"{api_key}\n\n"
            f"━ 페어링 코드 (다른 디바이스용, 10분) ━\n"
            f"{pair_code}\n\n"
            f"PC 설치:\n"
            f"git clone https://github.com/parkbohyun/claude-kakao-notify\n"
            f"cd claude-kakao-notify\n"
            f"powershell -ExecutionPolicy Bypass -File .\\install.ps1\n\n"
            f"host: dhub-ds.synology.me / port: 8003"
        )
    if status == ob.ST_DENIED:
        return kakao_response(
            base +
            f"상태: ❌ 거부됨\n"
            f"사유: {entry.get('denied_reason', '(없음)')}"
        )
    if status == ob.ST_EXPIRED:
        return kakao_response(
            base +
            f"상태: ⌛ 만료됨\n"
            f"새로 시작: /가입 {name}"
        )
    return kakao_response(base + f"상태: {status}")


# ─── Chatbot handlers — code re-request ──────────────────────────────────────

@app.api_route(methods=["GET", "HEAD", "POST"], path="/code-request")
async def code_request(request: Request):
    """`/코드요청 <name>` — 페어링 코드 재발급 요청 (관리자 승인 필요)."""
    if request.method == "GET":
        return kakao_response("/코드요청 <사용자명> 으로 페어링 코드 재발급을 신청합니다.")
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")
    user_id = get_user_id(payload)
    if not user_id:
        return kakao_response("⚠️ 사용자 식별 실패.")

    name = extract_name_arg(payload)
    if not name:
        return kakao_response(
            "사용자 이름을 함께 입력해주세요.\n"
            "예: /코드요청 jypark"
        )

    if not tenant_exists(name):
        return kakao_response(
            f"⚠️ '{name}' 은(는) 등록된 사용자가 아닙니다.\n"
            f"먼저 /가입 {name} 으로 신청하세요."
        )
    if not PUBLIC_BASE_URL:
        return kakao_response("⚠️ 서버 설정 오류 — 관리자에게 문의 (PUBLIC_BASE_URL).")

    entry = ob.create_code_request(name, user_id)
    approve_url = (
        f"{PUBLIC_BASE_URL}/code-request/approve"
        f"?req={entry['request_id']}&token={entry['approver_token']}"
    )
    status_url = f"{PUBLIC_BASE_URL}/code-request/status?req={entry['request_id']}"

    send_admin_push(
        message=(
            f"🔑 [코드 재발급 요청] {name}\n"
            f"━━━━━━━━━━━━━━\n"
            f"신청 시각: {entry['created_at']}\n"
            f"승인 페이지로 이동하여 처리하세요."
        ),
        url=approve_url,
        button="승인 페이지",
    )

    return kakao_response(
        f"📩 코드 재발급 요청\n"
        f"━━━━━━━━━━━━━━\n"
        f"사용자: {name}\n"
        f"상태: 관리자 승인 대기 중\n\n"
        f"진행 상황: /코드확인 {name}\n"
        f"또는 브라우저에서:\n"
        f"{status_url}\n\n"
        f"⏰ 30분 안에 승인되어야 합니다."
    )


@app.api_route(methods=["GET", "HEAD", "POST"], path="/code-check")
async def code_check(request: Request):
    """`/코드확인 <name>` — 코드 재발급 결과 확인."""
    if request.method == "GET":
        return kakao_response("/코드확인 <사용자명> 으로 코드 발급 여부를 확인합니다.")
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")
    user_id = get_user_id(payload)
    if not user_id:
        return kakao_response("⚠️ 사용자 식별 실패.")

    name = extract_name_arg(payload)
    if not name:
        return kakao_response(
            "사용자 이름을 함께 입력해주세요.\n"
            "예: /코드확인 jypark"
        )

    entry = ob.find_latest_code_request(name, user_id)
    if not entry:
        return kakao_response(
            f"📭 '{name}' 에 대한 최근 코드 요청이 없습니다.\n"
            f"/코드요청 {name} 으로 신청하세요."
        )
    status = entry.get("status")
    base = f"🔑 코드 요청 상태 — {name}\n━━━━━━━━━━━━━━\n"
    if status == ob.ST_PENDING:
        return kakao_response(
            base + f"상태: ⏳ 관리자 승인 대기\n"
                   f"신청: {entry['created_at']}\n"
                   f"만료: {entry['expires_at']}"
        )
    if status == ob.ST_APPROVED:
        # consume — mark fulfilled
        ob.update_code_request(entry["request_id"],
                               status=ob.ST_FULFILLED,
                               fulfilled_at=ob.iso())
        return kakao_response(
            base + f"상태: ✅ 발급 완료\n"
                   f"코드: {entry.get('approved_code', '?')}\n\n"
                   f"카카오 채널에서:\n"
                   f"  /연동 {entry.get('approved_code', '<코드>')}"
        )
    if status == ob.ST_FULFILLED:
        return kakao_response(
            base + f"상태: 이미 수령된 코드\n"
                   f"코드: {entry.get('approved_code', '?')}\n\n"
                   f"이미 받은 코드를 그대로 사용하세요.\n"
                   f"새 코드가 필요하면 /코드요청 {name}"
        )
    if status == ob.ST_DENIED:
        return kakao_response(base + "상태: ❌ 거부됨")
    if status == ob.ST_EXPIRED:
        return kakao_response(
            base + f"상태: ⌛ 만료됨\n새로 신청: /코드요청 {name}"
        )
    return kakao_response(base + f"상태: {status}")


# ═══════════════════════════════════════════════════════════════════════════
#  Web routes — registration form, OAuth callback, admin approval pages
# ═══════════════════════════════════════════════════════════════════════════

def _html_page(title: str, body_html: str, status: int = 200) -> HTMLResponse:
    page = (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<style>"
        "body{font-family:system-ui,sans-serif;max-width:520px;margin:2rem auto;padding:1rem;color:#222}"
        "h1{font-size:1.4rem;border-bottom:2px solid #ddd;padding-bottom:.4rem}"
        "label{display:block;margin-top:1rem;font-weight:600}"
        "input[type=text],input[type=password]{width:100%;padding:.5rem;font-size:1rem;border:1px solid #bbb;border-radius:4px;box-sizing:border-box}"
        ".btn{display:inline-block;padding:.6rem 1.2rem;font-size:1rem;border:0;border-radius:4px;cursor:pointer;margin-top:1rem;margin-right:.5rem}"
        ".btn-primary{background:#3a7afe;color:#fff}"
        ".btn-success{background:#28a745;color:#fff}"
        ".btn-danger{background:#dc3545;color:#fff}"
        ".muted{color:#666;font-size:.9rem}"
        ".card{background:#f7f8fa;border:1px solid #ddd;border-radius:6px;padding:1rem;margin-top:1rem}"
        ".key{font-family:monospace;background:#fff;padding:.4rem;border:1px solid #ccc;word-break:break-all;display:block;margin-top:.4rem}"
        ".ok{color:#28a745}.err{color:#dc3545}.warn{color:#e0a000}"
        "</style></head><body>"
        + body_html +
        "</body></html>"
    )
    return HTMLResponse(content=page, status_code=status)


@app.get("/register")
async def register_page(req: str = ""):
    if not req:
        return _html_page("등록", "<h1>잘못된 접근</h1><p>req 파라미터가 필요합니다.</p>", 400)
    data = ob.load_registrations()
    ob.sweep_registrations(data)
    ob.save_registrations(data)
    entry = ob.find_registration_by_request_id(data, req)
    if not entry:
        return _html_page("등록", "<h1>요청 없음</h1><p>유효하지 않거나 만료된 요청입니다.</p>", 404)

    status = entry["status"]
    name = html.escape(entry["name"])

    if status == ob.ST_EXPIRED:
        return _html_page("등록", f"<h1>만료됨</h1><p>'{name}' 신청이 만료되었습니다. 카카오 채널에서 /가입 {name} 으로 새로 신청하세요.</p>", 410)

    if status == ob.ST_OAUTH_DONE:
        return _html_page("등록 완료", f"<h1>✅ 등록 완료 — {name}</h1>"
                          "<div class='card'>OAuth 토큰까지 발급되었습니다. <strong>관리자 승인 대기 중</strong>입니다.<br>"
                          f"카카오 채널에서 <code>/가입상태 {name}</code> 를 입력해 진행 상황을 확인하세요.</div>")

    if status == ob.ST_APPROVED:
        return _html_page("등록 완료",
                          f"<h1>✅ 승인 완료 — {name}</h1>"
                          f"<div class='card'>카카오 채널에서 <code>/가입상태 {name}</code> 를 입력해 API 키와 설치 명령을 받으세요.</div>")

    if status == ob.ST_DENIED:
        return _html_page("등록 거부",
                          f"<h1 class='err'>❌ 거부됨 — {name}</h1>"
                          f"<p>{html.escape(entry.get('denied_reason') or '관리자가 거부했습니다.')}</p>")

    # CREATED or OAUTH_PENDING: show form
    body = f"""
    <h1>카카오 앱 정보 입력 — {name}</h1>
    <p class="muted">이 페이지에서 입력한 값은 카카오톡으로 전송되지 않으며 NAS 서버에만 저장됩니다.</p>
    <form method="post" action="/register/submit">
      <input type="hidden" name="req" value="{html.escape(req)}">
      <label>REST API key <span class="muted">(client_id)</span></label>
      <input type="text" name="client_id" required autocomplete="off"
             placeholder="d3e2454f...">
      <label>Client Secret <span class="muted">(없으면 비워두세요)</span></label>
      <input type="password" name="client_secret" autocomplete="off"
             placeholder="(선택)">
      <button class="btn btn-primary" type="submit">다음 → 카카오 로그인</button>
    </form>
    <div class="card muted">
      <strong>카카오 개발자 콘솔 설정 확인</strong><br>
      • [카카오 로그인 → 활성화] ON<br>
      • [카카오 로그인 → Redirect URI] 에 다음 등록:<br>
      <span class="key">{html.escape(PUBLIC_BASE_URL)}/oauth/callback</span>
      • [동의항목 → 카카오톡 메시지 전송 (talk_message)] 활성화<br>
      • [팀 관리] 에 본인 카카오 계정 추가<br>
    </div>
    """
    return _html_page("카카오 앱 등록", body)


@app.post("/register/submit")
async def register_submit(
    req: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(""),
):
    data = ob.load_registrations()
    ob.sweep_registrations(data)
    ob.save_registrations(data)
    entry = ob.find_registration_by_request_id(data, req)
    if not entry or entry["status"] in (ob.ST_EXPIRED, ob.ST_DENIED):
        return _html_page("오류", "<h1>요청 없음</h1>", 404)
    if entry["status"] in (ob.ST_OAUTH_DONE, ob.ST_APPROVED):
        return RedirectResponse(f"/register?req={req}", status_code=303)

    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id:
        return _html_page("오류", "<h1>입력 오류</h1><p>REST API key 가 필요합니다.</p>", 400)

    ob.transition_registration(req,
                               ob.ST_OAUTH_PENDING,
                               client_id=client_id,
                               client_secret=client_secret or None)

    redirect_uri = f"{PUBLIC_BASE_URL}/oauth/callback"
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": OAUTH_SCOPE,
        "state": req,
    }
    auth_url = f"{KAUTH_AUTHORIZE}?{urllib.parse.urlencode(auth_params)}"
    return RedirectResponse(auth_url, status_code=303)


@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "",
                         error: str = "", error_description: str = ""):
    if error:
        return _html_page("OAuth 오류",
                          f"<h1 class='err'>OAuth 실패</h1>"
                          f"<p>{html.escape(error)}: {html.escape(error_description)}</p>",
                          400)
    if not code or not state:
        return _html_page("OAuth 오류",
                          "<h1 class='err'>잘못된 콜백</h1><p>code/state 누락.</p>",
                          400)

    data = ob.load_registrations()
    entry = data.get("registrations", {}).get(state)
    if not entry:
        return _html_page("OAuth 오류",
                          "<h1 class='err'>요청 없음</h1><p>state 매칭 실패.</p>",
                          404)
    if entry["status"] != ob.ST_OAUTH_PENDING:
        return _html_page("OAuth 오류",
                          f"<h1 class='warn'>이미 처리됨 ({entry['status']})</h1>",
                          409)

    cfg_client_id = entry.get("client_id")
    cfg_client_secret = entry.get("client_secret")
    redirect_uri = f"{PUBLIC_BASE_URL}/oauth/callback"

    body = {
        "grant_type": "authorization_code",
        "client_id": cfg_client_id,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if cfg_client_secret:
        body["client_secret"] = cfg_client_secret
    body_data = urllib.parse.urlencode(body).encode("utf-8")
    req_obj = urllib.request.Request(
        KAUTH_TOKEN, data=body_data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req_obj, timeout=10) as resp:
            tok = json.load(resp)
    except Exception as e:
        return _html_page("OAuth 오류",
                          f"<h1 class='err'>토큰 교환 실패</h1>"
                          f"<p>{html.escape(str(e))}</p>", 502)

    if "access_token" not in tok or "refresh_token" not in tok:
        return _html_page("OAuth 오류",
                          f"<h1 class='err'>토큰 응답 누락</h1>"
                          f"<pre>{html.escape(json.dumps(tok, ensure_ascii=False))}</pre>", 502)

    ob.transition_registration(
        state, ob.ST_OAUTH_DONE,
        kakao_access_token=tok["access_token"],
        kakao_refresh_token=tok["refresh_token"],
        kakao_token_full=tok,
    )

    name = entry["name"]
    approve_url = (
        f"{PUBLIC_BASE_URL}/approve"
        f"?req={state}&token={entry['approver_token']}"
    )
    send_admin_push(
        message=(
            f"🆕 [신규 가입 신청] {name}\n"
            f"━━━━━━━━━━━━━━\n"
            f"OAuth 완료. 승인 페이지로 이동:"
        ),
        url=approve_url,
        button="승인 페이지",
    )

    return _html_page("OAuth 완료",
                      f"<h1 class='ok'>✅ OAuth 완료 — {html.escape(name)}</h1>"
                      f"<div class='card'>카카오 토큰이 발급되었고 관리자에게 승인 요청을 전송했습니다.<br>"
                      f"카카오 채널에서 <code>/가입상태 {html.escape(name)}</code> 를 입력해 진행 상황을 확인하세요.</div>")


@app.get("/approve")
async def approve_page(req: str = "", token: str = ""):
    """관리자가 카톡 push URL 클릭 시 보는 승인/거부 페이지."""
    data = ob.load_registrations()
    ob.sweep_registrations(data)
    ob.save_registrations(data)
    entry = ob.find_registration_by_request_id(data, req)
    if not entry or entry.get("approver_token") != token:
        return _html_page("승인", "<h1>잘못된 접근</h1>", 403)

    if entry["status"] == ob.ST_APPROVED:
        api_key = html.escape(entry.get("approved_api_key", ""))
        pair_code = html.escape(entry.get("approved_pair_code", ""))
        body = (
            f"<h1 class='ok'>✅ 이미 승인됨 — {html.escape(entry['name'])}</h1>"
            f"<div class='card'>API 키:<span class='key'>{api_key}</span>"
            f"페어링 코드:<span class='key'>{pair_code}</span></div>"
        )
        return _html_page("승인", body)

    if entry["status"] == ob.ST_DENIED:
        return _html_page("승인",
                          f"<h1 class='err'>❌ 이미 거부됨 — {html.escape(entry['name'])}</h1>"
                          f"<p>{html.escape(entry.get('denied_reason') or '')}</p>")

    if entry["status"] != ob.ST_OAUTH_DONE:
        return _html_page("승인",
                          f"<h1 class='warn'>아직 승인 가능 상태가 아님 ({html.escape(entry['status'])})</h1>"
                          "<p>사용자가 OAuth 까지 완료해야 승인할 수 있습니다.</p>", 409)

    name = html.escape(entry["name"])
    body = f"""
    <h1>🆕 신규 가입 승인 — {name}</h1>
    <div class="card">
      <div><strong>사용자명:</strong> {name}</div>
      <div><strong>OAuth 완료:</strong> {html.escape(entry.get('oauth_done_at', '?'))}</div>
      <div><strong>요청 시각:</strong> {html.escape(entry.get('created_at', '?'))}</div>
      <div><strong>client_id:</strong> <span class="key">{html.escape(entry.get('client_id', ''))}</span></div>
      <div><strong>client_secret:</strong> {('있음' if entry.get('client_secret') else '없음')}</div>
    </div>
    <p>승인 시 자동 처리:</p>
    <ul>
      <li>kakao_config.json + kakao_token.json 을 <code>/data/tenants/{name}/</code> 에 저장</li>
      <li>tenants.json 에 새 엔트리 추가 + 새 API 키 발급</li>
      <li>요청 사용자 (<code>{html.escape(entry.get('bot_user_id', '')[:16])}...</code>) 자동 페어링</li>
      <li>다른 디바이스용 페어링 코드도 별도 발급 (10분)</li>
    </ul>
    <form method="post" action="/approve/action">
      <input type="hidden" name="req" value="{html.escape(req)}">
      <input type="hidden" name="token" value="{html.escape(token)}">
      <button class="btn btn-success" type="submit" name="decision" value="approve">✅ 승인</button>
      <button class="btn btn-danger" type="submit" name="decision" value="deny">❌ 거부</button>
    </form>
    """
    return _html_page("승인", body)


@app.post("/approve/action")
async def approve_action(
    req: str = Form(...),
    token: str = Form(...),
    decision: str = Form(...),
    reason: str = Form(""),
):
    data = ob.load_registrations()
    entry = data.get("registrations", {}).get(req)
    if not entry or entry.get("approver_token") != token:
        return _html_page("승인", "<h1>잘못된 접근</h1>", 403)
    if entry["status"] != ob.ST_OAUTH_DONE:
        return _html_page("승인",
                          f"<h1 class='warn'>처리 불가 ({html.escape(entry['status'])})</h1>", 409)

    if decision == "deny":
        ob.transition_registration(req, ob.ST_DENIED, denied_reason=reason or "관리자 거부")
        return _html_page("승인",
                          f"<h1 class='err'>❌ 거부 처리됨 — {html.escape(entry['name'])}</h1>")

    if decision != "approve":
        return _html_page("승인", "<h1>잘못된 decision 값</h1>", 400)

    # APPROVE: write tenant files + register + auto-pair + issue pair code
    name = entry["name"]
    if tenant_exists(name):
        return _html_page("승인",
                          f"<h1 class='err'>중복 — '{html.escape(name)}' 이미 등록됨</h1>", 409)

    tenant_dir = os.path.join(DATA_DIR, "tenants", name)
    os.makedirs(tenant_dir, exist_ok=True)
    cfg_path = os.path.join(tenant_dir, "kakao_config.json")
    tok_path = os.path.join(tenant_dir, "kakao_token.json")

    cfg = {"client_id": entry["client_id"]}
    if entry.get("client_secret"):
        cfg["client_secret"] = entry["client_secret"]
    full_token = entry.get("kakao_token_full") or {
        "access_token": entry["kakao_access_token"],
        "refresh_token": entry["kakao_refresh_token"],
        "token_type": "bearer",
        "scope": OAUTH_SCOPE,
    }
    _save_atomic(cfg_path, cfg)
    _save_atomic(tok_path, full_token)
    # tenant files should be readable by notify-api (uid 1026:100)
    for p in (cfg_path, tok_path):
        try:
            os.chmod(p, 0o640)
            os.chown(p, 1026, 100)
        except (OSError, PermissionError):
            pass

    api_key = secrets.token_urlsafe(32)
    key_hash = hash_api_key(api_key)
    tenants = load_tenants()
    tenants.setdefault("tenants", []).append({
        "id": name,
        "api_key_sha256": key_hash,
        "added_at": ob.iso(),
        "note": "added via chatbot onboarding",
    })
    save_tenants(tenants)

    # Auto-pair the user who initiated the request
    bot_user_id = entry.get("bot_user_id")
    if bot_user_id:
        prefs_data = load_prefs()
        prefs = prefs_data.setdefault("prefs", {})
        prev = prefs.get(bot_user_id, {})
        prefs[bot_user_id] = {
            "tenant_id": name,
            "enabled": True,
            "linked_at": prev.get("linked_at") or now_iso(),
            "updated_at": now_iso(),
        }
        save_prefs(prefs_data)

    # Issue spare pair code for other devices
    pair_code, _ = issue_pair_code(name)

    ob.transition_registration(
        req, ob.ST_APPROVED,
        approved_api_key=api_key,
        approved_pair_code=pair_code,
    )

    body = (
        f"<h1 class='ok'>✅ 승인 완료 — {html.escape(name)}</h1>"
        f"<div class='card'>"
        f"<div>tenant 파일: <code>{html.escape(tenant_dir)}/</code></div>"
        f"<div>tenants.json 추가됨</div>"
        f"<div>요청 사용자 자동 페어링됨 (notify_prefs.json)</div>"
        f"</div>"
        f"<div class='card'>"
        f"<strong>API 키 (1회 표시 — 사용자가 /가입상태 로 다시 조회 가능):</strong>"
        f"<span class='key'>{html.escape(api_key)}</span>"
        f"<strong>페어링 코드 (10분):</strong>"
        f"<span class='key'>{html.escape(pair_code)}</span>"
        f"</div>"
        f"<p>사용자는 카카오 채널에서 <code>/가입상태 {html.escape(name)}</code> 입력 시 위 정보를 받습니다.</p>"
    )
    return _html_page("승인", body)


@app.get("/code-request/approve")
async def code_req_approve_page(req: str = "", token: str = ""):
    entry = ob.find_code_request_by_id(req)
    if not entry or entry.get("approver_token") != token:
        return _html_page("승인", "<h1>잘못된 접근</h1>", 403)
    if entry["status"] == ob.ST_APPROVED:
        return _html_page("승인",
                          f"<h1 class='ok'>✅ 이미 승인됨</h1>"
                          f"<div class='card'>코드: <span class='key'>{html.escape(entry.get('approved_code',''))}</span></div>")
    if entry["status"] != ob.ST_PENDING:
        return _html_page("승인",
                          f"<h1 class='warn'>처리 불가 ({html.escape(entry['status'])})</h1>", 409)
    name = html.escape(entry["tenant_id"])
    body = f"""
    <h1>🔑 코드 재발급 승인 — {name}</h1>
    <div class="card">
      <div><strong>사용자:</strong> {name}</div>
      <div><strong>신청 시각:</strong> {html.escape(entry.get('created_at', '?'))}</div>
      <div><strong>만료:</strong> {html.escape(entry.get('expires_at', '?'))}</div>
    </div>
    <form method="post" action="/code-request/approve/action">
      <input type="hidden" name="req" value="{html.escape(req)}">
      <input type="hidden" name="token" value="{html.escape(token)}">
      <button class="btn btn-success" type="submit" name="decision" value="approve">✅ 승인</button>
      <button class="btn btn-danger" type="submit" name="decision" value="deny">❌ 거부</button>
    </form>
    """
    return _html_page("승인", body)


@app.post("/code-request/approve/action")
async def code_req_approve_action(
    req: str = Form(...),
    token: str = Form(...),
    decision: str = Form(...),
):
    entry = ob.find_code_request_by_id(req)
    if not entry or entry.get("approver_token") != token:
        return _html_page("승인", "<h1>잘못된 접근</h1>", 403)
    if entry["status"] != ob.ST_PENDING:
        return _html_page("승인", f"<h1>처리 불가 ({html.escape(entry['status'])})</h1>", 409)

    if decision == "deny":
        ob.update_code_request(req, status=ob.ST_DENIED)
        return _html_page("승인",
                          f"<h1 class='err'>❌ 거부 처리됨 — {html.escape(entry['tenant_id'])}</h1>")

    if decision != "approve":
        return _html_page("승인", "<h1>잘못된 decision</h1>", 400)

    code, expires_at = issue_pair_code(entry["tenant_id"])
    ob.update_code_request(req, status=ob.ST_APPROVED,
                           approved_code=code, approved_at=ob.iso())
    return _html_page("승인",
                      f"<h1 class='ok'>✅ 승인 완료 — {html.escape(entry['tenant_id'])}</h1>"
                      f"<div class='card'>코드 (10분): <span class='key'>{html.escape(code)}</span><br>"
                      f"사용자가 <code>/코드확인 {html.escape(entry['tenant_id'])}</code> 로 받습니다.</div>")


@app.get("/code-request/status")
async def code_req_status_page(req: str = ""):
    entry = ob.find_code_request_by_id(req)
    if not entry:
        return _html_page("상태", "<h1>요청 없음</h1>", 404)
    name = html.escape(entry["tenant_id"])
    status = html.escape(entry.get("status", "?"))
    body = f"""
    <h1>코드 요청 상태 — {name}</h1>
    <div class='card'>
      <div><strong>상태:</strong> {status}</div>
      <div><strong>신청:</strong> {html.escape(entry.get('created_at', '?'))}</div>
      <div><strong>만료:</strong> {html.escape(entry.get('expires_at', '?'))}</div>
    </div>
    <p>승인 후 카카오 채널에서 <code>/코드확인 {name}</code> 입력하세요.</p>
    """
    return _html_page("상태", body)
