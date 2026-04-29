# 🧑‍💻 새 사용자 온보딩 가이드

> NAS 관리자가 알림 시스템에 본인을 초대했을 때 따라가는 가이드입니다.
> 이미 NAS는 운영 중이고, 본인은 PC 클라이언트만 설치하면 됩니다.

---

## 📋 받을 정보 (관리자에게서)

설치 전에 NAS 관리자(예: parkbohyun)에게서 다음 3가지를 받으세요:

| 항목 | 예시 |
|------|------|
| NAS host | `mynas.duckdns.org` 또는 외부 IP |
| NAS port | `8002` |
| API key | (4단계 후 받음 — 일단 보류) |

---

## 1️⃣ 본인 카카오 개발자 앱 만들기 (1회)

각 사용자는 **자기 카카오 계정**으로 카카오 개발자 앱을 만들어야 합니다 (관리자 앱과 별개).

> 카카오톡 '나에게 보내기' 메시지는 **OAuth 토큰을 동의한 카카오 계정**에게만 도착하기 때문에, 본인 카톡으로 받으려면 본인 계정으로 동의 절차를 거쳐야 합니다.

### 절차

1. https://developers.kakao.com 본인 카카오 계정으로 로그인
2. **내 애플리케이션 → 애플리케이션 추가하기**
   - 앱 이름: 자유 (예: "내 Claude 알림")
   - 사업자명: 개인이면 본인 이름
3. 좌측 메뉴 **앱 키** → **REST API 키** 복사 (이게 `client_id`)
4. **카카오 로그인 → 활성화** ON
5. **카카오 로그인 → Redirect URI** 추가:
   ```
   http://localhost:8765/callback
   ```
6. **카카오 로그인 → 동의항목** → **카카오톡 메시지 전송 (talk_message)** ON
7. (선택) **보안 → Client Secret** 발급 + 활성화 ON

---

## 2️⃣ OAuth 토큰 발급 — 본인 PC에서 1회

```powershell
# 1) 리포 클론
git clone https://github.com/parkbohyun/claude-kakao-notify.git
cd claude-kakao-notify

# 2) OAuth 도구 실행
python nas\tools\get_initial_token.py
```

실행 시 입력:
- **REST API key** — 1단계에서 복사한 것
- **Client secret** — 1단계에서 발급했으면 입력, 안 했으면 Enter

브라우저가 카카오 로그인 페이지로 자동 열림 → 로그인 + 권한 동의 → 콘솔에 "발급 완료" 메시지 + 현재 디렉터리에 두 파일 생성:

```
kakao_config.json     ← REST API key 정보
kakao_token.json      ← access/refresh 토큰
```

> ⚠️ 이 두 파일은 **비밀**입니다. 절대 git에 올리거나 채팅/이메일에 평문 첨부하지 마세요.

---

## 3️⃣ 토큰 파일을 관리자에게 전달

**안전한 경로**로 두 파일을 관리자에게 전달:

| 방법 | 설명 |
|------|------|
| **SCP** (권장) | `scp kakao_*.json admin@nas-host:/tmp/<your-name>/` |
| **암호화 ZIP** | 7z/AES-256 ZIP으로 압축 후 비밀번호 별도 채널로 |
| **사내 보안 메신저** | E2E 암호화되는 채널만 |

**피해야 할 방법**: 일반 이메일 첨부, 카카오톡(서버 저장됨), Slack/Discord 평문, 일반 클라우드(Google Drive 등 평문 업로드).

전달 후, 관리자가 등록을 끝내면 **API 키**를 회신해 줄 겁니다.

---

## 4️⃣ PC 클라이언트 설치

관리자에게서 API 키를 받았으면:

```powershell
# 본인 PC (위 2단계에서 클론한 디렉터리에서)
cd claude-kakao-notify
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

설치 중 입력:

| 항목 | 입력 값 |
|------|---------|
| NAS host or IP | 관리자가 알려준 host (예: `mynas.duckdns.org`) |
| NAS port | 관리자가 알려준 port (예: `8002`) |
| API key | 관리자가 보내준 API 키 |

설치 완료 후 **Claude Code를 새로 띄우면** 첫 알림이 본인 카카오톡 '나에게 보내기'에 도착합니다 🎉

---

## ✅ 동작 확인

Claude Code 채팅에서:

```
/remote-control
```
URL이 표시되면:
```
/rcd
```
→ 카톡으로 "🔗 [Claude Code] 원격 접속 URL" 메시지가 도착해야 정상.

---

## 🔧 문제 발생 시

| 증상 | 점검 |
|------|------|
| 설치 후 카톡 안 옴 | `~/.claude/hook-debug/SessionStart_*.json` 파일 존재 여부 → 있으면 PC 측은 정상, 관리자에게 NAS 로그 확인 요청 |
| `Invalid API key` (401) | 관리자가 보내준 키와 `~/.claude/notify-api.env` 의 키 정확히 일치하는지 (앞뒤 공백 포함) |
| `Connection refused` | NAS host/port 확인. `curl http://<host>:<port>/health` 로 도달성 확인 |
| 카톡이 다른 사람에게 도착 | 전달한 토큰 파일이 본인 카카오 계정 OAuth 결과인지 재확인 — 다른 계정으로 발급한 토큰을 보냈을 가능성 |

---

## 🙋 자주 묻는 질문

**Q. 관리자도 토큰 파일을 본 적이 있다는 게 보안상 문제 없나요?**
A. `kakao_token.json`은 본인 카카오 계정의 access/refresh 토큰이므로 관리자가 마음만 먹으면 본인 카톡에 메시지를 보낼 수 있습니다. 단, **읽기/통화/연락처 등 다른 권한은 없고 '나에게 보내기'만 가능**합니다 (talk_message scope만 동의했기 때문). 신뢰하는 관리자에게만 전달하세요.

**Q. 토큰을 갱신해야 하나요?**
A. access_token이 만료되면 NAS 컨테이너가 refresh_token으로 자동 갱신합니다. 설치 후엔 신경 쓸 필요 없습니다. refresh_token 자체가 만료되는 경우(약 60일 미사용)에만 OAuth 재진행이 필요.

**Q. 여러 PC에서 사용 가능?**
A. 네. 각 PC에서 `install.ps1`을 같은 host/port/key로 실행하면 됩니다. 토큰 파일은 NAS에만 있고 PC들은 모두 동일한 API 키를 사용합니다.

**Q. 설치를 제거하려면?**
A. `powershell -ExecutionPolicy Bypass -File .\uninstall.ps1` 실행. 추가로 NAS 관리자에게 본인 테넌트 제거를 요청하세요 (토큰 파일이 남아있으면 회수).
