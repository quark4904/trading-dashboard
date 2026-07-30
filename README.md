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

## 포함된 기능

- 전체 손익 요약
- 플랫폼별 손익
- 종목별 손익
- `.env` 기반 플랫폼 키 설정 여부 표시
- 업비트 실제 잔고 동기화
- 한국투자증권 실제 잔고 동기화
- 토스증권 실제 잔고 동기화
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
- 전략별 수수료·세금·슬리피지 가정과 DRY_RUN 예상 비용 기록

플랫폼별 주문 필드와 공식 자료 근거는
[`docs/platform-order-requirements.md`](docs/platform-order-requirements.md)에 정리되어 있습니다.

## DRY_RUN 거래 비용

DCA 전략에서 수수료·세금·슬리피지 비율을 직접 설정할 수 있습니다. 이 값은 증권사 공식
요율이 아닌 사용자 가정이며 실제 주문에는 적용되지 않습니다.

- 금액 주문: 입력한 주문 금액을 원금으로 예상 비용과 총 소요액 계산
- 수량 주문: 같은 플랫폼·종목의 최근 동기화 현재가가 있을 때만 계산
- 기준가가 없는 수량 주문: 주문은 기록하되 예상 비용은 `기준가 없음`으로 표시

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

1. 주문 전 리스크 엔진
2. 백테스트와 모의매매
3. FastAPI + React 전환
