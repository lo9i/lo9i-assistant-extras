"""The bot's python-telegram-bot application, polling Telegram for updates while it runs."""

import contextlib
from collections.abc import AsyncIterator

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, Updater, filters

from lo9i_telegram.handlers import Handlers

_MESSAGES = filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO


def build_application(token: str, handlers: Handlers) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("new", handlers.new))
    app.add_handler(CommandHandler("home", handlers.home))
    app.add_handler(CallbackQueryHandler(handlers.button))
    app.add_handler(MessageHandler(_MESSAGES, handlers.message))
    app.add_error_handler(handlers.failed)
    return app


@contextlib.asynccontextmanager
async def polling(app: Application) -> AsyncIterator[None]:
    """Polls for updates inside the block. `app` must be initialized (which checks the token)."""
    await app.start()
    try:
        await _updater(app).start_polling(allowed_updates=Update.ALL_TYPES)
        yield
    finally:
        with contextlib.suppress(TelegramError):
            if app.updater is not None and app.updater.running:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()


def _updater(app: Application) -> Updater:
    """Applications from `build_application` always poll, so they have an updater."""
    if app.updater is None:
        raise RuntimeError("The Telegram application was built without an updater.")
    return app.updater
