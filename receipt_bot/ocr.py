from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from .config import Settings
from .http import HttpRequestError, request

OCR_URL = "https://ai.api.cloud.yandex.net/ocr/v1/recognizeText"


class OcrError(RuntimeError):
    pass


def extract_full_text(response: dict[str, Any]) -> str:
    annotation = response.get("result", {}).get("textAnnotation", {})
    full_text = annotation.get("fullText")
    if isinstance(full_text, str) and full_text.strip():
        return full_text
    lines: list[str] = []
    for block in annotation.get("blocks", []):
        for line in block.get("lines", []):
            text = line.get("text")
            if isinstance(text, str):
                lines.append(text)
    return "\n".join(lines)


class YandexVisionOcr:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def recognize(self, image: bytes, mime_type: str) -> dict[str, Any]:
        body = json.dumps(
            {
                "mimeType": mime_type,
                "languageCodes": list(self.settings.yandex_ocr_languages) or ["*"],
                "model": self.settings.yandex_ocr_model,
                "content": base64.b64encode(image).decode("ascii"),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            response = request(
                OCR_URL,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Api-Key {self.settings.yandex_api_key}",
                    "x-folder-id": self.settings.yandex_folder_id,
                    # Receipt images may contain personal information. Do not opt in to data logging.
                    "x-data-logging-enabled": "false",
                },
                body=body,
                timeout=self.settings.http_timeout_seconds,
            )
            result = response.json()
        except HttpRequestError as exc:
            raise OcrError(str(exc)) from exc
        if not isinstance(result, dict):
            raise OcrError("OCR вернул ответ неожиданного формата")
        if result.get("error"):
            error = result["error"]
            raise OcrError(str(error.get("message", "Ошибка Yandex Vision OCR")))
        if not isinstance(result.get("result"), dict):
            raise OcrError("В ответе OCR отсутствует результат распознавания")
        return result


def image_mime_type(filename: str) -> tuple[str, str] | None:
    suffix = Path(filename).suffix.lower()
    return {
        ".jpg": ("JPEG", ".jpg"),
        ".jpeg": ("JPEG", ".jpg"),
        ".png": ("PNG", ".png"),
        ".pdf": ("application/pdf", ".pdf"),
    }.get(suffix)
