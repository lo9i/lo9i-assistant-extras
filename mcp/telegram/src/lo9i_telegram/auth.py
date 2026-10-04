"""Who may use the bot: accounts paired with a one-time code (pairing.py)."""

import logging

from telegram import Update, User

from lo9i_telegram.pairing import Pairing

logger = logging.getLogger(__name__)

_PAIRED = "Paired ✓. You can talk to the assistant here now. /new starts a new conversation."


def is_allowed(pairing: Pairing, update: Update) -> bool:
    user = update.effective_user
    if user is not None and pairing.allowed(user.id):
        return True
    logger.info("Ignored Telegram update from user id %s", user and user.id)
    return False


async def try_pairing(pairing: Pairing, update: Update) -> bool:
    """Pairs the sender when the message holds the open pairing code, and confirms in the chat."""
    user, message = update.effective_user, update.effective_message
    if user is None or message is None or not message.text:
        return False
    if not await pairing.pair(message.text, user.id, _display_name(user)):
        return False
    await message.reply_text(_PAIRED)
    return True


def _display_name(user: User) -> str:
    return f"{user.full_name} (@{user.username})" if user.username else user.full_name
