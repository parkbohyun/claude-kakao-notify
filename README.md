# claude-kakao-notify

Claude Code 세션 이벤트(작업 시작/종료/사용자 입력 필요)와 `/remote-control` URL을
**카카오톡 '나에게 보내기'**로 자동 발송하는 설치 키트.

## 구성

- **Hook** (`~/.claude/hooks/notify.py`) — SessionStart/Stop/Notification 시 자동 카톡 발송
- **MCP** (`~/.claude/mcp/notify-mcp/server.py`) — 모델이 임의 시점에 호출 가능한 `notify` 툴
- **슬래시 커맨드** (`~/.claude/commands/rcd.md`) — `/rcd <URL>` 한 줄로 RC URL 발송

## 사전 조건

- **Windows + Claude Code**
- **Python 3.10+** (PATH 등록)
- **NAS notify-api 컨테이너**가 운영 중이며 외부에서 도달 가능 (DDNS/포트포워딩 또는 LAN/VPN)
- **API key** 발급되어 있음

## 설치

```powershell
git clone https://github.com/<YOUR-USER>/claude-kakao-notify.git
cd claude-kakao-notify
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

설치 중 다음을 입력 받음:

| 항목 | 예시 | 비고 |
|------|------|------|
| `NAS host` | `mynas.duckdns.org` 또는 `203.0.113.10` | DDNS 도메인 / 외부 IP / LAN IP |
| `NAS port` | `8002` | 외부 포트포워딩 포트 (기본 8002) |
| `API key` | `••••••••` | notify-api 컨테이너 발급 키 |

> 기존에 설치돼 있던 경우: 기존 값이 기본값으로 채워지며, Enter로 그대로 사용 가능.

## 설치되는 것

- `~/.claude/hooks/notify.py`
- `~/.claude/mcp/notify-mcp/server.py`
- `~/.claude/commands/rcd.md`
- `~/.claude/notify-api.env` (HOST/PORT/KEY)
- `~/.claude/settings.json` — `hooks.{SessionStart,Stop,Notification}` + `permissions.allow.mcp__notify__notify` 머지
- `~/.claude.json` — `mcpServers.notify` 머지
- 위 두 JSON 파일은 수정 직전 `*.bak.<timestamp>`로 자동 백업

`mcp` Python 패키지가 `--user` 모드로 설치됨.

## 사용

### 자동
Claude Code 세션 시작/종료/사용자 입력 대기 시 자동 카톡 도착.

### 수동 — RC URL 발송
1. Claude Code 채팅에서 `/remote-control` → URL 출력
2. 같은 채팅에서 `/rcd` (자동 추출 시도) 또는 `/rcd <URL>` (명시 전달)
3. 카톡 도착

### 수동 — 임의 메시지
모델이 `notify` MCP 툴을 호출하면 카톡 발송. 예: "끝나면 카톡으로 알려줘"

## 환경변수 (notify-api.env)

```
NOTIFY_API_HOST=...
NOTIFY_API_PORT=8002
NOTIFY_API_KEY=...
# 선택
# NOTIFY_API_SCHEME=https
# NOTIFY_API_PATH=/notify
# 또는 위 4개 대신:
# NOTIFY_API_URL=http://host:port/notify
```

## 디버깅

- Hook stdin 덤프: `~/.claude/hook-debug/<event>_<timestamp>.json`
- 발송 실패 메시지: stderr (Claude Code 자체 로그 또는 hook stdout)
- 환경변수 미설정 시 hook은 조용히 종료 (Claude Code 동작에 영향 없음)

## 제거

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
```

- settings.json / ~/.claude.json 의 관련 항목만 제거 (다른 항목은 보존, 사전 백업)
- `notify-api.env`는 보존됨 (비밀 보호 — 수동 삭제 필요)

## 네트워크 토폴로지 메모

```
[Claude Code (PC)]
        │ HTTPS/HTTP POST
        ▼
[NAS host:port]   ← 외부면 DDNS+포트포워딩, 내부면 LAN IP
        │
        ▼
[notify-api 컨테이너 (FastAPI)]
        │
        ▼
[Kakao /v2/api/talk/memo/default/send]
        │
        ▼
   카카오톡 '나에게 보내기'
```

NAS notify-api 컨테이너 셋업은 본 리포 범위 밖. 본 리포는 **클라이언트(=PC) 측 설치**만 다룸.

## 라이선스

내부 도구. 자유롭게 사용/수정.
