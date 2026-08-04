# 접근 보안과 배포

## 인증과 권한

애플리케이션은 세션 쿠키와 CSRF 토큰을 사용한다.

- `viewer`: 포트폴리오·전략·주문·알림 조회
- `operator`: viewer 권한과 전략 수정, 동기화, DRY_RUN 실행, 알림 확인 처리
- 세션 쿠키는 `HttpOnly`, `SameSite=Strict`이며, HTTPS 운영에서는 `Secure`를 켠다.
- 상태 변경 요청은 세션별 CSRF 토큰을 `X-CSRF-Token` 헤더로 요구한다.
- 인증 활성화 상태에서 비밀번호 해시가 없으면 보호 API를 모두 거부한다.

비밀번호를 평문이나 일반 환경변수 값으로 저장하지 말고, 다음 명령으로 해시를 생성한다.

```bash
python3 -m app.auth hash-password
```

`.env`에 서로 다른 해시를 설정한다.

```dotenv
TRADING_DASHBOARD_AUTH_ENABLED=true
TRADING_DASHBOARD_VIEWER_PASSWORD_HASH=<generated-viewer-hash>
TRADING_DASHBOARD_OPERATOR_PASSWORD_HASH=<generated-operator-hash>
TRADING_DASHBOARD_COOKIE_SECURE=true
```

`.env`는 저장소에 커밋하지 않고 권한을 제한한다.

```bash
cp .env.example .env
chmod 600 .env
```

인증이 켜지면 `/api/health`와 로그인 화면·로그인 API만 공개되고, 나머지 API는 로그인과
역할 검사를 거친다. 인증이 꺼진 기본 Compose 설정은 로컬 개발과 기존 loopback 운영을
위한 호환 모드이며, 외부 접근을 허용하기 전에는 반드시 인증을 켠다.

## HTTPS reverse proxy

`deploy/Caddyfile`을 사용해 Caddy가 인증서 발급·갱신과 HTTPS 종단을 담당하고, 대시보드
컨테이너는 Compose 내부 네트워크에서만 Caddy에 연결한다.

1. `.env`에 인증 해시와 실제 도메인을 설정한다.
2. 서버 방화벽에서 80/443만 외부에 허용하고 8765는 localhost로 제한한다.
3. 보안 프로파일로 실행한다.

```bash
docker compose --profile secure up -d --build
docker compose ps
curl --fail https://dashboard.example.com/api/health
```

실제 도메인 대신 `localhost`를 사용할 때 Caddy의 내부 인증서를 사용하므로 브라우저에서
인증서 경고가 표시될 수 있다. 운영 도메인을 사용하면 Caddy가 공개 인증서를 관리한다.
`deploy/caddy-data`와 `deploy/caddy-config`는 인증서와 Caddy 상태를 보존하므로 삭제하지
않는다.

## 로그·상태 점검·백업

기본 운영 로그는 `/data/trading_dashboard.log`에 rotating file로 저장되고 컨테이너 표준
출력에도 남는다. `/api/health`는 SQLite `quick_check`, 스키마 버전, 미확인 알림 수, 인증
설정 상태를 반환한다.

Compose는 매일 `TRADING_DASHBOARD_BACKUP_TIME`에 SQLite Online Backup API로 데이터베이스를
백업하고 `TRADING_DASHBOARD_BACKUP_RETENTION_DAYS`보다 오래된 백업만 정리한다. 백업 디렉터리와
로그 파일은 `/data` 볼륨에 두므로 배포 시 데이터 볼륨을 유지한다.

```bash
curl --fail http://127.0.0.1:8765/api/health
docker compose logs --tail=100 trading-dashboard
docker compose exec trading-dashboard \
  python -m app.maintenance integrity --database /data/trading_dashboard.db
```

자동 백업 실패는 운영 알림과 로그에 기록된다. 복구 절차는 [`docs/operations.md`](operations.md)의
SQLite 백업·복구 항목을 따른다.
