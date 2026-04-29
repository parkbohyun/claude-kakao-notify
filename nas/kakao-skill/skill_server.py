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
import json
import os
import re
import subprocess
import time
from typing import Optional

from fastapi import FastAPI, Request

app = FastAPI()

DATA_DIR = "/data"
PREFS_FILE = os.path.join(DATA_DIR, "notify_prefs.json")
PAIR_FILE = os.path.join(DATA_DIR, "pair_codes.json")

DOCMOST_NAME = "docmost_25"

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

@app.api_route(methods=["GET", "POST"], path="/status")
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


@app.api_route(methods=["GET", "POST"], path="/info")
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


@app.api_route(methods=["GET", "POST"], path="/version")
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


@app.api_route(methods=["GET", "POST"], path="/backup")
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


@app.post("/pair")
async def pair(request: Request):
    """/연동 <CODE> — bot user_id ↔ tenant_id 연결."""
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")

    user_id = get_user_id(payload)
    utterance = get_utterance(payload).upper()
    if not user_id:
        return kakao_response("⚠️ 사용자 식별 실패.")

    m = PAIR_CODE_RE.search(utterance)
    if not m:
        return kakao_response(
            "🔑 페어링 코드를 입력해주세요.\n"
            "예: /연동 ABC123\n\n"
            "코드는 NAS 관리자에게서 받은 6자리 영문/숫자입니다."
        )
    code = m.group(1)

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


@app.post("/notify-on")
async def notify_on(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")
    return _set_enabled(payload, True)


@app.post("/notify-off")
async def notify_off(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return kakao_response("⚠️ 요청 파싱 실패.")
    return _set_enabled(payload, False)


@app.post("/notify-status")
async def notify_status(request: Request):
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


@app.api_route(methods=["GET", "POST"], path="/help")
async def help_(request: Request):
    return kakao_response(HELP_TEXT)


@app.api_route(methods=["GET", "POST"], path="/guide")
async def guide(request: Request):
    return kakao_response(HELP_TEXT)


@app.api_route(methods=["GET", "POST"], path="/welcome")
async def welcome(request: Request):
    text = (
        "👋 안녕하세요! NAS 알림 봇입니다.\n"
        "━━━━━━━━━━━━━━\n"
        "운영 상태 확인 + Claude Code 알림\n"
        "ON/OFF 토글이 가능합니다.\n\n"
        "📌 /상태  /정보  /버전  /백업\n"
        "📌 /연동  /알림 켜기  /알림 끄기  /알림 상태\n\n"
        "처음이라면 /도움 부터 시작하세요 😊"
    )
    return kakao_response(text)
