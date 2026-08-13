# 접근 보안과 배포

이 문서는 외부 접근 경로, HTTPS 종료, 영속 데이터, 운영 상태 점검 구성을 다룬다. 일상적인
장애 대응과 SQLite 복구 명령은 [`operations.md`](operations.md)를 기준으로 한다.

## 외부 인증: Cloudflare Tunnel과 Access

이 운영 환경은 단일 사용자용이므로 Cloudflare Access를 유일한 외부 인증 계층으로 사용한다.
애플리케이션 비밀번호 로그인은 중복 인증이므로 비활성화한다.

1. Cloudflare Zero Trust에서 대시보드를 `Self-hosted application`으로 등록한다.
2. `dashboard.example.com` 전체를 보호하고, 본인 계정만 허용하는 Allow 정책을 만든다.
3. Tunnel의 원본 서비스는 `http://127.0.0.1:8765`로 설정한다.
4. 서버에서는 8765를 외부 인터페이스에 공개하지 않는다.

예시 Tunnel ingress:

```yaml
ingress:
  - hostname: dashboard.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

`.env`에서는 다음 값을 유지한다.

```dotenv
TRADING_DASHBOARD_AUTH_ENABLED=false
TRADING_DASHBOARD_COOKIE_SECURE=false
```

`.env`는 저장소에 커밋하지 않고 권한을 제한한다.

```bash
cp .env.example .env
chmod 600 .env
```

기본 Compose 설정은 포트를 `127.0.0.1`에만 열고, 외부 인증은 Cloudflare Access가 담당한다.
이 모드에서 대시보드 API는 단일 Access 사용자만 사용하므로 앱 내부의 `viewer/operator`
분리는 필요하지 않다. 앱 비밀번호 인증 코드는 비상용 선택 기능으로 남아 있지만 기본적으로
사용하지 않는다.

Tunnel을 사용하지 않고 Caddy `secure` profile로 직접 외부에 공개하는 경우 Caddy는 TLS만
제공하고 사용자 인증은 제공하지 않는다. 따라서 외부 공개 전에 앱 인증을 활성화하고 두
역할의 PBKDF2 해시를 설정해야 한다.

해시는 `.env`의 `TRADING_DASHBOARD_VIEWER_PASSWORD_HASH`와
`TRADING_DASHBOARD_OPERATOR_PASSWORD_HASH`에 각각 저장한다.

```dotenv
TRADING_DASHBOARD_AUTH_ENABLED=true
TRADING_DASHBOARD_COOKIE_SECURE=true
```

해시는 다음 명령으로 각각 생성할 수 있다. 비밀번호나 생성된 해시는 셸 기록과 저장소에
노출하지 않도록 주의한다.

```bash
docker compose exec trading-dashboard python -m app.auth hash-password
```

## HTTPS reverse proxy

Cloudflare Tunnel을 사용하는 경우 Cloudflare가 외부 HTTPS 종단을 담당하므로 Caddy는
필수가 아니다. Tunnel을 사용하지 않고 서버에서 직접 HTTPS를 종료할 때만 `secure` profile을
선택적으로 사용한다.

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8765/api/health
```

직접 HTTPS를 사용할 때만 다음을 실행한다.

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

자동 백업 실패는 운영 알림과 로그에 기록된다. 복구 절차는 [`operations.md`](operations.md)의
SQLite 백업·복구 항목을 따른다.
