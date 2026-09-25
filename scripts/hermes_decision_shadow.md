# Hermes Decision Plane — Phase A Shadow

상태: **수동 one-shot CLI source 구현. 기본 비활성. 실제 Qwen inference, 운영 설치·채택, 자연 실행 및 외부 성과는 미검증.**

구현은 [hermes_decision_shadow.py](hermes_decision_shadow.py), native 계약 검증은 [test_hermes_decision_shadow.py](../tests/scripts/test_hermes_decision_shadow.py)에 있다. 상위 설계는 Ops-Hub의 `docs/ADR/0004-openjev-local-decision-boundaries.md` v3이다.

## 소유권과 비개입

이 CLI는 기존 작업을 실행하거나 감싸지 않는다. 운영자가 이미 허용된, 최소한의 비밀 없는 evidence를 별도로 제공하면 retrieval 필요 여부만 `YES / NO / ABSTAIN`으로 추천한다. 결과는 언제나 `shadow=true`, `applied=false`이다. 프로세스 exit 0은 관측 결과를 출력했다는 뜻이지 workflow 성공·검증 PASS·QA Acceptance가 아니다.

Hermes Agent의 orchestration, 기존 worker/model/profile, source ACL, RAG 실행, verification, retry, Git/push, deploy, process lifecycle, 외부 mutation은 그대로다. 자동 hook, ContextEngine, MemoryProvider, 새 scheduler, 새 DB, daemon, Gateway tool 또는 model endpoint를 등록하지 않는다. `recommend()`와 `main()`은 이 CLI의 내부 구현·native 테스트 seam이며, 실행 중인 agent에 import하여 callback으로 붙이는 방식은 Phase A의 지원 경계가 아니다.

자동 수집과 outcome correlation도 하지 않는다. 기존 session/job/receipt/event를 변경하지 않고 stdout의 ephemeral 결과만 사용한다. 별도 Decision Ledger와 조사 유형 taxonomy는 구현하지 않았다.

## 설정과 승인된 실행 경계

기존 owning profile의 `config.yaml`에서 별도 task namespace를 사용한다. 기존 `memory_query_rewrite`나 main model 설정을 상속하지 않는다. 다음은 설명용이며 **이번 변경에서 어떤 운영 config에도 추가하거나 활성화하지 않았다.**

```yaml
auxiliary:
  hermes_decision_shadow:
    enabled: false
    provider: custom
    model: qwen-hermes
    base_url: http://192.168.1.8:1234/v1
    api_mode: chat_completions
    policy_revision: hermes-shadow-retrieval-v1
    timeout: 5
```

명시적 boolean `true`만 enable한다. 누락·비활성은 stdin도 읽지 않고 `ABSTAIN/disabled`를 반환한다. URL, model, provider, protocol, policy가 일치하지 않거나 알 수 없는 설정 필드가 있으면 보류한다. timeout은 유한한 수 `0 < timeout <= 10`초만 허용한다. 새 환경변수나 새로운 profile은 만들지 않는다.

실제 모델 요청은 **현재 계약에서 허용한 로컬 운영자 실행 경로에서만** 사용한다. Hanil MCP Gateway의 AI relay 금지를 이 스크립트, shell wrapper 또는 테스트로 우회하지 않는다. Gateway 세션에서 실제 생성은 실행하지 않았다.

허용된 로컬 운영자가 별도 승인 후 해당 profile을 명시적으로 활성화했을 때, checkout root에서 다음 형태로 사용한다. 입력 파일은 운영자가 준비한 bounded JSON이며 자동 수집 파일이 아니다.

```powershell
Get-Content -Raw -Encoding utf8 .\shadow-request.json |
    .\.venv\Scripts\python.exe -X utf8 -m scripts.hermes_decision_shadow
```

반환 recommendation을 기존 실행 조건으로 사용하지 않는다. 실환경 관측 전에는 single-slot Qwen과의 자원 경합·queue latency도 평가해야 한다. 이 CLI를 반복 예약하거나 gateway/agent loop에 자동 연결하는 변경은 포함되지 않았다.

## 요청·응답 계약

입력 예시의 `source_revision`은 **예시 baseline**이다. 운영자가 실제 조사 대상 revision을 별도로 확인하여 넣어야 한다.

```json
{
  "project_id": "hermes-agent-desktop-control",
  "target_id": "local",
  "source_revision": "cd75f6d76775d92468c1f44aca5d441a877102a4",
  "decision_kind": "retrieval_needed",
  "policy_revision": "hermes-shadow-retrieval-v1",
  "allowed_candidates": ["YES", "NO", "ABSTAIN"],
  "bounded_evidence": "A named symbol is missing; the relevant source range has not been inspected."
}
```

`bounded_evidence`는 비어 있지 않은 최대 4,000 문자이며 초과 입력은 자르지 않고 거부한다. stdin JSON 전체 상한은 24,000 문자이다. optional boolean `rule_decided=true`는 이미 deterministic rule로 충분하다는 호출자 표시이며 모델 없이 보류한다. 다른 task/category, 다른 project/target, 잘못된 revision 형식, 후보 변경, unknown fields는 허용하지 않는다.

기존 `agent.redact.redact_sensitive_text(force=True, redact_url_credentials=True)`로 방어적 redaction을 적용한 새 복사본만 전송한다. 원래 입력을 수정하지 않는다. redaction은 모든 비밀·개인정보를 알아내는 완전한 분류기가 아니므로 운영자는 원문 대화·credentials·개인정보를 입력해서는 안 된다.

정규화된 redacted evidence와 project/target/source/task/candidates/policy를 SHA-256 `request_id`에 결속한다. 이것은 **내용 결속 digest**이지 signature, 독립 source attestation, execution/attempt ID, 실제 Git HEAD 확인 또는 성공 증거가 아니다.

Qwen 응답은 `request_id`, `decision_kind`, `policy_revision`, `allowed_candidates`를 정확히 echo하고 `recommendation`과 optional `raw_score`만 추가해야 한다. 단일 choice, `finish_reason=stop`, 동일한 model, 비어 있지 않은 최대 2,000 문자 JSON만 받는다. duplicate keys, 비유한 수·boolean score, tool/function calls, refusal, 미지정 필드, binding 불일치를 거부한다. `raw_score`는 보정되지 않은 수치일 뿐 성공 확률이 아니며, 보류 결과에는 점수를 남기지 않는다. model의 자유 서술 이유는 출력하지 않는다.

## Qwen 경로·실패 처리

[기존 Auxiliary resolver](../agent/auxiliary_client.py)의 `aux_probe_mode()` + `resolve_provider_client()`로 명시적 custom route를 확인한 뒤 기존 설치 SDK의 `/v1/chat/completions`에 한 번만 요청한다. `call_llm()`의 semaphore 대기·재시도·provider fallback은 Shadow 요구보다 넓으므로 사용하지 않는다. 기존 `call_llm()` 자체와 memory rewrite 동작은 변경하지 않았다.

고정된 destination은 현재 Hermes Control/Qwen 소스 계약의 LAN policy proxy `192.168.1.8:1234`, model alias `qwen-hermes`이다. raw upstream `127.0.0.1:1235`와 cloud provider는 허용하지 않는다. Qwen `production.json` schema 4의 network client ACL 및 backend 정책은 기존 owner가 유지한다. CLI는 인증 권한을 새로 만들지 않는다. SDK의 `no-key-required`는 credential이 아닌 이 기존 경로용 placeholder이며, main/profile/cloud API key와 조직·project 식별자, 사용자 extra headers를 빌리지 않는다. 인증을 요구하는 방향으로 owner 계약이 바뀌면 현재 CLI는 실패 후 보류하며 자동 우회하지 않는다.

전용 async HTTP transport는 환경 proxy와 redirect를 사용하지 않고 SDK retries는 0이다. 생성은 `temperature=0`, `max_tokens=512`, non-stream이며 tool schema나 backend typed-readout 확장을 요구하지 않는다. async 생성에 timeout과 cancellation을 적용하고 소유한 연결만 닫는다. 동기 config/import/resolution 및 프로세스 시작 시간은 강제 선점되지 않으므로 timeout을 CLI 전체 wall-clock SLA로 표현하지 않는다. Qwen proxy의 실제 slot 해제와 모델 출력 적합성은 source를 읽은 것만으로 검증된 것이 아니다.

모든 disabled, unsupported, timeout, cancellation, empty/malformed, unknown candidate, candidate/policy/request mismatch, truncation 의심 및 provider 실패는 고정 reason code의 `ABSTAIN`이다. 기존 workflow를 실행·중단·재시도하지 않는다. CLI entrypoint는 **자기 프로세스의** Python logging을 끄므로 SDK DEBUG 설정에서도 전체 prompt/response를 로그에 기록하지 않는다. 기존 agent 프로세스의 로깅은 건드리지 않는다. 내부 함수를 다른 process owner에 embedding하는 경우 이 CLI 격리가 성립하지 않으므로 별도 승인·설계가 필요하다.

## Native 검증과 미검증 경계

기존 runner로 검증한다. 새 QA framework, test launcher, DB 또는 실제 모델 호출 fixture를 만들지 않았다.

```bash
scripts/run_tests.sh tests/scripts/test_hermes_decision_shadow.py -j 1 --file-retries 0
scripts/run_tests.sh tests/agent/test_context_engine_select_context.py tests/tools/test_delegate_capability_inheritance.py tests/tools/test_delegate_child_cache_ttl.py -j 2 --file-retries 0
```

신규 25개 native 사례는 실제 config loader, Auxiliary resolver 및 SDK를 import하고 HTTP transport만 fake 처리한다. disabled/no-stdin, YES/NO/ABSTAIN, timeout, deadline cancellation 종료, provider failure/no-retry, redirect 거부, malformed/empty/unknown/mismatch/truncation, 기존 redaction, 실제 CLI entrypoint DEBUG privacy, multiplex profile A→B→A 및 config/input 보존을 확인한다. 기존 context/cache/delegation 14개는 별도 회귀 경계다.

최초 정상 사례는 짧은 2초 테스트 설정에서 한 번 ABSTAIN이었고 단독 재실행은 통과했다. 원인은 확정하지 않았으며 일반 계약 사례는 10초 설정으로 변경하고 별도 2초 deadline 사례로 취소·종료를 검증했다. 이 변경을 실제 Qwen latency 또는 cold-start 적합성의 증거로 사용하지 않는다.

실제 Qwen inference, 실익·정확도·calibration, runtime adoption, 자연 실행, 외부 결과는 **NOT VERIFIED**이다. 다음 설계 방향은 기존 Auxiliary/Qwen 경로에서의 제한된 Adaptive RAG이지만, 승인된 로컬 실제 inference 및 Shadow 실익·경합·보류율 관측을 먼저 통과해야 한다. 그 전에는 routing/RAG/verification 권한을 활성화하지 않는다.
