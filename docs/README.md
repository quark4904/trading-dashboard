# 문서 안내

Trading Dashboard의 문서를 목적별로 안내한다. 빠른 실행 방법은 저장소 루트의
[`README.md`](../README.md)를 먼저 보고, 운영 환경을 구성하거나 기능을 변경할 때는
아래 문서를 관련 범위에 맞게 확인한다.

## 권장 읽기 순서

| 목적 | 문서 | 다루는 내용 |
|---|---|---|
| 현재 개발 상태 확인 | [`development-roadmap.md`](development-roadmap.md) | 단계별 완료 상태와 다음 실주문 전환 기준 |
| 주문·비용·리스크 규칙 확인 | [`platform-order-requirements.md`](platform-order-requirements.md) | 플랫폼별 주문 필드, 체결 이력, 비용 정책, 주문 전 검증 |
| 백테스트 API 사용 | [`backtesting.md`](backtesting.md) | 요청 형식, 체결 규칙, 응답, 제한사항 |
| 외부 접근과 배포 구성 | [`security-and-deployment.md`](security-and-deployment.md) | Cloudflare Access, Caddy HTTPS, 로그, health check, 백업 구성 |
| 장애 대응과 데이터 유지보수 | [`operations.md`](operations.md) | 재시도, 잠금, 알림, SQLite 백업·복구·마이그레이션 |
| 변경사항 반영 절차 | [`work-completion-policy.md`](work-completion-policy.md) | 검증, 커밋, 푸시, 배포 전후 확인 기준 |

## 문서 경계

- 개발 로드맵은 상태와 완료 기준만 요약한다. 주문 세부 규칙은 플랫폼별 요구사항 문서를 기준으로 한다.
- 보안·배포 문서는 외부 접근과 Compose 구성을 다루고, 일상적인 장애 대응과 복구 명령은 운영 절차 문서를 기준으로 한다.
- 수수료 숫자와 외부 API 명세는 코드와 정책 파일의 변경에 따라 달라질 수 있다. 실제 주문을 도입하기 전에는 공식 제공자 문서를 다시 확인한다.
- 실행 가능한 동작의 최종 기준은 애플리케이션 코드, `compose.yaml`, `.env.example` 및 `config/`의 설정이다. 문서와 구현이 다르면 구현을 먼저 확인하고 문서를 갱신한다.

