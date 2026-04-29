"""Kakao OAuth 초기 토큰 발급 도구.

PC 또는 브라우저+Python 가능한 환경 어디서나 1회 실행.
완료 시 현재 디렉터리에 다음 두 파일 생성:
  - kakao_config.json  (client_id [+ client_secret])
  - kakao_token.json   (access_token, refresh_token, ...)

이 두 파일을 NAS notify-api 컨테이너의 /data 마운트 (예: ./data) 에
배치하면 끝. 토큰은 컨테이너가 자동으로 refresh 한다.

사전 조건:
  1) https://developers.kakao.com 에 앱 생성
  2) [카카오 로그인] 활성화 ON
  3) [카카오 로그인 → Redirect URI] 에 http://localhost:8765/callback 등록
  4) [동의항목] 에서 '카카오톡 메시지 전송 (talk_message)' 동의 받기 ON
"""

import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

REDIRECT_PORT = 8765
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}{REDIRECT_PATH}"
SCOPE = "talk_message"
WAIT_TIMEOUT_SEC = 300  # 5 minutes

KAUTH_AUTHORIZE = "https://kauth.kakao.com/oauth/authorize"
KAUTH_TOKEN = "https://kauth.kakao.com/oauth/token"


def port_available(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    return raw or default


def run_callback_server(holder: dict) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if "code" in params:
                holder["code"] = params["code"][0]
                self.wfile.write(
                    "<h2>인증 완료</h2>"
                    "<p>이 탭은 닫아도 됩니다.</p>".encode("utf-8")
                )
            elif "error" in params:
                holder["error"] = params.get("error_description", [params["error"][0]])[0]
                self.wfile.write(f"<h2>오류</h2><p>{holder['error']}</p>".encode("utf-8"))
            else:
                self.wfile.write(b"No code in callback.")

        def log_message(self, *_a, **_kw):  # silence default access log
            pass

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> int:
    print("=" * 60)
    print("  Kakao OAuth 초기 토큰 발급")
    print("=" * 60)
    print()

    if not port_available(REDIRECT_PORT):
        print(f"[!] 포트 {REDIRECT_PORT} 가 이미 사용 중입니다. 다른 프로세스 종료 후 재시도.")
        return 1

    client_id = prompt("REST API key (client_id)")
    if not client_id:
        print("[!] client_id 가 필요합니다.")
        return 1
    client_secret = prompt("Client secret (없으면 Enter)")

    holder: dict = {}
    server = run_callback_server(holder)

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    }
    auth_url = f"{KAUTH_AUTHORIZE}?{urllib.parse.urlencode(auth_params)}"
    print()
    print(f"브라우저에서 다음 URL 을 엽니다:\n  {auth_url}\n")
    print(f"콜백 대기 중... (최대 {WAIT_TIMEOUT_SEC}s)\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        print("(브라우저 자동 열기 실패 — URL 을 수동 복사 후 브라우저에 붙여넣으세요)")

    deadline = time.time() + WAIT_TIMEOUT_SEC
    while time.time() < deadline and "code" not in holder and "error" not in holder:
        time.sleep(0.1)
    server.shutdown()

    if "error" in holder:
        print(f"[!] OAuth 오류: {holder['error']}")
        return 1
    if "code" not in holder:
        print("[!] 시간 초과. 다시 실행하세요.")
        return 1

    code = holder["code"]
    print(f"인증 코드 수신: {code[:8]}...")

    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }
    if client_secret:
        data["client_secret"] = client_secret

    req = urllib.request.Request(
        KAUTH_TOKEN,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            tok = json.load(resp)
    except Exception as e:
        print(f"[!] 토큰 교환 실패: {e}")
        return 1

    if "access_token" not in tok or "refresh_token" not in tok:
        print(f"[!] 응답에 토큰이 없습니다: {tok}")
        return 1

    cfg = {"client_id": client_id}
    if client_secret:
        cfg["client_secret"] = client_secret

    cfg_path = os.path.abspath("kakao_config.json")
    tok_path = os.path.abspath("kakao_token.json")

    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    with open(tok_path, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(cfg_path, 0o600)
        os.chmod(tok_path, 0o600)
    except OSError:
        pass

    print()
    print("발급 완료:")
    print(f"  - {cfg_path}")
    print(f"  - {tok_path}")
    print()
    print("두 파일을 NAS notify-api 컨테이너의 /data 마운트 디렉터리로 복사하세요.")
    print("예: scp kakao_*.json user@nas:/volume1/docker/notify-api/data/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
