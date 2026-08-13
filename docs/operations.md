# 운영 안정성 절차

4단계 운영 안정성 기능은 외부 API의 일시 장애가 발생해도 호출을 제한하고, 같은 플랫폼의
동기화와 주문 실행이 겹치지 않게 하며, 장애 원인을 대시보드 알림과 SQLite에 남기는 것을
목표로 한다.

## 외부 API 재시도와 호출 제한

업비트·한국투자·토스증권·환율 조회는 공통 HTTP 전송 정책을 사용한다.

- `GET` 요청은 네트워크 오류와 HTTP 429, 500, 502, 503, 504에 한해 최대 3회 재시도한다.
- 재시도 간격은 지수 백오프이며, 429 응답의 `Retry-After`가 있으면 그 값을 우선한다.
- 같은 제공자의 요청 사이에는 최소 간격을 둔다.
- `POST` 토큰 발급 요청은 중복 발급을 막기 위해 기본적으로 재시도하지 않는다. 향후 주문
  전송을 추가할 때는 외부 멱등성 키가 확인된 경우에만 별도 허용한다.

기본값을 운영 환경에 맞게 조정할 수 있다.

```dotenv
TRADING_DASHBOARD_RETRY_MAX_ATTEMPTS=3
TRADING_DASHBOARD_RETRY_BACKOFF_SECONDS=0.25
TRADING_DASHBOARD_RETRY_MAX_BACKOFF_SECONDS=4
TRADING_DASHBOARD_API_MIN_INTERVAL_SECONDS=0.05
```

제공자별 설정이 필요하면 `TRADING_DASHBOARD_UPBIT_*`, `TRADING_DASHBOARD_KIS_*`,
`TRADING_DASHBOARD_TOSS_*`, `TRADING_DASHBOARD_FX_*` 이름으로 공통 설정을 덮어쓸 수 있다.

## 플랫폼 잠금과 장애 알림

SQLite의 `operation_locks` 테이블에 `platform:<code>:operation` 잠금을 기록한다. 동기화와
전략 실행은 같은 플랫폼에서 동시에 수행되지 않으며, 잠금은 5분 lease를 사용해 프로세스가
비정상 종료되어도 만료 후 다시 획득할 수 있다.

동기화 실패, 전략 실행 실패, 스케줄러 예외, 잠금 경합은 `alerts` 테이블에 저장한다.
같은 원인의 확인하지 않은 알림은 하나로 합치고 발생 횟수를 증가시킨다.

- `GET /api/alerts`: 확인하지 않은 운영 알림 조회
- `GET /api/alerts?include_acknowledged=true`: 확인 처리된 알림 포함
- `PATCH /api/alerts/<id>?acknowledged=true`: 알림 확인 처리
- `GET /api/sync/status`: 최신 동기화, 잠금, 알림을 함께 조회
- `GET /api/maintenance/migrations`: 현재 스키마 버전과 적용 이력 조회

## SQLite 백업·복구

백업은 SQLite Online Backup API로 실행 중인 데이터베이스의 일관된 사본을 만든다. 운영
데이터베이스를 덮어쓰는 복구는 명시적인 `--force`가 필요하다.

컨테이너 내부에서 실행할 때는 `/data`가 호스트의 `./data`에 연결되어 있는지 확인한다.

```bash
# 백업
docker compose exec trading-dashboard \
  python -m app.maintenance backup \
  --output /data/trading_dashboard-$(date -u +%Y%m%dT%H%M%SZ).db

# 무결성 확인
docker compose exec trading-dashboard \
  python -m app.maintenance integrity --database /data/trading_dashboard.db

# 마이그레이션 이력 확인
docker compose exec trading-dashboard \
  python -m app.maintenance migrations --database /data/trading_dashboard.db

# 복구 전 현재 DB를 먼저 백업한 뒤 실행
docker compose exec trading-dashboard \
  python -m app.maintenance restore \
  --source /data/trading_dashboard-backup.db \
  --target /data/trading_dashboard.db \
  --force
```

복구 후에는 `docker compose restart trading-dashboard`와 `/api/health`, 보유 자산·전략 실행
이력 확인을 수행한다. `.env`와 데이터베이스는 백업 명령의 대상이 아니므로 별도로 보존한다.

스키마는 `schema_migrations`에 버전과 적용 시각을 기록한다. 애플리케이션 시작 시 누락된
마이그레이션을 idempotent하게 적용하며, 현재 버전은 3이다. 버전 3은 실제 체결의 출처와
전략 연결 정보를 추가하며 기존 체결은 `external`로 보존한다.
