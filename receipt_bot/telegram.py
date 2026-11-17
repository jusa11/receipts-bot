from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import Settings
from .http import HttpRequestError, request
from .ocr import OcrError, YandexVisionOcr, extract_full_text, image_mime_type
from .storage import ReceiptStorage

logger = logging.getLogger(__name__)
TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_FILE_API = "https://api.telegram.org/file"
TELEGRAM_TEXT_LIMIT = 3900


class TelegramBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = ReceiptStorage(settings.data_dir)
        self.ocr = YandexVisionOcr(settings)
        self.base_url = f"{TELEGRAM_API}/bot{settings.telegram_bot_token}"

    def _api(self, method: str, payload: dict[str, Any]) -> Any:
        import json

        response = request(
            f"{self.base_url}/{method}",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload).encode("utf-8"),
            timeout=max(self.settings.http_timeout_seconds, 40),
        )
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise HttpRequestError(f"Telegram {method}: {data}")
        return data.get("result")

    def _send(self, chat_id: int, text: str) -> None:
        chunks = [text[i : i + TELEGRAM_TEXT_LIMIT] for i in range(0, len(text), TELEGRAM_TEXT_LIMIT)] or [" "]
        for chunk in chunks:
            self._api("sendMessage", {"chat_id": chat_id, "text": chunk})

    def _download_file(self, file_id: str) -> tuple[bytes, str]:
        info = self._api("getFile", {"file_id": file_id})
        file_path = info.get("file_path") if isinstance(info, dict) else None
        if not file_path:
            raise HttpRequestError("Telegram не вернул путь к файлу")
        url = f"{TELEGRAM_FILE_API}/bot{self.settings.telegram_bot_token}/{quote(file_path, safe='/')}"
        response = request(url, timeout=self.settings.http_timeout_seconds)
        return response.body, Path(file_path).name

    @staticmethod
    def _get_image(message: dict[str, Any]) -> tuple[str, str] | None:
        photos = message.get("photo") or []
        if photos:
            largest = max(photos, key=lambda photo: photo.get("file_size", 0))
            return largest["file_id"], "receipt.jpg"
        document = message.get("document")
        if document:
            filename = document.get("file_name", "")
            if image_mime_type(filename):
                return document["file_id"], filename
        return None

    async def _process_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        from_user = message.get("from", {})
        chat_id = chat.get("id")
        user_id = from_user.get("id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            return
        if user_id not in self.settings.allowed_telegram_user_ids:
            logger.warning("Ignored update from unauthorized Telegram user id=%s", user_id)
            return

        text = (message.get("text") or "").strip()
        if text.startswith("/start"):
            self._send(chat_id, f"Бот запущен. Ваш Telegram ID: {user_id}. Отправьте фото чека.")
            return
        if text.startswith("/id"):
            self._send(chat_id, f"Ваш Telegram ID: {user_id}")
            return

        image = self._get_image(message)
        if not image:
            if text:
                self._send(chat_id, "Отправьте фотографию чека или JPEG/PNG/PDF как файл.")
            return

        file_id, filename = image
        mime = image_mime_type(filename)
        if not mime:
            self._send(chat_id, "Поддерживаются JPEG, PNG и одностраничный PDF.")
            return
        receipt_id = self.storage.receipt_id(chat_id, int(message["message_id"]))
        existing = self.storage.read(receipt_id)
        if existing and existing.get("status") == "ocr_complete":
            self._send(chat_id, f"Этот чек уже распознан (ID {receipt_id}). Повторный вызов OCR не выполнялся.")
            return

        self._send(chat_id, "Фото чека получено. Выполняю OCR…")
        record: dict[str, Any] = {
            "receipt_id": receipt_id,
            "status": "processing",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "telegram_chat_id": chat_id,
            "telegram_message_id": int(message["message_id"]),
            "telegram_user_id": user_id,
            "filename": filename,
            "ocr_text": None,
            "error": None,
        }
        self.storage.write_json(receipt_id, "receipt.json", record)
        try:
            image_bytes, telegram_filename = await asyncio.to_thread(self._download_file, file_id)
            if not image_bytes:
                raise OcrError("Получен пустой файл")
            if len(image_bytes) > self.settings.max_image_bytes:
                raise OcrError(f"Файл превышает лимит {self.settings.max_image_bytes // (1024 * 1024)} МБ")
            suffix = mime[1]
            record["filename"] = telegram_filename or filename
            self.storage.write_bytes(receipt_id, f"source{suffix}", image_bytes)
            result = await asyncio.to_thread(self.ocr.recognize, image_bytes, mime[0])
            ocr_text = extract_full_text(result)
            if not ocr_text.strip():
                raise OcrError("OCR не нашёл текст на изображении")
            self.storage.write_json(receipt_id, "ocr-response.json", result)
            record.update({"status": "ocr_complete", "ocr_text": ocr_text, "error": None})
            self.storage.write_json(receipt_id, "receipt.json", record)
            self._send(chat_id, f"OCR завершён. Чек {receipt_id}. Распознанный текст:\n\n{ocr_text}")
        except (HttpRequestError, OcrError, OSError) as exc:
            logger.exception("Receipt OCR failed; receipt_id=%s", receipt_id)
            record.update({"status": "ocr_failed", "error": str(exc)[:1000]})
            self.storage.write_json(receipt_id, "receipt.json", record)
            self._send(chat_id, f"Не удалось распознать чек {receipt_id}: {exc}\nПерешлите фото новым сообщением для повтора.")

    async def run(self) -> None:
        import json

        logger.info("Starting local Telegram long-polling bot; data_dir=%s", self.settings.data_dir)
        offset: int | None = None
        while True:
            payload: dict[str, Any] = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = await asyncio.to_thread(self._api, "getUpdates", payload)
                for update in updates or []:
                    update_id = update.get("update_id")
                    try:
                        message = update.get("message")
                        if isinstance(message, dict):
                            await self._process_message(message)
                    except Exception:
                        logger.exception("Failed to process Telegram update_id=%s", update_id)
                    finally:
                        if isinstance(update_id, int):
                            offset = max(offset or 0, update_id + 1)
            except (HttpRequestError, OSError):
                logger.exception("Telegram polling failed; retrying in 3 seconds")
                await asyncio.sleep(3)
