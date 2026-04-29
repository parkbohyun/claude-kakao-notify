---
description: 채팅에 보이는 Claude Code remote-control URL을 카톡으로 전송
argument-hint: "[URL] (생략 시 최근 채팅에서 자동 추출)"
allowed-tools: ["mcp__notify__notify"]
---

# RC URL 카톡 전송

전달받은 인자: `$ARGUMENTS`

## 할 일

1. **URL 확보**
   - 위 인자가 비어있지 않다면 그것을 URL로 사용.
   - 비어있다면, 이 대화의 최근 메시지/명령 출력에서 `https://claude.ai/code/...` 형태의 remote-control URL을 찾아라. 가장 최근에 등장한 것을 사용.
   - 못 찾으면 **딱 한 줄로** "rcd: URL을 찾지 못함. `/rcd <URL>`로 직접 전달해주세요." 라고 응답하고 즉시 종료.

2. **즉시 발송** — `mcp__notify__notify` 툴을 다음 인자로 호출:
   - `message`: `🔗 [Claude Code] 원격 접속 URL\n\n<확보한 URL>` (실제 줄바꿈 두 번 후 URL을 본문에 직접 포함)
   - `url`: 위에서 확보한 URL
   - `button`: `원격 접속`

3. **결과 보고** — 한 줄로 `rcd: 발송 완료 → <URL>` 또는 실패 시 `rcd: 발송 실패 — <사유>`.

## 제약

- 다른 작업 일절 하지 말 것 (요약/설명/추가 도구 호출 금지).
- URL 검증: `https://`로 시작하고 `claude.ai`를 포함해야 함. 아니면 사용자에게 확인받지 말고 그대로 발송 (사용자가 명시 인자 전달했다면 그 의도를 신뢰).
- 사용자에게 추가 질문하지 말 것.
