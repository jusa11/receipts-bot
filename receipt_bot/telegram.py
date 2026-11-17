from __future__ import annotations

import asyncio
import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import Settings
from .categories import CategorySourceError, load_static_categories
from .business_validation import apply_receipt_validation, strip_validation_metadata
from .http import HttpRequestError, request
from .ocr import OcrError, YandexVisionOcr, extract_full_text, image_mime_type
from .parser import ParserError, YandexReceiptParser
from .google_sheets import GoogleSheetsClient, GoogleSheetsError
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
        self.parser = YandexReceiptParser(settings)
        self.sheets: GoogleSheetsClient | None = None
        self.base_url = f"{TELEGRAM_API}/bot{settings.telegram_bot_token}"

    def _sheets_client(self) -> GoogleSheetsClient:
        if not self.settings.google_sheets_spreadsheet_id or not self.settings.google_service_account_file:
            raise GoogleSheetsError("Google Sheets не настроен в .env")
        if self.sheets is None:
            self.sheets = GoogleSheetsClient(
                self.settings.google_sheets_spreadsheet_id,
                self.settings.google_service_account_file,
                timeout=self.settings.http_timeout_seconds,
            )
        return self.sheets

    async def _write_approved_receipt(
        self,
        chat_id: int,
        receipt_id: str,
        record: dict[str, Any],
        approved: dict[str, Any],
    ) -> None:
        record.update({"status": "write_pending", "error": None})
        self.storage.write_json(receipt_id, "receipt.json", record)
        try:
            category_records = await asyncio.to_thread(load_static_categories, self.settings.categories_path)
            category_types = {item["name"]: item["calculation_type"] for item in category_records}
            result = await asyncio.to_thread(
                self._sheets_client().write_receipt,
                receipt_id,
                approved,
                category_types,
            )
        except Exception as exc:
            logger.exception("Google Sheets write failed; receipt_id=%s", receipt_id)
            record.update({"status": "write_failed", "error": str(exc)[:1000]})
            self.storage.write_json(receipt_id, "receipt.json", record)
            self._send(
                chat_id,
                f"Не удалось записать чек {receipt_id} в тестовую таблицу. Данные сохранены локально; "
                f"после исправления доступа или настроек повторите: /retrywrite {receipt_id}\nПричина: {exc}",
            )
            return
        record.update(
            {
                "status": "written",
                "written_at": datetime.now(timezone.utc).isoformat(),
                "sheet_rows": list(result.rows),
                "write_was_retry": result.already_written,
                "write_warnings": list(result.warnings),
                "error": None,
            }
        )
        self.storage.write_json(receipt_id, "receipt.json", record)
        row_text = ", ".join(str(row) for row in result.rows)
        note = " (повторная попытка; дубль не создавался)" if result.already_written else ""
        warning_text = "\n\nПроверьте пустые поля в таблице:\n" + "\n".join(f"• {warning}" for warning in result.warnings) if result.warnings else ""
        self._send(chat_id, f"Чек {receipt_id} записан в тестовую таблицу, лист «Продукты», строки {row_text}.{note}{warning_text}")

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

    def _send(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        chunks = [text[i : i + TELEGRAM_TEXT_LIMIT] for i in range(0, len(text), TELEGRAM_TEXT_LIMIT)] or [" "]
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if index == len(chunks) - 1 and reply_markup is not None:
                payload["reply_markup"] = reply_markup
            self._api("sendMessage", payload)

    @staticmethod
    def _callback_markup(receipt_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        for index, item in enumerate(items, start=1):
            skipped = item.get("skipped", False)
            product = str(item.get("product") or "Без названия")
            category = str(item.get("category") or "категория не выбрана")
            category_label = f"🏷 {product[:30]} → {category[:24]}"
            skip_label = f"{'↩ Вернуть' if skipped else '⏭ Пропустить'}: {product[:45]}"
            rows.append(
                [
                    {"text": category_label, "callback_data": f"r|{receipt_id}|category|{index}|0"},
                    {
                        "text": skip_label,
                        "callback_data": f"r|{receipt_id}|{'include' if skipped else 'skip'}|{index}",
                    },
                ]
            )
        rows.extend(
            [
                [{"text": "✅ Подтвердить чек", "callback_data": f"r|{receipt_id}|confirm"}],
                [{"text": "❌ Отменить чек", "callback_data": f"r|{receipt_id}|cancel"}],
            ]
        )
        return {"inline_keyboard": rows}

    @staticmethod
    def _category_markup(receipt_id: str, item_index: int, categories: list[str], page: int) -> dict[str, Any]:
        page_size = 8
        start = page * page_size
        page_categories = categories[start : start + page_size]
        rows = [
            [{"text": name[:60], "callback_data": f"r|{receipt_id}|setcat|{item_index}|{start + offset}"}]
            for offset, name in enumerate(page_categories)
        ]
        navigation = []
        if page > 0:
            navigation.append({"text": "⬅️", "callback_data": f"r|{receipt_id}|category|{item_index}|{page - 1}"})
        if start + page_size < len(categories):
            navigation.append({"text": "➡️", "callback_data": f"r|{receipt_id}|category|{item_index}|{page + 1}"})
        if navigation:
            rows.append(navigation)
        rows.append([{"text": "↩️ К чеку", "callback_data": f"r|{receipt_id}|show"}])
        return {"inline_keyboard": rows}

    def _show_review(self, chat_id: int, receipt_id: str, parsed: dict[str, Any]) -> None:
        self._send(chat_id, self._format_preview(parsed), self._callback_markup(receipt_id, parsed["items"]))

    def _edit_markup(self, chat_id: int, message_id: int) -> None:
        self._api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}})

    async def _process_callback(self, query: dict[str, Any]) -> None:
        query_id = query.get("id")
        sender = query.get("from", {})
        message = query.get("message", {})
        chat = message.get("chat", {})
        user_id, chat_id = sender.get("id"), chat.get("id")
        data = query.get("data", "")
        if not isinstance(user_id, int) or user_id not in self.settings.allowed_telegram_user_ids:
            if query_id:
                self._api("answerCallbackQuery", {"callback_query_id": query_id, "text": "Нет доступа"})
            return
        parts = data.split("|")
        if len(parts) < 3 or parts[0] != "r" or not isinstance(chat_id, int):
            if query_id:
                self._api("answerCallbackQuery", {"callback_query_id": query_id, "text": "Некорректное действие"})
            return
        receipt_id, action = parts[1], parts[2]
        try:
            record = self.storage.read(receipt_id)
        except (ValueError, OSError, json.JSONDecodeError):
            record = None
        if not record or record.get("telegram_chat_id") != chat_id:
            if query_id:
                self._api("answerCallbackQuery", {"callback_query_id": query_id, "text": "Чек не найден"})
            return
        if record.get("status") in {"confirmed", "cancelled", "written"}:
            if query_id:
                self._api("answerCallbackQuery", {"callback_query_id": query_id, "text": "Этот чек уже закрыт"})
            return
        if record.get("status") != "review_pending":
            if query_id:
                self._api("answerCallbackQuery", {"callback_query_id": query_id, "text": "Этот чек сейчас не ожидает проверки"})
            return
        result_path = self.storage.receipt_dir(receipt_id) / "llm-result.json"
        if not result_path.exists():
            if query_id:
                self._api("answerCallbackQuery", {"callback_query_id": query_id, "text": "Нет результата разбора"})
            return
        parsed = json.loads(result_path.read_text(encoding="utf-8"))
        if action == "confirm":
            approved = strip_validation_metadata(parsed)
            accepted = approved["items"]
            if not accepted:
                if query_id:
                    self._api("answerCallbackQuery", {"callback_query_id": query_id, "text": "Нельзя подтвердить чек без позиций"})
                return
            self.storage.write_json(receipt_id, "approved-result.json", approved)
            self._edit_markup(chat_id, message["message_id"])
            if query_id:
                self._api("answerCallbackQuery", {"callback_query_id": query_id})
                query_id = None
            record["confirmed_at"] = datetime.now(timezone.utc).isoformat()
            if self.settings.google_sheets_spreadsheet_id:
                await self._write_approved_receipt(chat_id, receipt_id, record, approved)
            else:
                record["status"] = "confirmed"
                self.storage.write_json(receipt_id, "receipt.json", record)
                self._send(chat_id, f"Чек {receipt_id} подтверждён. Принято позиций: {len(accepted)}. Данные сохранены локально; Google Sheets не настроен.")
        elif action == "cancel":
            record.update({"status": "cancelled", "cancelled_at": datetime.now(timezone.utc).isoformat()})
            self.storage.write_json(receipt_id, "receipt.json", record)
            self._edit_markup(chat_id, message["message_id"])
            self._send(chat_id, f"Обработка чека {receipt_id} отменена. В Google Sheets ничего не записано.")
        elif action in {"skip", "include"} and len(parts) == 4:
            index = int(parts[3]) - 1
            if not 0 <= index < len(parsed["items"]):
                raise ValueError("Неверный номер позиции")
            parsed["items"][index]["skipped"] = action == "skip"
            category_records = await asyncio.to_thread(load_static_categories, self.settings.categories_path)
            apply_receipt_validation(parsed, {item["name"]: item["calculation_type"] for item in category_records})
            self.storage.write_json(receipt_id, "llm-result.json", parsed)
            self._show_review(chat_id, receipt_id, parsed)
        elif action == "category" and len(parts) == 5:
            index, page = int(parts[3]), int(parts[4])
            categories = [item["name"] for item in await asyncio.to_thread(load_static_categories, self.settings.categories_path)]
            if not 1 <= index <= len(parsed["items"]):
                raise ValueError("Неверная позиция")
            item = parsed["items"][index - 1]
            self._send(
                chat_id,
                f"Выберите категорию для «{item['product']}» (сейчас: {item['category'] or 'не выбрана'}):",
                self._category_markup(receipt_id, index, categories, page),
            )
        elif action == "setcat" and len(parts) == 5:
            index, category_index = int(parts[3]) - 1, int(parts[4])
            categories = [item["name"] for item in await asyncio.to_thread(load_static_categories, self.settings.categories_path)]
            if not 0 <= index < len(parsed["items"]) or not 0 <= category_index < len(categories):
                raise ValueError("Неверная позиция или категория")
            parsed["items"][index]["category"] = categories[category_index]
            self._mark_item_review(parsed["items"][index])
            apply_receipt_validation(parsed, {item["name"]: item["calculation_type"] for item in await asyncio.to_thread(load_static_categories, self.settings.categories_path)})
            self.storage.write_json(receipt_id, "llm-result.json", parsed)
            self._show_review(chat_id, receipt_id, parsed)
        elif action == "show":
            self._show_review(chat_id, receipt_id, parsed)
        else:
            if query_id:
                self._api("answerCallbackQuery", {"callback_query_id": query_id, "text": "Неизвестное действие"})
            return
        if query_id:
            self._api("answerCallbackQuery", {"callback_query_id": query_id})

    @staticmethod
    def _mark_item_review(item: dict[str, Any]) -> None:
        missing = any(item.get(key) is None for key in ("category", "quantity", "quantity_unit", "unit_price", "unit_price_unit", "total", "discount"))
        if missing:
            item["needs_review"] = True
            item["review_reason"] = item.get("review_reason") or "Не определены обязательные данные позиции"

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
        if text.startswith("/sheetscheck"):
            if not self.settings.google_sheets_spreadsheet_id:
                self._send(chat_id, "Google Sheets пока не настроен: заполните GOOGLE_SHEETS_SPREADSHEET_ID и GOOGLE_SERVICE_ACCOUNT_FILE в .env.")
                return
            try:
                row_count = await asyncio.to_thread(self._sheets_client().check_connection)
            except Exception as exc:
                logger.exception("Google Sheets connection check failed")
                self._send(chat_id, f"Не удалось проверить доступ к Google Sheets: {exc}")
                return
            self._send(chat_id, f"Доступ к таблице подтверждён. Лист «Продукты» и заголовки найдены; строк в листе: {row_count}. Ничего не изменено.")
            return
        if text.startswith("/review"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip().startswith(f"{chat_id}_"):
                self._send(chat_id, "Использование: /review <ID вашего чека>, например /review 12345_67")
                return
            receipt_id = parts[1].strip()
            try:
                saved_record = self.storage.read(receipt_id)
                result_path = self.storage.receipt_dir(receipt_id) / "llm-result.json"
                parsed = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
            except (ValueError, OSError, json.JSONDecodeError):
                saved_record, parsed = None, None
            if (
                not saved_record
                or saved_record.get("telegram_chat_id") != chat_id
                or saved_record.get("status") not in {"parse_complete", "review_pending"}
                or not isinstance(parsed, dict)
            ):
                self._send(chat_id, "Не нашёл открытый сохранённый разбор для этого чека.")
                return
            categories = await asyncio.to_thread(load_static_categories, self.settings.categories_path)
            apply_receipt_validation(parsed, {item["name"]: item["calculation_type"] for item in categories})
            saved_record["status"] = "review_pending"
            self.storage.write_json(receipt_id, "receipt.json", saved_record)
            self.storage.write_json(receipt_id, "llm-result.json", parsed)
            self._show_review(chat_id, receipt_id, parsed)
            return
        if text.startswith("/retrywrite"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip().startswith(f"{chat_id}_"):
                self._send(chat_id, "Использование: /retrywrite <ID вашего чека>")
                return
            receipt_id = parts[1].strip()
            try:
                record = self.storage.read(receipt_id)
                approved_path = self.storage.receipt_dir(receipt_id) / "approved-result.json"
                approved = json.loads(approved_path.read_text(encoding="utf-8")) if approved_path.is_file() else None
            except (ValueError, OSError, json.JSONDecodeError):
                record, approved = None, None
            if (
                not record
                or record.get("telegram_chat_id") != chat_id
                or record.get("status") not in {"confirmed", "write_failed", "write_pending"}
                or not isinstance(approved, dict)
            ):
                self._send(chat_id, "Не нашёл подтверждённый чек для первой записи или безопасного повтора.")
                return
            await self._write_approved_receipt(chat_id, receipt_id, record, approved)
            return
        if text.startswith("/reparse"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip().startswith(f"{chat_id}_"):
                self._send(chat_id, "Использование: /reparse <ID вашего чека>, например /reparse 12345_67")
                return
            receipt_id = parts[1].strip()
            try:
                saved_record = self.storage.read(receipt_id)
            except ValueError:
                saved_record = None
            if not saved_record or saved_record.get("telegram_chat_id") != chat_id or not saved_record.get("ocr_text"):
                self._send(chat_id, "Не нашёл сохранённый OCR-текст для этого чека.")
                return
            if saved_record.get("status") in {"confirmed", "cancelled", "write_pending", "write_failed", "written"}:
                self._send(chat_id, "Этот чек уже подтверждён, отменён или передан на запись; повторный разбор для него закрыт.")
                return
            await self._parse_saved_receipt(chat_id, receipt_id, saved_record)
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
        if existing and existing.get("status") in {"ocr_complete", "parse_complete", "review_pending", "confirmed", "cancelled", "write_pending", "write_failed", "written", "parse_failed"}:
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
            await self._parse_saved_receipt(chat_id, receipt_id, record)
        except (HttpRequestError, OcrError, OSError) as exc:
            logger.exception("Receipt OCR failed; receipt_id=%s", receipt_id)
            record.update({"status": "ocr_failed", "error": str(exc)[:1000]})
            self.storage.write_json(receipt_id, "receipt.json", record)
            self._send(chat_id, f"Не удалось распознать чек {receipt_id}: {exc}\nПерешлите фото новым сообщением для повтора.")

    async def _parse_saved_receipt(self, chat_id: int, receipt_id: str, record: dict[str, Any]) -> None:
        self._send(chat_id, f"Разбираю сохранённый OCR-текст чека {receipt_id}…")
        try:
            category_records = await asyncio.to_thread(load_static_categories, self.settings.categories_path)
            categories = [item["name"] for item in category_records]
            response_path = self.storage.receipt_dir(receipt_id) / "llm-response.json"
            parsed = await asyncio.to_thread(self.parser.parse, record["ocr_text"], categories, response_path)
            apply_receipt_validation(parsed, {item["name"]: item["calculation_type"] for item in category_records})
            self.storage.write_json(receipt_id, "llm-result.json", parsed)
            record.update({"status": "review_pending", "error": None})
            self.storage.write_json(receipt_id, "receipt.json", record)
            self._show_review(chat_id, receipt_id, parsed)
        except (CategorySourceError, ParserError, OSError) as exc:
            logger.exception("Receipt parse failed; receipt_id=%s", receipt_id)
            record.update({"status": "parse_failed", "error": str(exc)[:1000]})
            self.storage.write_json(receipt_id, "receipt.json", record)
            self._send(
                chat_id,
                f"OCR есть, но разбор моделью не завершился: {exc}\n"
                f"Если ответ модели получен, он сохранён локально: data/receipts/{receipt_id}/llm-response.json",
            )

    @staticmethod
    def _format_preview(receipt: dict[str, Any]) -> str:
        lines = ["Черновой разбор. Проверьте позиции вручную.", f"Магазин: {receipt['store'] or 'не определён'}"]
        if receipt["receipt_total"] is not None:
            lines.append(f"Итого по чеку: {receipt['receipt_total']}")
        for index, item in enumerate(receipt["items"], start=1):
            quantity = item["quantity"]
            quantity_text = "не определено" if quantity is None else f"{quantity:g} {item['quantity_unit'] or ''}".strip()
            total = "не определена" if item["total"] is None else str(item["total"])
            lines.extend(
                [
                    "",
                    f"{index}. {'⏭ ПРОПУСКАЕТСЯ — ' if item.get('skipped') else ''}{item['product']}",
                    f"   Бренд: {item['brand'] or 'не определён'}; категория: {item['category'] or 'не определена'}",
                    f"   Количество: {quantity_text}; цена: {item['unit_price']} {item['unit_price_unit'] or ''}; итого: {total}",
                    f"   Скидка: {'да' if item['discount'] is True else 'нет' if item['discount'] is False else 'не определена'}",
                ]
            )
            if item["needs_review"]:
                lines.append(f"   ⚠ Проверить: {item['review_reason'] or 'неоднозначные данные'}")
        if receipt["needs_review"]:
            lines.extend(["", f"Требуется проверка: {receipt['review_reason'] or 'есть неоднозначные данные'}"])
        if receipt.get("validation_issues"):
            lines.extend(["", "⚠ Проверка по формулам таблицы:"])
            lines.extend(f"• {issue['message']}" for issue in receipt["validation_issues"])
        lines.extend(["", "Этот результат пока не записывается в Google Sheets."])
        return "\n".join(lines)

    async def run(self) -> None:
        import json

        logger.info("Starting local Telegram long-polling bot; data_dir=%s", self.settings.data_dir)
        offset: int | None = None
        while True:
            payload: dict[str, Any] = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = await asyncio.to_thread(self._api, "getUpdates", payload)
                for update in updates or []:
                    update_id = update.get("update_id")
                    callback = update.get("callback_query")
                    try:
                        message = update.get("message")
                        if isinstance(message, dict):
                            await self._process_message(message)
                        if isinstance(callback, dict):
                            await self._process_callback(callback)
                    except Exception:
                        logger.exception("Failed to process Telegram update_id=%s", update_id)
                        if isinstance(callback, dict) and callback.get("id"):
                            try:
                                self._api(
                                    "answerCallbackQuery",
                                    {"callback_query_id": callback["id"], "text": "Не удалось выполнить действие. Проверьте бота и повторите."},
                                )
                            except Exception:
                                logger.exception("Failed to acknowledge Telegram callback update_id=%s", update_id)
                    finally:
                        if isinstance(update_id, int):
                            offset = max(offset or 0, update_id + 1)
            except (HttpRequestError, OSError):
                logger.exception("Telegram polling failed; retrying in 3 seconds")
                await asyncio.sleep(3)
