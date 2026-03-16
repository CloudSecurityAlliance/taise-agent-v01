"""
TAISE-Agent v0.2 - OpenClaw Adapter

Communicates with AI agents running on OpenClaw by sending messages
through the OpenClaw Gateway's webhook or OpenResponses API.

This adapter supports two modes:
1. Webhook mode (default): POST /hooks/agent - best for same-machine testing
2. OpenResponses mode: POST /v1/responses - OpenAI-compatible API

Since TAISE typically runs on the same machine as the OpenClaw agent,
this uses localhost by default (http://127.0.0.1:18789).

Configuration required on the OpenClaw side:
- Webhooks must be enabled in openclaw.json
- A hook token must be configured for authentication
"""

import asyncio
import json
import os
import time
from typing import Optional

import httpx

from .adapters import AgentResponse


DEFAULT_GATEWAY_URL = "http://127.0.0.1:18789"


class OpenClawAdapter:
    """Adapter for testing OpenClaw agents via the Gateway API.

    Sends scenario prompts to an OpenClaw agent through the local
    Gateway webhook endpoint and captures responses.
    """

    def __init__(self, config: Optional[dict] = None):
        oc_config = (config or {}).get("runner", {}).get("openclaw", {})
        self.gateway_url = oc_config.get("gateway_url", DEFAULT_GATEWAY_URL)
        self.hook_token = oc_config.get("hook_token") or os.environ.get("OPENCLAW_HOOK_TOKEN", "")
        self.timeout = oc_config.get("timeout_seconds", 120)
        self.mode = oc_config.get("mode", "webhook")  # "webhook" or "openresponses"

    async def _send_webhook(
        self,
        agent_name: str,
        message: str,
        client: httpx.AsyncClient,
        timeout: int,
    ) -> dict:
        """Send a message via the /hooks/agent webhook endpoint.

        Returns dict with 'text' and 'status' keys.
        """
        headers = {}
        if self.hook_token:
            headers["x-openclaw-token"] = self.hook_token

        payload = {
            "message": message,
            "name": agent_name,
            "wakeMode": "now",
        }

        resp = await client.post(
            f"{self.gateway_url}/hooks/agent",
            json=payload,
            headers=headers,
        )

        if resp.status_code == 200:
            data = resp.json()
            # Webhook may return the response directly or an acknowledgment
            if isinstance(data, dict):
                response_text = data.get("response", data.get("text", data.get("message", "")))
                if response_text:
                    return {"text": response_text, "status": "completed"}
                # If webhook returns an ack, we may need to poll
                return {"text": json.dumps(data), "status": "completed"}
            return {"text": str(data), "status": "completed"}
        else:
            error_detail = ""
            try:
                error_detail = resp.json()
            except Exception:
                error_detail = resp.text[:500]
            return {
                "text": "",
                "status": "error",
                "error": f"Gateway returned {resp.status_code}: {error_detail}",
            }

    async def _send_openresponses(
        self,
        agent_name: str,
        message: str,
        client: httpx.AsyncClient,
        timeout: int,
    ) -> dict:
        """Send a message via the /v1/responses OpenAI-compatible endpoint.

        Returns dict with 'text' and 'status' keys.
        """
        headers = {
            "Content-Type": "application/json",
        }
        if self.hook_token:
            headers["Authorization"] = f"Bearer {self.hook_token}"

        payload = {
            "model": agent_name,
            "input": message,
        }

        resp = await client.post(
            f"{self.gateway_url}/v1/responses",
            json=payload,
            headers=headers,
        )

        if resp.status_code == 200:
            data = resp.json()
            # OpenResponses format: extract output text
            output = data.get("output", [])
            if isinstance(output, list):
                texts = []
                for item in output:
                    if isinstance(item, dict):
                        content = item.get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "output_text":
                                    texts.append(c.get("text", ""))
                        elif isinstance(content, str):
                            texts.append(content)
                    elif isinstance(item, str):
                        texts.append(item)
                response_text = "\n".join(texts).strip()
            elif isinstance(output, str):
                response_text = output
            else:
                response_text = json.dumps(data)

            if response_text:
                return {"text": response_text, "status": "completed"}
            return {"text": json.dumps(data), "status": "completed"}
        else:
            error_detail = ""
            try:
                error_detail = resp.json()
            except Exception:
                error_detail = resp.text[:500]
            return {
                "text": "",
                "status": "error",
                "error": f"Gateway returned {resp.status_code}: {error_detail}",
            }

    async def send(
        self,
        endpoint_url: str,
        message: str,
        auth_method: str = "openclaw_token",
        auth_token: str = "",
        timeout_seconds: int = 120,
        **kwargs,
    ) -> AgentResponse:
        """Send a scenario prompt to an OpenClaw agent and capture its response.

        Args:
            endpoint_url: The agent name or gateway URL
                - If it starts with http, treated as the full gateway URL
                - Otherwise, treated as the agent name (gateway URL from config)
            message: The scenario prompt text
            auth_method: Authentication method
            auth_token: Hook token or bearer token (overrides config)
            timeout_seconds: Max wait time for response

        Returns:
            AgentResponse with the agent's reply
        """
        # Determine gateway URL and agent name
        if endpoint_url.startswith("http"):
            gateway = endpoint_url.rstrip("/")
            agent_name = kwargs.get("agent_name", "default")
        else:
            gateway = self.gateway_url
            agent_name = endpoint_url

        # Override gateway URL if provided
        original_gateway = self.gateway_url
        self.gateway_url = gateway

        # Use provided token or fall back to configured one
        original_token = self.hook_token
        if auth_token:
            self.hook_token = auth_token

        start_time = time.monotonic()

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds + 10)
            ) as client:
                # First, check if the gateway is reachable
                try:
                    health = await client.get(
                        f"{self.gateway_url}/health",
                        timeout=5.0,
                    )
                    if health.status_code != 200:
                        print(f"  [openclaw] Gateway health check returned {health.status_code}")
                except Exception as e:
                    return AgentResponse(
                        text="",
                        elapsed_ms=int((time.monotonic() - start_time) * 1000),
                        status="connection_error",
                        raw_response=None,
                        error_message=f"Cannot reach OpenClaw Gateway at {self.gateway_url}. "
                        f"Ensure the gateway is running: {str(e)}",
                    )

                print(f"  [openclaw] Sending prompt to agent '{agent_name}' via {self.mode} mode...")

                # Send via the configured mode
                if self.mode == "openresponses":
                    result = await self._send_openresponses(
                        agent_name, message, client, timeout_seconds
                    )
                else:
                    result = await self._send_webhook(
                        agent_name, message, client, timeout_seconds
                    )

                elapsed = int((time.monotonic() - start_time) * 1000)

                if result["status"] == "error":
                    return AgentResponse(
                        text="",
                        elapsed_ms=elapsed,
                        status="connection_error",
                        raw_response=result,
                        error_message=result.get("error", "Unknown error from OpenClaw Gateway"),
                    )

                return AgentResponse(
                    text=result["text"],
                    elapsed_ms=elapsed,
                    status="completed",
                    raw_response=result,
                    error_message=None,
                )

        except httpx.TimeoutException:
            elapsed = int((time.monotonic() - start_time) * 1000)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed,
                status="timeout",
                raw_response=None,
                error_message=f"Agent '{agent_name}' did not respond within {timeout_seconds}s.",
            )
        except Exception as e:
            elapsed = int((time.monotonic() - start_time) * 1000)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed,
                status="connection_error",
                raw_response=None,
                error_message=f"OpenClaw error: {str(e)}",
            )
        finally:
            self.gateway_url = original_gateway
            self.hook_token = original_token
