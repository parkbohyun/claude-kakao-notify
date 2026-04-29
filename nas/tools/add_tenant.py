"""Multi-tenant administration tool for NAS notify-api.

Subcommands:
  add        — Register a new tenant (generates or accepts an API key).
               Also issues a 6-char pairing code (10-min TTL) for KakaoTalk
               channel toggling.
  pair       — Issue a fresh pairing code for an existing tenant.
  migrate    — Migrate legacy single-tenant data into a tenant entry.
  list       — List registered tenants (no keys, no hashes).
  remove     — Remove a tenant from registry (data dir kept unless --purge).

Examples (run on NAS host with --data-dir, or inside container with default):

  # 새 테넌트 추가 (사용자가 PC에서 get_initial_token.py 실행한 결과 파일 받아서)
  python add_tenant.py add userB \\
      --config /tmp/userB_kakao_config.json \\
      --token  /tmp/userB_kakao_token.json \\
      --data-dir /volume1/docker/claude-kakao-notify/nas/data

  # 기존 단일테넌트 → 멀티테넌트 마이그레이션 (NOTIFY_API_KEY env 필요)
  NOTIFY_API_KEY=<현재키> python add_tenant.py migrate parkbohyun \\
      --data-dir /volume1/docker/claude-kakao-notify/nas/data

  # 등록된 테넌트 목록
  python add_tenant.py list --data-dir /volume1/docker/claude-kakao-notify/nas/data

  # 제거 (데이터까지 삭제하려면 --purge)
  python add_tenant.py remove userB --purge \\
      --data-dir /volume1/docker/claude-kakao-notify/nas/data
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time

# Force UTF-8 on Windows consoles (cp949 chokes on em dash etc.)
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

TENANT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Code chars exclude visually ambiguous: 0,O,1,I,L
PAIR_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PAIR_CODE_LEN = 6
PAIR_CODE_TTL_SEC = 600  # 10 minutes


# ─── Helpers ─────────────────────────────────────────────────────────────────

def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def tenants_path(data_dir: str) -> str:
    return os.path.join(data_dir, "tenants.json")


def load_tenants(data_dir: str) -> dict:
    path = tenants_path(data_dir)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"tenants": []}


def save_tenants_atomic(data_dir: str, data: dict) -> None:
    path = tenants_path(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def validate_tenant_id(tid: str) -> None:
    if not TENANT_ID_RE.match(tid):
        sys.exit(f"[!] tenant_id must match {TENANT_ID_RE.pattern}")


def fail_if_exists(tenants: dict, tid: str) -> None:
    if any(t["id"] == tid for t in tenants["tenants"]):
        sys.exit(f"[!] tenant '{tid}' already exists")


def fail_if_hash_collides(tenants: dict, key_hash: str) -> None:
    if any(t.get("api_key_sha256") == key_hash for t in tenants["tenants"]):
        sys.exit("[!] API key hash collides with another tenant — regenerate")


def pair_codes_path(data_dir: str) -> str:
    return os.path.join(data_dir, "pair_codes.json")


def load_pair_codes(data_dir: str) -> dict:
    path = pair_codes_path(data_dir)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"codes": {}}


def save_pair_codes_atomic(data_dir: str, data: dict) -> None:
    path = pair_codes_path(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def generate_pair_code(data_dir: str, tenant_id: str) -> tuple[str, str]:
    """Generate a fresh pair code; sweep expired entries; return (code, expires_at)."""
    data = load_pair_codes(data_dir)
    codes = data.get("codes", {})
    now = int(time.time())

    # Drop expired and any codes already pointing at this tenant (one active at a time).
    codes = {
        c: meta for c, meta in codes.items()
        if meta.get("expires_ts", 0) > now and meta.get("tenant_id") != tenant_id
    }

    for _ in range(20):
        code = "".join(secrets.choice(PAIR_CODE_CHARS) for _ in range(PAIR_CODE_LEN))
        if code not in codes:
            break
    else:
        sys.exit("[!] failed to allocate a unique pair code — retry")

    expires_ts = now + PAIR_CODE_TTL_SEC
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(expires_ts))
    codes[code] = {
        "tenant_id": tenant_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "expires_at": expires_at,
        "expires_ts": expires_ts,
    }
    data["codes"] = codes
    save_pair_codes_atomic(data_dir, data)
    return code, expires_at


# ─── Subcommands ─────────────────────────────────────────────────────────────

def cmd_add(args: argparse.Namespace) -> int:
    validate_tenant_id(args.tenant_id)

    for label, path in (("config", args.config), ("token", args.token)):
        if not os.path.isfile(path):
            sys.exit(f"[!] {label} file not found: {path}")

    tenants = load_tenants(args.data_dir)
    fail_if_exists(tenants, args.tenant_id)

    api_key = args.api_key or secrets.token_urlsafe(32)
    if len(api_key) < 16:
        sys.exit("[!] --api-key must be at least 16 characters")
    key_hash = hash_key(api_key)
    fail_if_hash_collides(tenants, key_hash)

    tenant_dir = os.path.join(args.data_dir, "tenants", args.tenant_id)
    os.makedirs(tenant_dir, exist_ok=True)
    cfg_dst = os.path.join(tenant_dir, "kakao_config.json")
    tok_dst = os.path.join(tenant_dir, "kakao_token.json")
    shutil.copy2(args.config, cfg_dst)
    shutil.copy2(args.token, tok_dst)
    for p in (cfg_dst, tok_dst):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    tenants["tenants"].append({
        "id": args.tenant_id,
        "api_key_sha256": key_hash,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    save_tenants_atomic(args.data_dir, tenants)

    code, expires_at = generate_pair_code(args.data_dir, args.tenant_id)

    print()
    print(f"✓ Tenant '{args.tenant_id}' added.")
    print(f"  config: {cfg_dst}")
    print(f"  token : {tok_dst}")
    print()
    print("─── API KEY (저장하세요 — 다시 출력되지 않습니다) ──────────────")
    print(api_key)
    print("─── 클라이언트 ~/.claude/notify-api.env 의 NOTIFY_API_KEY 로 사용 ─")
    print()
    print("─── 페어링 코드 (카카오 채널에서 알림 ON/OFF 토글하려면 1회 입력) ──")
    print(f"  코드: {code}")
    print(f"  만료: {expires_at}  (10분)")
    print("  사용자 → 카카오 채널에서: /연동 " + code)
    print()
    print("힌트: 컨테이너 재시작 불필요 — tenants.json 변경은 즉시 반영됨.")
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    validate_tenant_id(args.tenant_id)
    tenants = load_tenants(args.data_dir)
    if not any(t["id"] == args.tenant_id for t in tenants.get("tenants", [])):
        sys.exit(f"[!] tenant '{args.tenant_id}' not found — register it first with 'add'")

    code, expires_at = generate_pair_code(args.data_dir, args.tenant_id)
    print()
    print(f"✓ Pair code issued for tenant '{args.tenant_id}'.")
    print(f"  코드: {code}")
    print(f"  만료: {expires_at}  (10분)")
    print()
    print("사용자가 카카오 채널에서 입력:")
    print(f"  /연동 {code}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    validate_tenant_id(args.tenant_id)

    legacy_cfg = os.path.join(args.data_dir, "kakao_config.json")
    legacy_tok = os.path.join(args.data_dir, "kakao_token.json")
    if not (os.path.isfile(legacy_cfg) and os.path.isfile(legacy_tok)):
        sys.exit(f"[!] no legacy kakao_*.json files in {args.data_dir} — nothing to migrate")

    api_key = args.api_key or os.environ.get("NOTIFY_API_KEY", "").strip()
    if not api_key:
        sys.exit("[!] need --api-key or NOTIFY_API_KEY env (the existing legacy key)")

    tenants = load_tenants(args.data_dir)
    fail_if_exists(tenants, args.tenant_id)
    key_hash = hash_key(api_key)
    fail_if_hash_collides(tenants, key_hash)

    tenant_dir = os.path.join(args.data_dir, "tenants", args.tenant_id)
    os.makedirs(tenant_dir, exist_ok=True)
    cfg_dst = os.path.join(tenant_dir, "kakao_config.json")
    tok_dst = os.path.join(tenant_dir, "kakao_token.json")

    shutil.move(legacy_cfg, cfg_dst)
    shutil.move(legacy_tok, tok_dst)

    legacy_lock = os.path.join(args.data_dir, "kakao_token.lock")
    if os.path.isfile(legacy_lock):
        try:
            os.remove(legacy_lock)
        except OSError:
            pass

    tenants["tenants"].append({
        "id": args.tenant_id,
        "api_key_sha256": key_hash,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "migrated from legacy single-tenant",
    })
    save_tenants_atomic(args.data_dir, tenants)

    print(f"✓ Legacy data migrated to tenant '{args.tenant_id}'.")
    print(f"  files moved to: {tenant_dir}/")
    print(f"  api key hashed (existing key still valid for clients — no .env change needed)")
    print()
    print("권장:")
    print("  - docker-compose.yml 의 NOTIFY_API_KEY env 는 이제 무시되니 제거 가능")
    print("  - 새 테넌트는 'add' 서브커맨드로 추가")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    tenants = load_tenants(args.data_dir)
    entries = tenants.get("tenants", [])
    if not entries:
        print("(no tenants registered — running in legacy single-tenant mode if "
              "kakao_*.json + NOTIFY_API_KEY env are present)")
        return 0
    print(f"{'ID':<24} {'ADDED':<22} NOTE")
    print(f"{'─' * 24} {'─' * 22} ─────────────────")
    for t in entries:
        print(f"{t['id']:<24} {t.get('added_at', '?'):<22} {t.get('note', '')}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    tenants = load_tenants(args.data_dir)
    before = len(tenants["tenants"])
    tenants["tenants"] = [t for t in tenants["tenants"] if t["id"] != args.tenant_id]
    if len(tenants["tenants"]) == before:
        sys.exit(f"[!] tenant '{args.tenant_id}' not found")
    save_tenants_atomic(args.data_dir, tenants)

    tenant_dir = os.path.join(args.data_dir, "tenants", args.tenant_id)
    if os.path.isdir(tenant_dir):
        if args.purge:
            shutil.rmtree(tenant_dir)
            print(f"✓ Tenant '{args.tenant_id}' removed (data purged).")
        else:
            print(f"✓ Tenant '{args.tenant_id}' removed from registry.")
            print(f"  Data dir kept: {tenant_dir}")
            print(f"  (--purge to also delete the directory)")
    else:
        print(f"✓ Tenant '{args.tenant_id}' removed from registry (no data dir found).")
    return 0


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        prog="add_tenant.py",
        description="Multi-tenant admin tool for notify-api.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--data-dir", default="/data",
                   help="Data directory (container: /data, host: e.g. ./data)")

    sp = p.add_subparsers(dest="cmd", required=True)

    a = sp.add_parser("add", help="Register a new tenant")
    a.add_argument("tenant_id")
    a.add_argument("--config", required=True, help="Path to kakao_config.json")
    a.add_argument("--token", required=True, help="Path to kakao_token.json")
    a.add_argument("--api-key", help="API key (autogenerated 32-byte URL-safe if omitted)")
    a.set_defaults(func=cmd_add)

    m = sp.add_parser("migrate", help="Migrate legacy single-tenant data")
    m.add_argument("tenant_id")
    m.add_argument("--api-key",
                   help="Legacy API key (defaults to NOTIFY_API_KEY env)")
    m.set_defaults(func=cmd_migrate)

    pp = sp.add_parser("pair", help="Issue a fresh KakaoTalk pairing code")
    pp.add_argument("tenant_id")
    pp.set_defaults(func=cmd_pair)

    sp.add_parser("list", help="List registered tenants").set_defaults(func=cmd_list)

    r = sp.add_parser("remove", help="Remove a tenant from registry")
    r.add_argument("tenant_id")
    r.add_argument("--purge", action="store_true",
                   help="Also delete the tenant's data directory")
    r.set_defaults(func=cmd_remove)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
