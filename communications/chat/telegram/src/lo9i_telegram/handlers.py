"""Telegram update handlers: messages, /new, /start and the buttons of approvals, questions and
scheduled jobs' requests."""

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from telegram import CallbackQuery, Message, Update
from telegram.ext import ContextTypes

from lo9i_chat.approvals import parse_decision, parse_job_decision
from lo9i_chat.attachments import Attachment
from lo9i_chat.client import SpeechError
from lo9i_chat.errors import DaemonError
from lo9i_chat.events import ApprovalRequired, Event, is_question
from lo9i_chat.questions import Press, answered_text, choices, parse_press, question_buttons
from lo9i_chat.turn import ChatTurn
from lo9i_telegram.auth import is_allowed, try_pairing
from lo9i_telegram.media import UnsupportedMediaError, attachments_from
from lo9i_telegram.pairing import Pairing
from lo9i_telegram.port import LIMITS, TelegramChat, markup

logger = logging.getLogger(__name__)

_GREETING = "Hi! Send me a message, a voice note, a photo or a document. /new starts a new conversation."
_PLACE = "Telegram"


class Conversation(Protocol):
    """The ChannelClient calls the handlers need."""

    def message(self, text: str, files: Sequence[Attachment] = ()) -> AsyncIterator[Event]: ...
    def answer(self, decisions: dict[str, Any]) -> AsyncIterator[Event]: ...
    async def new_conversation(self) -> str: ...
    async def pending_approvals(self) -> list[ApprovalRequired]: ...
    async def answer_job(self, interrupt_id: str, decision: dict[str, Any]) -> bool: ...
    async def speech(self, text: str, after_voice_message: bool) -> bytes | None: ...


class Handlers:
    def __init__(self, client: Conversation, pairing: Pairing) -> None:
        self._client = client
        self._pairing = pairing

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is not None and is_allowed(self._pairing, update):
            await message.reply_text(_GREETING)

    async def new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is not None and is_allowed(self._pairing, update):
            await self._client.new_conversation()
            await message.reply_text("Started a new conversation.")

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or await try_pairing(self._pairing, update) or not is_allowed(self._pairing, update):
            return
        try:
            attachments = await attachments_from(message)
        except UnsupportedMediaError as e:
            await message.reply_text(str(e))
            return
        chat = self._chat(update, context)
        turn = ChatTurn(chat, LIMITS)
        await turn.render(self._client.message(message.text or message.caption or "", attachments))
        await self._speak(chat, turn.final_text, after_voice_message=bool(message.voice or message.audio))

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not is_allowed(self._pairing, update):
            return
        data = query.data or ""
        if job_decision := parse_job_decision(data, _PLACE):
            await self._answer_job(query, *job_decision)
        elif press := parse_press(data):
            await self._answer_question(query, press, self._chat(update, context))
        elif decision := parse_decision(data, _PLACE):
            await self._answer_approval(query, *decision, self._chat(update, context))

    async def failed(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Python-telegram-bot's error handler: a request the daemon refused or couldn't take is said
        in the chat; anything else is a bug, logged."""
        error = context.error
        message = update.effective_message if isinstance(update, Update) else None
        if isinstance(error, DaemonError) and message is not None:
            await message.reply_text(f"⚠️ {error}")
            return
        logger.error("Telegram update failed", exc_info=error)

    async def _answer_approval(
        self, query: CallbackQuery, interrupt_id: str, answer: dict[str, Any], chat: TelegramChat
    ) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        await chat.send("Approved." if answer["approved"] else "Rejected.")
        await ChatTurn(chat, LIMITS).render(self._client.answer({interrupt_id: answer}))

    async def _answer_question(self, query: CallbackQuery, press: Press, chat: TelegramChat) -> None:
        """A toggle only redraws the buttons; a pick or Done answers and the run goes on."""
        request = await self._waiting_question(press.interrupt_id)
        if request is None:
            await query.edit_message_reply_markup(reply_markup=None)
            await chat.send("This question isn't waiting for an answer any more.")
            return
        if not press.answers:
            buttons = question_buttons(press.interrupt_id, request, press.picked)
            await query.edit_message_reply_markup(reply_markup=markup(buttons))
            return
        picked = choices(request, press.picked)
        await query.edit_message_reply_markup(reply_markup=None)
        await chat.send(answered_text(picked))
        await ChatTurn(chat, LIMITS).render(self._client.answer({press.interrupt_id: {"choices": picked}}))

    async def _waiting_question(self, interrupt_id: str) -> dict[str, Any] | None:
        waiting = await self._client.pending_approvals()
        return next((a.request for a in waiting if a.interrupt_id == interrupt_id and is_question(a.request)), None)

    async def _answer_job(self, query: CallbackQuery, interrupt_id: str, answer: dict[str, Any]) -> None:
        """A scheduled job's request: the job's run continues on its own, nothing to show here."""
        await query.edit_message_reply_markup(reply_markup=None)
        if not await self._client.answer_job(interrupt_id, answer):
            reply = "This request isn't waiting any more: the job's run already went on without it."
        else:
            reply = "Approved. The job goes on." if answer["approved"] else "Rejected. The job goes on without it."
        if isinstance(query.message, Message):
            await query.message.reply_text(reply)

    async def _speak(self, chat: TelegramChat, reply: str, after_voice_message: bool) -> None:
        """Also sends the reply as a voice note when Settings → Audio says so."""
        if not reply:
            return
        try:
            voice = await self._client.speech(reply, after_voice_message)
        except SpeechError as e:
            await chat.send(f"⚠️ {e}")
            return
        if voice is not None:
            await chat.send_voice(voice)

    @staticmethod
    def _chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> TelegramChat:
        chat = update.effective_chat
        if chat is None:
            raise ValueError("Telegram update without a chat.")
        return TelegramChat(context.bot, chat.id)
