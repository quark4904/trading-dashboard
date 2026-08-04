# Trading Dashboard MVP

토스증권, 한국투자증권, 업비트 연동을 목표로 한 자동매매 대시보드 MVP입니다.

현재 버전은 안전을 위해 실제 주문을 전송하지 않습니다. 전략 관리와 손익 확인 중심이며, `.env` 키는 백엔드에서만 읽습니다.

## 선택한 구성

- Backend: Python 표준 라이브러리 HTTP 서버 + SQLite
- Frontend: 정적 HTML/CSS/JavaScript
- Storage: `data/trading_dashboard.db`

FastAPI와 React를 설치하지 않아도 바로 실행되는 MVP를 우선 만들었습니다. 기능이 안정되면 백엔드를 FastAPI로, 프론트엔드를 React/Vite로 옮기기 쉬운 API 경계를 유지했습니다.

## 실행

```bash
python3 -m app.main
```

브라우저에서 `http://127.0.0.1:8765`로 접속합니다.

다른 프로세스가 8765 포트를 사용 중이면 포트를 바꿔 실행합니다.

```bash
python3 -m app.main --port 8766
```

## Docker 실행

우분투 서버에서는 Docker Compose로 실행하는 구성을 기본으로 사용합니다.
Compose 설정 파일은 `compose.yaml`입니다.

```bash
docker compose up -d --build
```

기본 Compose 설정은 포트를 서버의 로컬 인터페이스에만 열어 둡니다.
Cloudflare Tunnel의 서비스 URL은 `http://localhost:8765`로 설정하세요.

운영 시 데이터와 설정은 컨테이너 밖에 둡니다.

- `.env`: Compose가 읽는 API 키와 계좌 설정
- `./data/trading_dashboard.db`: SQLite 데이터베이스

컨테이너 로그 확인:

```bash
docker compose logs -f
```

중지:

```bash
docker compose down
```

## 작업 완료 및 배포

변경 작업은 관련 검증에 문제가 없을 때 **커밋 → 푸시 → Docker Compose 배포 → 배포 후 확인** 순서로 반영합니다. 세부 기준과 중단 조건은 [`docs/work-completion-policy.md`](docs/work-completion-policy.md)에 정리되어 있으며, 프로젝트 작업 지침은 [`AGENTS.md`](AGENTS.md)에서 관리합니다.

## 포함된 기능

- 전체 손익 요약
- 플랫폼별 손익
- 종목별 손익
- `.env` 기반 플랫폼 키 설정 여부 표시
- 업비트 실제 잔고 동기화
- 한국투자증권 실제 잔고 동기화
- 토스증권 실제 잔고 동기화
- 업비트·한국투자증권·토스증권 실제 체결 이력 동기화
- 실제 체결 수수료·세금 우선 저장 및 공식 요율 추정값 구분 표시
- 평가금액 100원 미만 자산 별도 분리
- 전략 추가 및 활성/중지 관리
- 전략 실행 로그 조회
- SQLite 기반 전략/실행 기록 저장
- 플랫폼별 동기화 성공·실패 이력 저장
- 계좌별 독립 동기화 및 부분 실패 표시
- 자산별 사용자 한글 별칭 등록·수정·삭제
- USD/KRW 환율 이력 저장 및 조회 실패 시 마지막 정상값 사용
- 전략·별칭 API 입력 검증과 동적 HTML 이스케이프
- 플랫폼별 주문 capability 기반 DCA 입력·검증
- 토스·한국투자·업비트용 DRY_RUN 주문 요청 컴파일
- 공식 수수료 정책·사용자 override·업비트 실시간 요율 기반 DRY_RUN 예상 비용 기록
- 주문 전 플랫폼 현금·최소 금액·거래시간·종목 가능 여부 검증
- 전략별 일일 예산·최대 주문 횟수 제한과 검증 실패 이력 저장
- 주문별 멱등성 키와 사전 거부 취소 정책 기록
- 외부 API 재시도·지수 백오프·호출 간격 제한
- 플랫폼별 동기화·전략 실행 lease 잠금과 운영 장애 알림
- SQLite Online Backup 기반 백업·복구 및 스키마 마이그레이션 이력
- HTTP API 라우팅·외부 API 재시도 mock 테스트

플랫폼별 주문 필드와 공식 자료 근거는
[`docs/platform-order-requirements.md`](docs/platform-order-requirements.md)에 정리되어 있습니다.

## DRY_RUN 거래 비용

DCA 전략은 [`config/fee-policies.json`](config/fee-policies.json)의 공식 기본 정책을
사용합니다. 정책 파일은 실행할 때마다 다시 읽으므로 요율을 수정하면 다음 DRY_RUN부터
적용되며 앱을 재빌드하거나 재시작할 필요가 없습니다.

적용 우선순위:

1. 플랫폼·시장별 공식 기본 정책
2. 전략에 입력한 계좌별 수수료·세금 override
3. 업비트 `/v1/orders/chance`에서 조회한 실제 매수 수수료율

현재 기본 정책은 업비트 KRW 마켓, 토스 국내 KRX/NXT·미국주식, 한국투자 BanKIS 온라인
국내주식·ETF/ETN 요율을 구분합니다. 토스 미국주식은 주문 금액 10 USD 이하 수수료 면제
규칙도 적용합니다. 한국투자는 계좌별 우대 요율이 다를 수 있으므로 전략의 직접 설정값으로
덮어쓸 수 있습니다. 슬리피지는 공식 요율이 아니므로 계속 사용자 가정값을 사용합니다.

- 금액 주문: 입력한 주문 금액을 원금으로 예상 비용과 총 소요액 계산
- 수량 주문: 같은 플랫폼·종목의 최근 동기화 현재가가 있을 때만 계산
- 기준가가 없는 수량 주문: 주문은 기록하되 예상 비용은 `기준가 없음`으로 표시
- 주문 이력: 적용 수수료율과 공식 정책·사용자 설정·실시간 API 중 어느 출처인지 표시

## 실제 체결 이력과 수수료

잔고 동기화 버튼은 최근 체결 이력도 함께 가져옵니다. 기본 조회 기간은 최근 90일이며,
동일한 외부 주문 ID는 덮어써서 반복 동기화해도 중복 저장되지 않습니다.

- 업비트: 종료 주문의 `paid_fee`를 실제 수수료로 저장
- 토스증권: 주문 이력의 `execution.commission`, `execution.tax`를 실제값으로 저장
- 한국투자증권: 주문별 체결 수량·평균가·금액을 저장하고, 주문별 수수료 필드가 없으므로
  공식 요율로 계산한 값을 `추정`으로 명시
- 실제 수수료가 누락된 응답: 공식 정책 요율을 보조값으로 사용

조회 기간은 1~90일 범위에서 조정할 수 있습니다.

```dotenv
TRADING_DASHBOARD_EXECUTION_HISTORY_DAYS=90
```

업비트 API 키에는 잔고 조회 권한 외에 주문 조회 권한이 필요합니다. 실제 체결 이력은
대시보드의 `거래 및 전략 실행 로그`에서 확인할 수 있습니다. 이 데이터는 체결 비용 확인용이며,
일부 기간만 가져온 수수료를 현재 보유 자산 전체 원가에 임의로 더하지는 않습니다.

다른 정책 파일을 사용하려면 다음 환경 변수를 설정합니다.

```dotenv
TRADING_DASHBOARD_FEE_POLICY_PATH=/path/to/fee-policies.json
```

Docker Compose에서는 로컬 `./config` 디렉터리를 읽기 전용으로 마운트하므로 호스트의 정책
파일을 수정하면 다음 실행에 바로 반영됩니다.

## 데모 데이터

새 데이터베이스는 기본적으로 빈 상태로 생성됩니다. 개발용 샘플 자산과 전략이 필요할 때만 아래 환경 변수를 설정합니다.

```bash
TRADING_DASHBOARD_SEED_DEMO=true python3 -m app.main
```

## API 키 만료일

동기화 상태 모달에서 만료일까지 남은 기간을 확인하려면 `.env`에 `YYYY-MM-DD` 형식으로 설정합니다.

```dotenv
UPBIT_KEY_EXPIRES_ON=2027-01-01
TOSSINVEST_KEY_EXPIRES_ON=2027-01-01
KIS_PENSION_KEY_EXPIRES_ON=2027-01-01
KIS_ISA_KEY_EXPIRES_ON=2027-01-01
```

만료 30일 전부터 주의 상태로 표시됩니다. 설정하지 않은 플랫폼은 `만료일 미설정`으로 표시됩니다.

## 다음 단계

1. 대시보드 인증 및 권한 분리
2. HTTPS 리버스 프록시와 외부 접근 제어
3. 비밀값 관리 방식 정리 및 `.env.example` 제공
4. 백테스트와 모의매매
5. FastAPI + React 전환
