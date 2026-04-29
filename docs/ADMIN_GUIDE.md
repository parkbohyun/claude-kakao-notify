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

## ➕ 새 테넌트 추가

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

힌트: 컨테이너 재시작 불필요 — tenants.json 변경은 즉시 반영됨.
```

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

## 📚 참고

- 메인 문서: [README.md](../README.md)
- 새 사용자 가이드: [docs/NEW_USER_GUIDE.md](NEW_USER_GUIDE.md)
- 코드: `nas/app.py` (FastAPI 게이트웨이), `nas/tools/add_tenant.py` (관리 도구)
