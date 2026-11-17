import asyncio
import logging

from .config import load_settings
from .telegram import TelegramBot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    asyncio.run(TelegramBot(settings).run())


if __name__ == "__main__":
    main()
