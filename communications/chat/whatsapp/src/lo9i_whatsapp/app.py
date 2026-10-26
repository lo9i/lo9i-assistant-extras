"""The WhatsApp plugin's process. lo9i starts it (server.yaml) with the data folder, the number the user
entered (if any) and its channel credentials:

    WHATSAPP_DATA=... WHATSAPP_PHONE=... LO9I_URL=... LO9I_CHANNEL=... LO9I_CHANNEL_TOKEN=... lo9i-whatsapp

It runs the WhatsApp connection (whatsapp.py), lo9i's stream (runner.py) and the socket the plugin's tools write to
others through (tools.py) side by side, until lo9i stops it; when one ends with an error the process ends, and
lo9i starts it again later.
"""

import asyncio
import logging
import os
import signal
from collections.abc import Mapping
from pathlib import Path

from lo9i_whatsapp.channel import WhatsAppChannel
from lo9i_whatsapp.choices import Choices
from lo9i_whatsapp.daemon import Daemon
from lo9i_whatsapp.handlers import Handlers
from lo9i_whatsapp.link import Link
from lo9i_whatsapp.others import Others
from lo9i_whatsapp.port import LIMITS, SelfChat
from lo9i_whatsapp.runner import Runner
from lo9i_whatsapp.tools import serve
from lo9i_whatsapp.whatsapp import WhatsApp

logger = logging.getLogger(__name__)


def main() -> None:
    """Ends without waiting for neonize's threads: one showing QR codes only returns when they run out, minutes
    later, and an exit that waits for it would keep lo9i from stopping or restarting the channel."""
    # neonize sets up logging when it's imported; this process's format replaces it.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    loop, status = asyncio.new_event_loop(), 0
    try:
        loop.run_until_complete(run(os.environ))
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    except Exception:
        logger.exception("The WhatsApp channel failed")
        status = 1
    finally:
        logging.shutdown()
        os._exit(status)


async def run(environ: Mapping[str, str]) -> None:
    # lo9i stops the process with SIGTERM: the connection closes before it ends.
    current = asyncio.current_task()
    if current is not None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
    folder = Path(environ["WHATSAPP_DATA"])
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    phone = environ.get("WHATSAPP_PHONE", "").strip()
    daemon, link, choices = Daemon.from_env(environ), Link(), Choices()
    whatsapp = WhatsApp(folder, link, phone)
    chat = SelfChat(whatsapp, choices)
    whatsapp.on_message = Handlers(daemon, chat, choices).message
    channel = WhatsAppChannel(daemon, link, whatsapp, chat, phone_given=bool(phone))
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(whatsapp.run())
            tasks.create_task(Runner(daemon, channel, LIMITS).run())
            tasks.create_task(serve(folder, Others(whatsapp)))
    finally:
        await daemon.aclose()


if __name__ == "__main__":
    main()
