# Claude Code → 카카오톡 알림 시스템

> Claude Code의 작업 이벤트와 원격 접속 URL을 카카오톡 **'나에게 보내기'**로 자동 발송하는 셀프호스팅 키트.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%2B%20Synology-blue)](#)
[![Python](https://img.shields.io/badge/Python-3.10%2B-green)](#)

---

## 📌 한 줄 요약

**Claude Code가 작업을 끝냈거나 사용자 입력을 기다릴 때, 또는 원격 접속 URL이 생겼을 때 카카오톡으로 즉시 받습니다.**

---

## 🎯 무엇을 받을 수 있나

| 시점 | 카톡 메시지 |
|------|-------------|
| 새 Claude Code 세션 시작 | 🚀 [Claude Code] 작업 시작 + cwd + session id |
| 세션 종료 | ✅ [Claude Code] 작업 완료 |
| 사용자 입력 대기 | ⚠️ [Claude Code] 사용자 입력 필요 + 메시지 |
| `/rcd <URL>` 또는 `/rcd` | 🔗 원격 접속 URL + 클릭 가능한 버튼 |
| 모델 판단 임의 알림 | 모델이 `notify` MCP 툴을 호출할 때 |

> **활용 예**: 외근 중에도 PC가 작업 끝났는지, 사용자 결정을 기다리는지 즉시 인지. 원격 접속 URL을 카톡으로 받아 모바일에서 곧바로 이어서 작업.

---

## 🏗️ 동작 흐름

```
┌─────────────┐        ┌─────────────────┐        ┌──────────────┐
│ Claude Code │  POST  │  NAS notify-api │  POST  │  Kakao API   │
│    (PC)     │ ─────► │ (Docker/FastAPI)│ ─────► │              │
└─────────────┘        └─────────────────┘        └──────────────┘
       │                       │                          │
   hook / MCP /          /data 마운트                access_token
   /rcd 트리거         kakao_config.json             자동 갱신 +
                       kakao_token.json              flock race 방어
                              │
                              ▼
                       카카오톡 '나에게 보내기'
```

세 가지 트리거 경로가 모두 같은 컨테이너 엔드포인트로 모입니다:
- **Hook** (`notify.py`) — Claude Code 세션 라이프사이클 이벤트 자동
- **MCP 툴** (`notify`) — 모델이 임의 시점에 호출
- **슬래시 커맨드** (`/rcd`) — 사용자가 RC URL을 한 줄로 발송

---

## 🧱 시스템 요구사항

### NAS / 서버 / 로컬 서버
- Linux + Docker 또는 **Synology DSM 7+** (Container Manager 패키지)
- 외부 노출 가능한 포트 1개 (기본 `8002`)
- 24/7 운영 권장

### 카카오 개발자 계정 (1회 설정)
- https://developers.kakao.com 무료 앱 생성
- "카카오 로그인" 활성화 + "카카오톡 메시지(talk_message)" 동의 항목 ON
- 본인 계정으로 OAuth 1회 진행 → access/refresh 토큰 획득
- 본 리포의 `nas/tools/get_initial_token.py` 가 자동화

### PC (여러 대 가능)
- **Windows + PowerShell 5.1+**
- **Python 3.10+** (PATH 등록)
- **Claude Code** 설치
- NAS 도달 가능한 네트워크 (사내/VPN 또는 인터넷 + 포트포워딩)

---

## 🚀 빠른 시작 — 3단계

> ① 카카오 앱 만들기 → ② NAS 컨테이너 띄우기 → ③ PC 클라이언트 설치 (각 PC)

### ① 카카오 개발자 설정 (1회)

1. https://developers.kakao.com 로그인 → **내 애플리케이션 → 추가하기**
2. 좌측 메뉴 **앱 키** → **REST API 키** 복사 (이것이 `client_id`)
3. **카카오 로그인 → 활성화** ON
4. **카카오 로그인 → Redirect URI** 추가: `http://localhost:8765/callback`
5. **카카오 로그인 → 동의항목** → **카카오톡 메시지 전송 (talk_message)** ON
6. (선택) **보안 → Client Secret** 발급 + 활성화 ON

### ② NAS notify-api 컨테이너 (1회)

#### a. 리포 클론 + 작업 디렉터리 진입

NAS 에 SSH 접속 후:

```bash
cd /volume1/docker
git clone https://github.com/parkbohyun/claude-kakao-notify.git
cd claude-kakao-notify/nas
```

#### b. 환경 파일 작성

```bash
cp .env.example .env

# 강한 무작위 키 생성해서 NOTIFY_API_KEY 에 채우기
openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' > /tmp/key.txt
cat /tmp/key.txt   # ← 이 값을 .env 에 붙여넣고 클라이언트 설치 시에도 동일하게 사용
nano .env
```

#### c. 카카오 토큰 초기 발급 (PC 어디서나 1회)

PC 또는 브라우저+Python 가능한 환경에서:

```bash
python claude-kakao-notify/nas/tools/get_initial_token.py
```

브라우저가 카카오 로그인 페이지로 자동 열림 → 로그인 + 동의 → 같은 디렉터리에 `kakao_config.json` + `kakao_token.json` 두 파일 생성.

#### d. NAS에 토큰 파일 배치

```bash
mkdir -p /volume1/docker/claude-kakao-notify/nas/data
scp kakao_config.json kakao_token.json \
    you@nas:/volume1/docker/claude-kakao-notify/nas/data/
ssh you@nas 'chmod 600 /volume1/docker/claude-kakao-notify/nas/data/kakao_*.json'
```

#### e. 컨테이너 기동

```bash
cd /volume1/docker/claude-kakao-notify/nas
sudo docker compose up -d --build

# 헬스체크 (config:true, token:true 가 떠야 정상)
curl http://localhost:8002/health
```

#### f. 외부 노출 (외부 PC에서도 사용할 경우)

라우터 관리 페이지에서:

```
외부 8002  →  NAS LAN IP:8002   (TCP)
```

DDNS 도메인이 있으면 클라이언트는 `your-ddns.example.com:8002` 사용. 없으면 외부 IP 직접 사용.

> **HTTPS 권장**: 시놀로지 리버스 프록시 + Let's Encrypt 로 `https://notify.your-ddns.example.com` 식의 HTTPS 노출 후, 클라이언트 `.env` 에서 `NOTIFY_API_SCHEME=https` + `NOTIFY_API_PORT=443` 설정.

### ③ PC 클라이언트 설치 (각 PC)

```powershell
git clone https://github.com/parkbohyun/claude-kakao-notify.git
cd claude-kakao-notify
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

설치 중 입력:

| 항목 | 입력 값 |
|------|---------|
| **NAS host or IP** | DDNS 도메인 / 외부 IP / LAN IP 중 환경에 맞는 것 |
| **NAS port** | `8002` (기본) |
| **API key** | NAS `.env` 의 `NOTIFY_API_KEY` 와 **동일하게** |

설치되는 것 (모두 자동):

```
~/.claude/
├── hooks/notify.py                  ← 세션 이벤트 hook
├── mcp/notify-mcp/server.py         ← notify MCP 툴
├── commands/rcd.md                  ← /rcd 슬래시 커맨드
├── notify-api.env                   ← host/port/key (이 파일만 비밀)
├── settings.json                    ← hooks + permissions 머지 (자동 백업)
└── .. (← ~/.claude.json 의 mcpServers.notify 도 머지, 자동 백업)
```

`mcp` Python 패키지가 `pip install --user` 로 자동 설치됩니다.

---

## 💡 사용 방법

### 🔔 자동 알림
설치 후 별도 작업 없음. Claude Code 세션 시작/종료/사용자 입력 대기 시 자동 카톡 도착.

### 🔗 `/rcd` 로 원격 접속 URL 발송

```
1) Claude Code 채팅에서  /remote-control          → URL 표시됨
2) 같은 채팅에서          /rcd                     ← 자동 추출 시도
   또는                    /rcd https://claude.ai/code/session_xxx
3) 모바일 카톡 → URL 클릭 → 핸드폰/태블릿에서 그대로 이어서 작업
```

### 🤖 임의 시점 알림 (모델 판단)

채팅에서 "**끝나면 카톡으로 알려줘**" 같은 지시를 하면, 모델이 적절한 시점에 `notify` MCP 툴을 호출합니다.

---

## 🔧 환경변수 레퍼런스

### PC `~/.claude/notify-api.env`

| 변수 | 필수 | 설명 |
|------|:---:|------|
| `NOTIFY_API_HOST` | ✅ | NAS host (DDNS / 외부 IP / LAN IP) |
| `NOTIFY_API_PORT` | (기본 `8002`) | NAS 외부 포트 |
| `NOTIFY_API_KEY`  | ✅ | API 키 (NAS `.env` 와 동일) |
| `NOTIFY_API_SCHEME` | (기본 `http`) | HTTPS 사용 시 `https` |
| `NOTIFY_API_PATH` | (기본 `/notify`) | 엔드포인트 경로 |
| `NOTIFY_API_URL` | (대안) | HOST/PORT 대신 전체 URL 한 줄로 — 하위 호환 |

### NAS `nas/.env`

| 변수 | 필수 | 설명 |
|------|:---:|------|
| `NOTIFY_API_KEY` | ✅ | 클라이언트와 동일한 키 |
| `HOST_PORT` | (기본 `8002`) | 호스트 노출 포트 |
| `RUN_UID` / `RUN_GID` | (기본 `1026`/`100`) | 컨테이너 실행 uid/gid (호스트 소유권 보존) |
| `DATA_DIR` | (기본 `./data`) | `/data` 마운트 소스 — 기존 토큰 디렉터리 공유 시 절대경로 |

---

## 🛠️ 트러블슈팅

| 증상 | 점검 |
|------|------|
| **카톡이 안 옴** | `~/.claude/hook-debug/SessionStart_*.json` 덤프 존재 여부 → 있으면 PC 측 발송은 시도됨. NAS 쪽: `sudo docker logs notify-api --tail 50` |
| **`Workspace not trusted`** | 새 cwd 에서 `claude` 1회 인터랙티브 실행 후 trust 다이얼로그 수락 |
| **`NOTIFY_API_KEY 미설정`** | `~/.claude/notify-api.env` 위치/내용/접근권한 확인 |
| **`/rcd: URL을 찾지 못함`** | `/rcd <URL>` 인자 명시 전달 |
| **NAS 토큰 만료 (401)** | 컨테이너가 자동 refresh 시도 — 영구 실패 시 `get_initial_token.py` 재실행 |
| **외부에서 안 됨** | 라우터 포트포워딩 / NAS 방화벽 / DDNS 갱신 / `curl your-host:port/health` 확인 |
| **카톡 본문에 URL 없음** | 카카오 텍스트 템플릿은 `link.web_url` 을 **버튼 액션**용으로만 사용. `/rcd` 는 message 본문에도 URL 인라인 포함시켜 발송함 — 다른 호출자도 본문에 URL을 보이게 하려면 message 문자열에 직접 포함 |
| **멀티테넌트에서 401 Invalid API key** | `tools/add_tenant.py list` 로 테넌트 등록 여부 확인 → 클라이언트 `.env` 의 `NOTIFY_API_KEY` 가 `add` 시점에 출력된 키와 일치하는지 확인 |
| **멀티테넌트로 마이그레이션 후 카톡이 다른 사람에게** | 토큰 파일 매핑 오류 가능 — `data/tenants/<id>/kakao_token.json` 이 그 테넌트 본인의 OAuth 결과인지 재확인 |

---

## 👥 멀티테넌트 — 여러 사용자가 한 NAS 공유

> 본인 NAS 하나로 가족/팀원 여러 명이 **각자의 카카오톡으로** 알림을 받게 할 수 있습니다.

### 동작 원리

각 사용자는 자기 카카오 계정으로 OAuth 1회 진행 → 자기 토큰 파일을 NAS 관리자(본인)에게 전달 → 관리자가 `add_tenant.py`로 등록 → 시스템이 **API 키 → 테넌트** 라우팅. 카카오 API는 access_token 소유자에게만 메시지를 보내므로 누가 호출하든 메시지는 그 토큰의 주인에게 도착.

### 디렉터리 레이아웃 (자동 생성)

```
data/
├── tenants.json                  ← 키 SHA-256 해시 ↔ 테넌트 ID 매핑
└── tenants/
    ├── parkbohyun/
    │   ├── kakao_config.json
    │   └── kakao_token.json
    └── userB/
        ├── kakao_config.json
        └── kakao_token.json
```

API 키는 평문으로 저장되지 않습니다 (SHA-256 해시만 저장).

### 신규 테넌트 추가 절차

#### 추가 사용자 (PC) 측 — 카카오 OAuth 1회

```bash
# 그 사용자의 카카오 계정으로 진행
python claude-kakao-notify/nas/tools/get_initial_token.py
# → 현재 디렉터리에 kakao_config.json + kakao_token.json 생성됨
# 두 파일을 NAS 관리자에게 안전한 경로로 전달 (이메일/USB/SCP 등)
```

> 주의: 이 사용자는 자기 카카오 개발자 앱을 만들거나 기존 앱에 팀원으로 추가되어야 함. 추가 사용자는 [카카오 개발자 설정](#-카카오-개발자-설정-1회) 절차를 자기 계정으로 1회 수행.

#### NAS 관리자 측 — 테넌트 등록

```bash
ssh nas
cd /volume1/docker/claude-kakao-notify/nas

# 사용자한테 받은 토큰 파일 임시 위치에 두고
python tools/add_tenant.py add userB \
    --config /tmp/userB_kakao_config.json \
    --token  /tmp/userB_kakao_token.json \
    --data-dir ./data

# → 출력 마지막에 API 키가 한 번 표시됨. 이걸 사용자에게 안전하게 전달.
```

`tenants.json` 변경은 **즉시 반영** — 컨테이너 재시작 불필요 (mtime 캐시).

#### 추가 사용자 (PC) 측 — 클라이언트 설치

```powershell
git clone https://github.com/parkbohyun/claude-kakao-notify.git
cd claude-kakao-notify
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

설치 중 입력:
- NAS host = 관리자가 알려준 host (예: `mynas.duckdns.org`)
- NAS port = 관리자가 알려준 port (예: `8002`)
- API key = `add_tenant.py`가 출력한 그 키

이제 이 사용자의 Claude Code 알림이 **이 사용자 본인의 카톡**으로 도착.

### 관리 명령

```bash
# 테넌트 목록
python tools/add_tenant.py list --data-dir ./data

# 테넌트 제거 (데이터 디렉터리는 보존)
python tools/add_tenant.py remove userB --data-dir ./data

# 테넌트 제거 + 토큰 파일까지 삭제
python tools/add_tenant.py remove userB --purge --data-dir ./data

# 기존 단일테넌트 → 멀티테넌트 마이그레이션 (기존 NOTIFY_API_KEY 그대로 유지)
NOTIFY_API_KEY=<현재키> python tools/add_tenant.py migrate parkbohyun --data-dir ./data
```

### 레거시 모드 (단일테넌트)

`/data/tenants.json`이 **없으면** 자동으로 레거시 모드로 동작:
- env `NOTIFY_API_KEY` 로 인증
- `/data/kakao_config.json` + `/data/kakao_token.json` 사용

따라서 v1.0 으로 셋업한 기존 배포는 **변경 없이 그대로 동작**합니다. 멀티테넌트가 필요해질 때 `migrate` 서브커맨드 한 번으로 무중단 전환.

---

## ❌ 제거

### PC 측

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

- `settings.json` / `~/.claude.json` 의 관련 항목**만** 제거 (다른 항목 보존)
- 사전 백업 `*.bak.<timestamp>` 자동 생성
- `notify-api.env` 는 보존됨 (수동 삭제 필요)

### NAS 측

```bash
cd /volume1/docker/claude-kakao-notify/nas
sudo docker compose down
# 토큰 파일까지 제거할 거면:
# sudo rm -rf data/ .env
```

---

## 📁 리포지토리 구성

```
claude-kakao-notify/
├── README.md                  ← 본 문서
├── LICENSE                    ← MIT
├── .gitignore
│
├── install.ps1                ← PC 클라이언트 설치 스크립트
├── uninstall.ps1              ← PC 클라이언트 제거 스크립트
│
├── files/                     ← PC ~/.claude/ 에 배치되는 파일 원본
│   ├── hooks/notify.py        ← 세션 이벤트 hook
│   ├── mcp/notify-mcp/server.py  ← notify MCP 툴 (FastMCP stdio)
│   ├── commands/rcd.md        ← /rcd 슬래시 커맨드 정의
│   └── notify-api.env.example
│
├── tools/
│   └── merge_config.py        ← settings.json / ~/.claude.json JSON 머지 헬퍼
│
└── nas/                       ← NAS 측 컨테이너 자산
    ├── Dockerfile
    ├── app.py                 ← FastAPI 게이트웨이 + 멀티테넌트 라우팅 (v2.0)
    ├── docker-compose.yml
    ├── .env.example
    ├── kakao_config.json.example
    └── tools/
        ├── get_initial_token.py  ← 사용자가 PC에서 Kakao OAuth 1회 진행
        └── add_tenant.py         ← 관리자가 NAS에서 테넌트 add/list/remove/migrate
```

---

## 🔐 보안 권고

- **`NOTIFY_API_KEY`** 는 `openssl rand -base64 32` 등으로 생성한 강한 랜덤 문자열 사용.
- 인터넷 직접 노출 시 **HTTPS** 권장 — 평문 HTTP 노출은 키 도청 위험.
- **`kakao_token.json`** 은 호스트에서 `chmod 600` 유지. 컨테이너는 `--user 1026:100` 으로 실행되어 atomic replace 시 호스트 소유권 보존.
- **`.env` / 토큰 파일** 절대 git 커밋 금지 (`.gitignore` 기본 적용).
- 이 리포의 `notify-api.env.example` 처럼 placeholder 값만 커밋.

---

## 🙋 기여 / 라이선스

**MIT License** — `LICENSE` 파일 참조. 자유롭게 fork/수정/사용하셔도 됩니다.

이슈/PR 환영합니다.

---

<div align="center">

**Made with ☕ by [parkbohyun](https://github.com/parkbohyun)**

</div>
