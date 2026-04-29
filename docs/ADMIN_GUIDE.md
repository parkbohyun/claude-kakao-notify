# 🛠️ NAS 관리자 가이드

> NAS notify-api 컨테이너를 운영하면서 다른 사용자(테넌트)를 추가/관리하는 작업 흐름.
> 멀티테넌트(v2.0) 모드 전제. 레거시 단일 테넌트는 하위 호환으로 그대로 동작.

---

## 📦 사전 점검

```bash
ssh admin@nas

# 컨테이너 v2.0 인지 확인
sudo docker ps --filter name=notify-api --format '{{.Image}}'
# 기대: notify-api:2.0

# 멀티테넌트 모드 동작 중인지
curl http://localhost:8002/health
# 기대: {"ok":true,"mode":"multi-tenant","tenants_total":N,"tenants_healthy":N}
```

`mode: legacy`로 보이면 `tenants.json`이 없는 상태 → [마이그레이션](#-레거시--멀티테넌트-마이그레이션) 먼저.

---

## 🤖 챗봇으로 가입 승인하기 (권장 — 신규 사용자용)

신규 사용자가 카카오 채널에서 `/가입 <name>` 으로 신청하면, OAuth 완료 시점에 **관리자 본인 카톡으로 승인 요청 푸시**가 옵니다:

```
🆕 [신규 가입 신청] alice
━━━━━━━━━━━━━━
OAuth 완료. 승인 페이지로 이동:
[승인 페이지] 버튼
```

### 절차

1. 카톡 메시지의 **[승인 페이지]** 버튼 클릭
2. 브라우저에 사용자 정보(이름, OAuth 완료 시각, client_id 등) 표시
3. **[✅ 승인]** 또는 **[❌ 거부]** 클릭
4. 승인 시 자동 처리:
   - `/data/tenants/<name>/kakao_{config,token}.json` 저장
   - `tenants.json` 에 새 엔트리 + 새 API 키 발급
   - 신청한 사용자 `bot_user_id` 자동 페어링 (`notify_prefs.json`)
   - 추가 디바이스용 페어링 코드 발급 (10분)
5. 사용자가 카카오 채널에서 `/가입상태 <name>` 입력 시 API 키 + 페어링 코드 받음

### 환경 설정

이 흐름이 동작하려면 kakao-skill 컨테이너에 다음 env 가 필요:

```bash
docker run -d --name kakao-skill --restart=unless-stopped \
  -p 8001:8000 \
  -e PUBLIC_BASE_URL=https://dhub-ds.synology.me:8003 \
  -e ADMIN_NOTIFY_API_KEY=<관리자 본인 NOTIFY_API_KEY> \
  -e ADMIN_TENANT_ID=parkbohyun \
  -v /volume1/docker/scripts:/data \
  -v /volume1/docker/backups:/data/backups:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  kakao-skill:latest
```

| Env | 용도 |
|---|---|
| `PUBLIC_BASE_URL` | 외부에서 접근 가능한 base URL (OAuth redirect, 승인 페이지에 노출) |
| `ADMIN_NOTIFY_API_KEY` | 관리자 본인의 notify-api API 키 — 신규 가입 시 푸시 발송 인증 |
| `ADMIN_TENANT_ID` | 관리자 tenant id (기본 `parkbohyun`) |
| `NOTIFY_API_URL` | notify-api 컨테이너 주소 (기본 `http://172.30.1.77:8002`) |

또한 사용자가 카카오 앱에 등록할 **Redirect URI** 가 `${PUBLIC_BASE_URL}/oauth/callback` 이어야 합니다 (`NEW_USER_GUIDE.md` 1단계 6번 참고).

### 신청자 식별

`bot_user_id` 는 카카오 챗봇이 부여하는 익명 hash 라 누가 신청했는지 직접 보이진 않습니다. **사용자가 입력한 사용자명(`name`)** 을 합의된 식별자로 사용합니다 (예: 사내에서 `alice` 으로 통용).

### 코드 재발급 (추가 디바이스)

기존 사용자가 다른 디바이스에서 토글하고 싶다면:
- 사용자: `/코드요청 alice` → 관리자에게 푸시
- 관리자: 승인 페이지에서 [승인]
- 사용자: `/코드확인 alice` → 코드 회신 → 그 디바이스에서 `/연동 <코드>`

코드 요청은 30분 내 승인 필요, 코드 자체는 10분 만료.

---

## ➕ 새 테넌트 추가 (수동 — 챗봇 사용 못 할 때)

### 1. 사용자에게 안내
[`docs/NEW_USER_GUIDE.md`](NEW_USER_GUIDE.md) 링크를 사용자에게 전달. 사용자가 1~3단계까지 직접 진행해서 두 파일(`kakao_config.json`, `kakao_token.json`)을 안전한 경로로 보내옵니다.

### 2. 토큰 파일 받기

사용자별로 임시 작업 디렉터리에 보관:

```bash
ssh admin@nas
mkdir -p /tmp/onboard/<tenant-id>
# 사용자가 보낸 두 파일을 여기에 배치 (scp/usb/etc)
ls /tmp/onboard/<tenant-id>/
# kakao_config.json
# kakao_token.json
```

### 3. add_tenant.py 다운로드 (NAS에 git 미설치 환경)

```bash
mkdir -p /tmp/cnk-tools
curl -fsSL \
  https://raw.githubusercontent.com/parkbohyun/claude-kakao-notify/main/nas/tools/add_tenant.py \
  -o /tmp/cnk-tools/add_tenant.py
```

### 4. 테넌트 등록

```bash
python3 /tmp/cnk-tools/add_tenant.py add <tenant-id> \
    --config /tmp/onboard/<tenant-id>/kakao_config.json \
    --token  /tmp/onboard/<tenant-id>/kakao_token.json \
    --data-dir /volume1/docker/scripts
```

출력 예:
```
✓ Tenant 'aliceB' added.
  config: /volume1/docker/scripts/tenants/aliceB/kakao_config.json
  token : /volume1/docker/scripts/tenants/aliceB/kakao_token.json

─── API KEY (저장하세요 — 다시 출력되지 않습니다) ──────────────
xQ8vNm-3RzKj5TpQwEfYa7hDcLbR4NsVuXyZMpKwJqB
─── 클라이언트 ~/.claude/notify-api.env 의 NOTIFY_API_KEY 로 사용 ─

─── 페어링 코드 (카카오 채널에서 알림 ON/OFF 토글하려면 1회 입력) ──
  코드: 2HBPSQ
  만료: 2026-04-29T14:38:59  (10분)
  사용자 → 카카오 채널에서: /연동 2HBPSQ

힌트: 컨테이너 재시작 불필요 — tenants.json 변경은 즉시 반영됨.
```

API 키 + 페어링 코드 모두 사용자에게 안전 채널로 전달. 페어링은 사용자가 카카오 채널에서 토글(ON/OFF) 하고 싶을 때만 1회 필수 — 안 하면 기본 ON 으로 동작 (기존 동작 동일).

### 5. API 키를 사용자에게 안전하게 전달

[새 사용자 가이드의 토큰 전달 섹션](NEW_USER_GUIDE.md#3️⃣-토큰-파일을-관리자에게-전달)과 동일한 채널 권장 (SCP / 암호화 ZIP / 사내 보안 메신저).

### 6. 임시 파일 정리

```bash
shred -u /tmp/onboard/<tenant-id>/kakao_*.json
rmdir /tmp/onboard/<tenant-id>
```

> 등록 단계에서 `add_tenant.py`가 파일을 `/volume1/docker/scripts/tenants/<id>/`로 복사했으므로 `/tmp/onboard/`의 원본은 더 이상 필요 없음.

---

## 📋 테넌트 관리 명령

```bash
PYAT="python3 /tmp/cnk-tools/add_tenant.py --data-dir /volume1/docker/scripts"

# 등록된 테넌트 목록 (해시/키 미노출)
$PYAT list

# 테넌트 제거 (데이터 디렉터리 보존)
$PYAT remove <tenant-id>

# 테넌트 제거 + 데이터까지 삭제
$PYAT remove <tenant-id> --purge

# 페어링 코드 재발급 (이전 코드가 만료/분실됐을 때)
$PYAT pair <tenant-id>
```

`tenants.json` 변경은 컨테이너 mtime 캐시로 **즉시 반영** (재시작 불필요).

---

## 🔄 레거시 → 멀티테넌트 마이그레이션

기존 v1.0 단일 테넌트 배포에서 멀티테넌트로 전환할 때 1회만:

### 옵션 A: 토큰 파일 위치 유지 (in-place — 호스트 스크립트 호환)

호스트 측 `kakao_notify.sh` 같은 스크립트가 `/data` 루트의 토큰 파일을 직접 사용하는 경우, 파일을 **이동시키지 않고** `data_dir` 트릭으로 등록:

```bash
KEY='<현재 NOTIFY_API_KEY 값>'
HASH=$(python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$KEY")
NOW=$(date -u +%Y-%m-%dT%H:%M:%S)
cat > /volume1/docker/scripts/tenants.json <<JSON
{
  "tenants": [
    {
      "id": "<your-tenant-id>",
      "api_key_sha256": "$HASH",
      "data_dir": "/data",
      "added_at": "$NOW",
      "note": "in-place migration"
    }
  ]
}
JSON
chmod 600 /volume1/docker/scripts/tenants.json
```

기존 클라이언트 변경 불필요 — 같은 API 키 그대로 작동.

### 옵션 B: 표준 디렉터리로 이동 (`add_tenant.py migrate`)

호스트 측 다른 의존성이 없으면 표준 위치 `/data/tenants/<id>/`로 이동:

```bash
NOTIFY_API_KEY='<현재 키>' python3 /tmp/cnk-tools/add_tenant.py migrate <tenant-id> \
    --data-dir /volume1/docker/scripts
```

이후 `docker-compose.yml`의 `NOTIFY_API_KEY` env는 무시됨 (제거 가능).

---

## 🔁 컨테이너 업그레이드 (notify-api 코드 갱신)

리포 코드를 갱신하고 NAS에 적용할 때:

```bash
ssh admin@nas
cd /volume1/docker/notify-api

# 백업
TS=$(date +%Y%m%d_%H%M%S)
cp -p app.py "app.py.bak.$TS"
cp -p Dockerfile "Dockerfile.bak.$TS"

# 새 코드 가져오기
curl -fsSL https://raw.githubusercontent.com/parkbohyun/claude-kakao-notify/main/nas/app.py -o app.py
curl -fsSL https://raw.githubusercontent.com/parkbohyun/claude-kakao-notify/main/nas/Dockerfile -o Dockerfile

# 새 이미지 빌드 (이전 태그 보존)
sudo docker tag notify-api:2.0 notify-api:2.0-prev || true
sudo docker build -t notify-api:2.0 .

# 컨테이너 교체
sudo docker stop notify-api && sudo docker rm notify-api
sudo docker run -d --name notify-api --restart=always \
  --user 1026:100 -p 8002:8000 \
  -v /volume1/docker/scripts:/data \
  notify-api:2.0

# 검증
sleep 3
curl http://localhost:8002/health
```

### 롤백 (필요 시)

```bash
sudo docker stop notify-api && sudo docker rm notify-api
cp app.py.bak.<TS> app.py
sudo docker run -d --name notify-api --restart=always \
  --user 1026:100 -p 8002:8000 \
  -v /volume1/docker/scripts:/data \
  notify-api:2.0-prev    # 또는 notify-api:1.0 (레거시 롤백 시 NOTIFY_API_KEY env 필요)
```

---

## 🩺 모니터링 / 트러블슈팅

```bash
# 컨테이너 로그 (마지막 100줄)
sudo docker logs notify-api --tail 100

# 실시간 로그
sudo docker logs notify-api -f

# 헬스
curl http://localhost:8002/health

# 테넌트별 토큰 파일 존재 여부
ls -la /volume1/docker/scripts/tenants/*/kakao_*.json
```

| 증상 | 원인 가능성 |
|------|-------------|
| `mode: legacy` 인데 멀티테넌트 원함 | `/volume1/docker/scripts/tenants.json` 파일이 없거나 부적절한 권한 |
| `tenants_healthy < tenants_total` | 어떤 테넌트의 `kakao_config.json` 또는 `kakao_token.json` 파일 누락 |
| 모든 발송이 401 | `tenants.json`의 hash가 실제 클라이언트 키와 불일치 |
| 일부 사용자만 401 | 그 사용자의 `tenants.json` 엔트리만 hash 불일치 — 재발급 필요 |
| 카톡이 다른 사람 계정으로 도착 | 토큰 파일과 테넌트 ID 매핑 오류 — 해당 `data/tenants/<id>/kakao_token.json` 검수 |

---

## 🔐 보안 체크리스트

- [ ] `/volume1/docker/scripts/tenants.json` 권한 `600` (소유자만 읽기/쓰기)
- [ ] `/volume1/docker/scripts/tenants/*/kakao_*.json` 권한 `600`
- [ ] 컨테이너 `--user 1026:100`로 실행 (호스트 소유권 보존)
- [ ] HTTPS 노출 (시놀로지 리버스 프록시 + Let's Encrypt) — 평문 HTTP 외부 노출 회피
- [ ] 사용자별 `add_tenant.py` 출력 API 키는 1회만 출력되니 그 자리에서 안전한 채널로 전달
- [ ] 임시 토큰 파일(`/tmp/onboard/<tenant-id>/`)은 등록 후 `shred -u`로 삭제
- [ ] 백업 파일(`*.bak.<TS>`)에 비밀이 들어있을 수 있는지 주기 점검 (`tenants.json` 백업은 hash만 — 안전)

---

## 📡 i.kakao 챗봇 블록 등록 (1회 작업)

챗봇 흐름 동작에 필요한 블록 (시나리오에 추가):

| 블록 이름 | 사용자 발화 (예시) | 스킬 URL (POST) |
|---|---|---|
| `가입` | `/가입 alice`, `가입 alice`, `/가입` | `${PUBLIC_BASE_URL}/register-start` |
| `가입상태` | `/가입상태 alice`, `가입상태 alice` | `${PUBLIC_BASE_URL}/register-status` |
| `코드요청` | `/코드요청 alice`, `코드요청 alice` | `${PUBLIC_BASE_URL}/code-request` |
| `코드확인` | `/코드확인 alice`, `코드확인 alice` | `${PUBLIC_BASE_URL}/code-check` |

각 블록 공통:
- 응답: **스킬데이터 사용** (서버가 `simpleText` 그대로 반환)
- 파라미터: 불필요 (서버가 utterance 에서 사용자명 파싱)
- 학습 발화: 다양한 이름으로 5~10개 등록 (`/가입 alice`, `/가입 bob`, ... → NLU 일반화)

기존 블록들 (`/연동`, `/알림 켜기`, `/알림 끄기`, `/알림 상태`, `/상태`, `/정보`, `/버전`, `/백업`)은 그대로 유지.

배포 (i.kakao 우상단 [배포]) 후 카카오 채널에서 동작.

---

## 📚 참고

- 메인 문서: [README.md](../README.md)
- 새 사용자 가이드: [docs/NEW_USER_GUIDE.md](NEW_USER_GUIDE.md)
- 코드:
  - `nas/app.py` (FastAPI 게이트웨이 — notify-api)
  - `nas/kakao-skill/skill_server.py` (챗봇 스킬 + 가입 흐름)
  - `nas/kakao-skill/onboarding.py` (등록/코드요청 상태 머신)
  - `nas/tools/add_tenant.py` (수동 관리 도구)
