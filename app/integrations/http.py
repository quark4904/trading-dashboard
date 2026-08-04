from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen


RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class HTTPTransportError(RuntimeError):
    """An external HTTP request failed after the configured retry policy."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.headers = headers or {}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0.25
    max_backoff_seconds: float = 4.0
    timeout_seconds: float = 12.0
    min_interval_seconds: float = 0.05


class RateLimiter:
    """Reserve a minimum interval between requests shared by a provider."""

    def __init__(
        self,
        min_interval_seconds: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._clock = clock
        self._sleeper = sleeper
        self._lock = Lock()
        self._next_allowed_at = 0.0

    def wait(self, *, sleeper: Callable[[float], None] | None = None) -> None:
        sleeper = sleeper or self._sleeper
        with self._lock:
            now = self._clock()
            delay = max(0.0, self._next_allowed_at - now)
            self._next_allowed_at = max(now, self._next_allowed_at) + self.min_interval_seconds
        if delay > 0:
            sleeper(delay)


_LIMITER_LOCK = Lock()
_PROVIDER_LIMITERS: dict[str, RateLimiter] = {}


def retry_policy(provider: str, *, timeout_seconds: float | None = None) -> RetryPolicy:
    key = provider.upper().replace("-", "_")
    return RetryPolicy(
        max_attempts=_env_int(
            f"TRADING_DASHBOARD_{key}_MAX_ATTEMPTS",
            _env_int("TRADING_DASHBOARD_RETRY_MAX_ATTEMPTS", 3, minimum=1, maximum=6),
            minimum=1,
            maximum=6,
        ),
        backoff_seconds=_env_float(
            f"TRADING_DASHBOARD_{key}_BACKOFF_SECONDS",
            _env_float("TRADING_DASHBOARD_RETRY_BACKOFF_SECONDS", 0.25, minimum=0, maximum=30),
            minimum=0,
            maximum=30,
        ),
        max_backoff_seconds=_env_float(
            f"TRADING_DASHBOARD_{key}_MAX_BACKOFF_SECONDS",
            _env_float("TRADING_DASHBOARD_RETRY_MAX_BACKOFF_SECONDS", 4.0, minimum=0, maximum=120),
            minimum=0,
            maximum=120,
        ),
        timeout_seconds=timeout_seconds
        if timeout_seconds is not None
        else _env_float(
            f"TRADING_DASHBOARD_{key}_TIMEOUT_SECONDS",
            _env_float("TRADING_DASHBOARD_HTTP_TIMEOUT_SECONDS", 12.0, minimum=1, maximum=60),
            minimum=1,
            maximum=60,
        ),
        min_interval_seconds=_env_float(
            f"TRADING_DASHBOARD_{key}_MIN_INTERVAL_SECONDS",
            _env_float("TRADING_DASHBOARD_API_MIN_INTERVAL_SECONDS", 0.05, minimum=0, maximum=10),
            minimum=0,
            maximum=10,
        ),
    )


def provider_rate_limiter(provider: str, policy: RetryPolicy) -> RateLimiter:
    key = provider.lower()
    with _LIMITER_LOCK:
        limiter = _PROVIDER_LIMITERS.get(key)
        if limiter is None:
            limiter = RateLimiter(policy.min_interval_seconds)
            _PROVIDER_LIMITERS[key] = limiter
        return limiter


def request_json(
    request: Request,
    *,
    provider: str,
    opener: Callable[..., Any] = urlopen,
    policy: RetryPolicy | None = None,
    limiter: RateLimiter | None = None,
    retry_non_idempotent: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[Any, dict[str, str]]:
    """Fetch and decode JSON with bounded retries for transient failures.

    Non-idempotent methods are not retried unless the caller explicitly opts in.
    This keeps a future order submission from being duplicated by a transport retry.
    """

    policy = policy or retry_policy(provider)
    limiter = limiter or provider_rate_limiter(provider, policy)
    method = (request.get_method() or "GET").upper()
    can_retry = retry_non_idempotent or method in IDEMPOTENT_METHODS
    last_error: HTTPTransportError | None = None

    for attempt in range(1, policy.max_attempts + 1):
        limiter.wait(sleeper=sleep_fn)
        try:
            with opener(request, timeout=policy.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
                return data, _response_headers(response)
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            finally:
                if getattr(exc, "fp", None) is not None:
                    exc.fp.close()
            headers = _response_headers(exc)
            last_error = HTTPTransportError(
                f"HTTP {exc.code}: {body[:4000]}",
                status=exc.code,
                body=body,
                headers=headers,
            )
            if not can_retry or exc.code not in RETRYABLE_STATUS_CODES or attempt >= policy.max_attempts:
                raise last_error from exc
            _sleep_before_retry(attempt, policy, headers, sleep_fn)
        except OSError as exc:
            last_error = HTTPTransportError(f"연결 실패: {exc}")
            if not can_retry or attempt >= policy.max_attempts:
                raise last_error from exc
            _sleep_before_retry(attempt, policy, {}, sleep_fn)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPTransportError(f"JSON 응답 해석 실패: {exc}") from exc

    raise last_error or HTTPTransportError("외부 API 요청이 실패했습니다.")


def _sleep_before_retry(
    attempt: int,
    policy: RetryPolicy,
    headers: dict[str, str],
    sleeper: Callable[[float], None],
) -> None:
    retry_after = _retry_after_seconds(headers)
    if retry_after is None:
        retry_after = min(
            policy.max_backoff_seconds,
            policy.backoff_seconds * (2 ** max(0, attempt - 1)),
        )
    if retry_after > 0:
        sleeper(retry_after)


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0.0, retry_at - time.time())


def _response_headers(response: Any) -> dict[str, str]:
    headers = getattr(response, "headers", None)
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))
