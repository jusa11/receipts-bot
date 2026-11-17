from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class HttpRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Any

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpRequestError("Сервис вернул некорректный JSON") from exc


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 45,
    retries: int = 3,
) -> HttpResponse:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return HttpResponse(response.status, response.read(), response.headers)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                detail = payload.decode("utf-8", errors="replace")[:1000]
                raise HttpRequestError(f"HTTP {exc.code}: {detail}") from exc
            last_error = exc
            retry_after = exc.headers.get("Retry-After", "")
            try:
                delay = min(max(float(retry_after), 0.0), 10.0) if retry_after else 2**attempt
            except ValueError:
                delay = 2**attempt
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == retries:
                raise HttpRequestError(f"Не удалось подключиться к внешнему сервису: {exc}") from exc
            last_error = exc
            delay = 2**attempt
        time.sleep(delay)
    raise HttpRequestError(f"Запрос завершился ошибкой: {last_error}")
