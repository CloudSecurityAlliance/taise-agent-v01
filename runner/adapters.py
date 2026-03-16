"""
TAISE-Agent v0.2 - Agent Communication Adapters

Supports four communication modes:
- Chat mode: Simple message/response JSON API
- API mode: OpenAI Chat Completions compatible format
- Telegram mode: Telegram Bot API for agents like OpenClaw
- MCP mode: Model Context Protocol (stdio and HTTP transports)
"""

import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx


def _make_client(endpoint_url: str, **kwargs) -> httpx.AsyncClient:
    """Create an httpx AsyncClient, bypassing proxy for localhost."""
    parsed = urlparse(endpoint_url)
    host = parsed.hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        # Bypass any SOCKS/HTTP proxy for localhost connections
        # httpx picks up ALL_PROXY/all_proxy env vars, so we must
        # explicitly set proxy=None AND use transport without proxy
        return httpx.AsyncClient(
            proxy=None,
            transport=httpx.AsyncHTTPTransport(proxy=None),
            **kwargs,
        )
    return httpx.AsyncClient(**kwargs)


@dataclass
class AgentResponse:
    """Normalized response from an agent endpoint."""
    text: str
    elapsed_ms: int
    status: str  # "completed", "timeout", "connection_error", "parse_error"
    raw_response: Optional[dict] = None
    error_message: Optional[str] = None


class ChatAdapter:
    """Adapter for simple chat-style agent endpoints.

    Sends POST with {"message": "..."} and expects {"response": "..."} or {"message": "..."}.
    """

    async def send(
        self,
        endpoint_url: str,
        message: str,
        auth_method: str = "none",
        auth_token: str = "",
        timeout_seconds: int = 30,
    ) -> AgentResponse:
        headers = {"Content-Type": "application/json"}
        if auth_method == "bearer_token" and auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        elif auth_method == "api_key" and auth_token:
            headers["X-API-Key"] = auth_token

        payload = {"message": message}

        start_time = time.time()
        try:
            async with _make_client(endpoint_url) as client:
                response = await client.post(
                    endpoint_url,
                    json=payload,
                    headers=headers,
                    timeout=timeout_seconds,
                )
                elapsed_ms = int((time.time() - start_time) * 1000)

                if response.status_code != 200:
                    return AgentResponse(
                        text="",
                        elapsed_ms=elapsed_ms,
                        status="connection_error",
                        error_message=f"HTTP {response.status_code}: {response.text[:200]}",
                    )

                data = response.json()
                # Try common response field names
                text = (
                    data.get("response")
                    or data.get("message")
                    or data.get("text")
                    or data.get("content")
                    or data.get("reply")
                    or str(data)
                )

                return AgentResponse(
                    text=text,
                    elapsed_ms=elapsed_ms,
                    status="completed",
                    raw_response=data,
                )

        except httpx.TimeoutException:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed_ms,
                status="timeout",
                error_message=f"Agent did not respond within {timeout_seconds}s",
            )
        except httpx.ConnectError as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed_ms,
                status="connection_error",
                error_message=f"Connection failed: {str(e)}",
            )
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed_ms,
                status="connection_error",
                error_message=f"Unexpected error: {str(e)}",
            )


class APIAdapter:
    """Adapter for OpenAI Chat Completions compatible endpoints.

    Sends POST with {"messages": [{"role": "user", "content": "..."}]}
    and expects {"choices": [{"message": {"content": "..."}}]}.
    """

    async def send(
        self,
        endpoint_url: str,
        message: str,
        auth_method: str = "none",
        auth_token: str = "",
        timeout_seconds: int = 30,
        model: str = "",
    ) -> AgentResponse:
        headers = {"Content-Type": "application/json"}
        if auth_method == "bearer_token" and auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        elif auth_method == "api_key" and auth_token:
            headers["X-API-Key"] = auth_token

        payload = {
            "messages": [{"role": "user", "content": message}],
        }
        if model:
            payload["model"] = model

        start_time = time.time()
        try:
            async with _make_client(endpoint_url) as client:
                response = await client.post(
                    endpoint_url,
                    json=payload,
                    headers=headers,
                    timeout=timeout_seconds,
                )
                elapsed_ms = int((time.time() - start_time) * 1000)

                if response.status_code != 200:
                    return AgentResponse(
                        text="",
                        elapsed_ms=elapsed_ms,
                        status="connection_error",
                        error_message=f"HTTP {response.status_code}: {response.text[:200]}",
                    )

                data = response.json()

                # Extract text from OpenAI-compatible format
                try:
                    text = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError):
                    # Try Anthropic format
                    try:
                        text = data["content"][0]["text"]
                    except (KeyError, IndexError):
                        text = str(data)

                return AgentResponse(
                    text=text,
                    elapsed_ms=elapsed_ms,
                    status="completed",
                    raw_response=data,
                )

        except httpx.TimeoutException:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed_ms,
                status="timeout",
                error_message=f"Agent did not respond within {timeout_seconds}s",
            )
        except httpx.ConnectError as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed_ms,
                status="connection_error",
                error_message=f"Connection failed: {str(e)}",
            )
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed_ms,
                status="connection_error",
                error_message=f"Unexpected error: {str(e)}",
            )


def get_adapter(agent_type: str, config: Optional[dict] = None):
    """Factory function to get the appropriate adapter for an agent type.

    Args:
        agent_type: One of 'chat', 'api', 'telegram', 'mcp'
        config: Optional config dict (needed for telegram and mcp adapters)
    """
    if agent_type == "chat":
        return ChatAdapter()
    elif agent_type == "api":
        return APIAdapter()
    elif agent_type == "telegram":
        from .telegram_adapter import TelegramAdapter
        return TelegramAdapter(config)
    elif agent_type == "mcp":
        from .mcp_adapter import MCPAdapter
        return MCPAdapter(config)
    elif agent_type == "openclaw":
        from .openclaw_adapter import OpenClawAdapter
        return OpenClawAdapter(config)
    else:
        raise ValueError(
            f"Unknown agent type: {agent_type}. "
            f"Must be 'chat', 'api', 'telegram', 'mcp', or 'openclaw'."
        )
