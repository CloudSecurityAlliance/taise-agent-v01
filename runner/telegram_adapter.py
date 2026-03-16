"""
TAISE-Agent v0.2 - Telegram Adapter (User Account via Telethon)

Tests AI agents deployed as Telegram bots by sending messages from a
real Telegram user account. This avoids the bot-to-bot limitation
(bots cannot see messages from other bots in Telegram).

One-time setup:
1. Get API credentials from https://my.telegram.org ("API development tools")
2. Set environment variables: TELEGRAM_API_ID and TELEGRAM_API_HASH
3. Run the auth flow once: python3 -m runner.telegram_adapter
   (This creates a session file so you don't need to re-authenticate)

Then on the TAISE web form:
- Agent Type: Telegram Bot
- Telegram Chat ID: The bot's @username (e.g., @Orbert_of_CSA_bot)
"""

import asyncio
import os
import time
from typing import Optional

from .adapters import AgentResponse


# Session file location — persists auth across runs
SESSION_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".telegram_session")
SESSION_NAME = os.path.join(SESSION_DIR, "taise_tester")


class TelegramAdapter:
    """Adapter for testing Telegram bot agents using a user account.

    Uses Telethon (Telegram MTProto client) to send messages as a real
    user and capture bot responses. This works with any Telegram bot.
    """

    def __init__(self, config: Optional[dict] = None):
        tg_config = (config or {}).get("runner", {}).get("telegram", {})
        self.api_id = tg_config.get("api_id") or os.environ.get("TELEGRAM_API_ID", "")
        self.api_hash = tg_config.get("api_hash") or os.environ.get("TELEGRAM_API_HASH", "")
        self.poll_timeout = tg_config.get("poll_timeout_seconds", 120)
        self.quiet_period = tg_config.get("quiet_period_seconds", 3.0)

        # Ensure session directory exists
        os.makedirs(SESSION_DIR, exist_ok=True)

    async def send(
        self,
        endpoint_url: str,
        message: str,
        auth_method: str = "none",
        auth_token: str = "",
        timeout_seconds: int = 120,
        **kwargs,
    ) -> AgentResponse:
        """Send a message to a Telegram bot and capture its response.

        Args:
            endpoint_url: The bot's @username or numeric ID
            message: The scenario prompt text
            timeout_seconds: Max wait time for the bot's response

        Returns:
            AgentResponse with the bot's reply
        """
        if not self.api_id or not self.api_hash:
            return AgentResponse(
                text="",
                elapsed_ms=0,
                status="connection_error",
                raw_response=None,
                error_message="Telegram API credentials not configured. "
                "Set TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables. "
                "Get these from https://my.telegram.org",
            )

        try:
            from telethon import TelegramClient
            from telethon.events import NewMessage, MessageEdited
        except ImportError:
            return AgentResponse(
                text="",
                elapsed_ms=0,
                status="connection_error",
                raw_response=None,
                error_message="Telethon not installed. Run: pip install telethon",
            )

        bot_identifier = endpoint_url.strip()
        start_time = time.monotonic()

        # Multi-message accumulator: bots often split responses across
        # several Telegram messages. We collect all messages and use a
        # quiet-period debounce to detect when the bot is done.
        message_chunks = []
        first_message_event = asyncio.Event()  # signals at least one message arrived
        quiet_period = self.quiet_period  # seconds of silence before we consider response complete

        client = TelegramClient(
            SESSION_NAME,
            int(self.api_id),
            self.api_hash,
        )

        try:
            await client.connect()

            if not await client.is_user_authorized():
                return AgentResponse(
                    text="",
                    elapsed_ms=int((time.monotonic() - start_time) * 1000),
                    status="connection_error",
                    raw_response=None,
                    error_message="Telegram session not authorized. "
                    "Run the one-time auth on the droplet: "
                    "cd taise-agent-v01 && source venv/bin/activate && "
                    "python3 -m runner.telegram_adapter",
                )

            # Resolve the bot entity
            try:
                entity = await client.get_entity(bot_identifier)
            except Exception as e:
                return AgentResponse(
                    text="",
                    elapsed_ms=int((time.monotonic() - start_time) * 1000),
                    status="connection_error",
                    raw_response=None,
                    error_message=f"Could not find Telegram bot '{bot_identifier}': {str(e)}",
                )

            bot_id = entity.id

            # Track when the last message arrived/edited for quiet-period detection
            last_message_time = 0.0
            # Map message IDs to their index in message_chunks for edit tracking
            message_id_map = {}

            # Set up handlers to accumulate ALL bot messages AND edits
            @client.on(NewMessage(from_users=bot_id))
            async def on_new_message(event):
                nonlocal last_message_time
                msg_text = event.message.text or event.message.message or ""
                if msg_text:
                    message_id_map[event.message.id] = len(message_chunks)
                    message_chunks.append(msg_text)
                last_message_time = time.monotonic()
                first_message_event.set()

            @client.on(MessageEdited(from_users=bot_id))
            async def on_message_edited(event):
                nonlocal last_message_time
                msg_text = event.message.text or event.message.message or ""
                if msg_text:
                    idx = message_id_map.get(event.message.id)
                    if idx is not None:
                        # Replace the chunk with the updated (longer) text
                        message_chunks[idx] = msg_text
                    else:
                        message_id_map[event.message.id] = len(message_chunks)
                        message_chunks.append(msg_text)
                last_message_time = time.monotonic()
                first_message_event.set()

            # Send the scenario prompt, with retry on Telegram flood limits
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    print(f"  [telegram] Sending prompt to {bot_identifier}...")
                    await client.send_message(entity, message)
                    break
                except Exception as send_err:
                    # Handle Telegram FloodWaitError (rate limiting)
                    err_str = str(send_err)
                    if "wait" in err_str.lower() and "seconds" in err_str.lower():
                        import re
                        wait_match = re.search(r'(\d+)\s*seconds', err_str)
                        wait_secs = int(wait_match.group(1)) if wait_match else 30
                        wait_secs = min(wait_secs, 120)
                        print(f"  [telegram] Rate limited, waiting {wait_secs}s before retry {attempt+1}/{max_retries}...")
                        await asyncio.sleep(wait_secs)
                    else:
                        raise

            # Wait for first message to arrive
            try:
                await asyncio.wait_for(
                    first_message_event.wait(),
                    timeout=min(timeout_seconds, self.poll_timeout),
                )
            except asyncio.TimeoutError:
                elapsed = int((time.monotonic() - start_time) * 1000)
                return AgentResponse(
                    text="",
                    elapsed_ms=elapsed,
                    status="timeout",
                    raw_response=None,
                    error_message=f"Bot '{bot_identifier}' did not respond within {timeout_seconds}s.",
                )

            # Wait for the bot to stop sending (quiet period debounce)
            # Keep waiting as long as new messages arrive within quiet_period
            while True:
                await asyncio.sleep(quiet_period)
                if time.monotonic() - last_message_time >= quiet_period:
                    break

            # Combine all message chunks
            response_text = "\n\n".join(message_chunks)
            elapsed = int((time.monotonic() - start_time) * 1000)

            msg_count = len(message_chunks)
            if msg_count > 1:
                print(f"  [telegram] Captured {msg_count} message chunks ({len(response_text)} chars)")

            return AgentResponse(
                text=response_text,
                elapsed_ms=elapsed,
                status="completed",
                raw_response={"text": response_text, "bot_id": bot_id, "message_count": msg_count},
                error_message=None,
            )

        except Exception as e:
            elapsed = int((time.monotonic() - start_time) * 1000)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed,
                status="connection_error",
                raw_response=None,
                error_message=f"Telegram error: {str(e)}",
            )
        finally:
            await client.disconnect()


# ── One-time authentication flow ──
# Run this module directly to set up the Telegram session:
#   python3 -m runner.telegram_adapter

if __name__ == "__main__":
    import sys

    api_id = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")

    if not api_id or not api_hash:
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables first.")
        print("Get these from https://my.telegram.org ('API development tools')")
        sys.exit(1)

    async def auth():
        from telethon import TelegramClient

        os.makedirs(SESSION_DIR, exist_ok=True)
        client = TelegramClient(SESSION_NAME, int(api_id), api_hash)
        await client.start()
        me = await client.get_me()
        print(f"\nAuthenticated as: {me.first_name} (@{me.username})")
        print(f"Session saved to: {SESSION_NAME}.session")
        print("TAISE can now send Telegram messages as this user.")
        await client.disconnect()

    asyncio.run(auth())
