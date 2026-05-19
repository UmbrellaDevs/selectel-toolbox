"""
Selectel IP Hunter — entry point.
Читает BOT_TOKEN + USER_ID из .env и запускает long-polling.
"""

import asyncio
import sys

from .bot import build, stop_hunt
from .config import get_bot_token, get_user_id


async def _run() -> None:
    try:
        token   = get_bot_token()
        user_id = get_user_id()
    except ValueError as e:
        print(f"[fatal] {e}")
        sys.exit(1)

    bot, dp = build(token, user_id)
    print(f"[info] Selectel IP Hunter started. Allowed user: {user_id}")

    try:
        await dp.start_polling(
            bot, allowed_updates=["message", "callback_query"],
        )
    finally:
        await stop_hunt(bot)
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
