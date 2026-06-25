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

브라우저에서 `http://서버IP:8765`로 접속합니다.

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

## 다음 단계

1. 수수료, 세금, 슬리피지 반영
2. 주문 전 리스크 엔진
3. 백테스트와 모의매매
4. FastAPI + React 전환
