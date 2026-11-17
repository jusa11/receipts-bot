from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class ReceiptStorage:
    def __init__(self, root: Path) -> None:
        self.root = root / "receipts"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def receipt_id(chat_id: int, message_id: int) -> str:
        return f"{chat_id}_{message_id}"

    def receipt_dir(self, receipt_id: str) -> Path:
        if not re.fullmatch(r"-?\d+_\d+", receipt_id):
            raise ValueError("Недопустимый ID чека")
        return self.root / receipt_id

    def read(self, receipt_id: str) -> dict[str, Any] | None:
        path = self.receipt_dir(receipt_id) / "receipt.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, receipt_id: str, filename: str, value: Any) -> None:
        folder = self.receipt_dir(receipt_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / filename
        fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def write_bytes(self, receipt_id: str, filename: str, value: bytes) -> None:
        folder = self.receipt_dir(receipt_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / filename
        fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", dir=folder)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
