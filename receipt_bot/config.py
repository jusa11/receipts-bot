from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value and value[0:1] in ("'", '"') and value[-1:] == value[0]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_telegram_user_ids: frozenset[int]
    yandex_api_key: str
    yandex_folder_id: str
    yandex_ocr_model: str
    yandex_ocr_languages: tuple[str, ...]
    data_dir: Path
    max_image_bytes: int
    http_timeout_seconds: int


def load_settings() -> Settings:
    _load_dotenv(Path(".env"))
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    api_key = os.getenv("YANDEX_API_KEY", "").strip()
    folder_id = os.getenv("YANDEX_FOLDER_ID", "").strip()
    raw_ids = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").strip()
    if not token:
        raise ValueError("Заполните TELEGRAM_BOT_TOKEN в .env")
    if not raw_ids:
        raise ValueError("Заполните ALLOWED_TELEGRAM_USER_IDS в .env; пустой allowlist запрещён")
    if not api_key or not folder_id:
        raise ValueError("Заполните YANDEX_API_KEY и YANDEX_FOLDER_ID в .env")
    try:
        allowed_ids = frozenset(int(item.strip()) for item in raw_ids.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("ALLOWED_TELEGRAM_USER_IDS должен содержать числовые ID через запятую") from exc
    if not allowed_ids:
        raise ValueError("В ALLOWED_TELEGRAM_USER_IDS должен быть хотя бы один ID")

    try:
        max_image_bytes = int(os.getenv("MAX_IMAGE_BYTES", "10485760"))
        timeout = int(os.getenv("HTTP_TIMEOUT_SECONDS", "45"))
    except ValueError as exc:
        raise ValueError("MAX_IMAGE_BYTES и HTTP_TIMEOUT_SECONDS должны быть целыми числами") from exc
    if max_image_bytes <= 0 or timeout <= 0:
        raise ValueError("MAX_IMAGE_BYTES и HTTP_TIMEOUT_SECONDS должны быть больше нуля")

    return Settings(
        telegram_bot_token=token,
        allowed_telegram_user_ids=allowed_ids,
        yandex_api_key=api_key,
        yandex_folder_id=folder_id,
        yandex_ocr_model=os.getenv("YANDEX_OCR_MODEL", "page").strip() or "page",
        yandex_ocr_languages=tuple(
            part.strip() for part in os.getenv("YANDEX_OCR_LANGUAGES", "ru,en").split(",") if part.strip()
        ),
        data_dir=Path(os.getenv("DATA_DIR", "./data")).resolve(),
        max_image_bytes=max_image_bytes,
        http_timeout_seconds=timeout,
    )
