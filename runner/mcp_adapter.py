"""
TAISE-Agent v0.2 - MCP Adapter (Universal Connector)

Uses the official MCP Python SDK (mcp v1.26+) to connect to any MCP server.
Follows the standard MCP lifecycle:
  1. Connect (streamable HTTP or stdio transport)
  2. Initialize (protocol handshake, capability negotiation)
  3. Discover (tools/list, resources/list, prompts/list)
  4. Test (route TAISE prompts through the best available channel)

Written against the actual SDK source:
  - mcp.client.streamable_http.streamable_http_client
    yields (read_stream, write_stream, get_session_id_callback) — 3-tuple
    accepts (url, *, http_client, terminate_on_close)
    If http_client is provided, the caller owns its lifecycle.
    If http_client is None, the SDK creates one via create_mcp_http_client().

  - mcp.client.stdio.stdio_client
    yields (read_stream, write_stream) — 2-tuple

  - mcp.ClientSession(read_stream, write_stream)
    .initialize() -> InitializeResult (serverInfo, protocolVersion, capabilities)
    .list_tools() -> ListToolsResult (.tools[].name, .description, .inputSchema)
    .list_resources() -> ListResourcesResult (.resources[].uri, .name, .description)
    .list_prompts() -> ListPromptsResult (.prompts[].name, .description, .arguments)
    .call_tool(name, arguments) -> CallToolResult (.content[] with .text blocks)
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from .adapters import AgentResponse


@dataclass
class MCPServerCapabilities:
    """Discovered capabilities of an MCP server."""
    tools: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)
    server_name: str = ""
    server_version: str = ""
    protocol_version: str = ""


class MCPAdapter:
    """Universal adapter for communicating with any MCP server.

    Uses the official MCP Python SDK (mcp package) for spec-compliant
    communication. Supports streamable HTTP and stdio transports.

    The adapter auto-discovers what the server offers (tools, resources,
    prompts) and routes TAISE test prompts through the best channel.
    """

    def __init__(self, config: Optional[dict] = None):
        mcp_config = (config or {}).get("runner", {}).get("mcp", {})
        self.default_timeout = mcp_config.get("timeout_seconds", 60)

    def _parse_endpoint(self, endpoint_url: str) -> tuple[str, str]:
        """Determine transport type from endpoint URL."""
        if endpoint_url.startswith("stdio://"):
            return "stdio", endpoint_url[len("stdio://"):]
        elif endpoint_url.startswith(("http://", "https://")):
            return "http", endpoint_url
        else:
            return "stdio", endpoint_url

    @staticmethod
    def _make_http_client(
        endpoint_url: str,
        auth_token: str = "",
        timeout_seconds: int = 60,
    ) -> "httpx.AsyncClient":
        """Create an httpx AsyncClient for MCP HTTP transport.

        Bypasses proxy for localhost (matches adapters._make_client behaviour)
        and sets auth headers when a token is provided.
        """
        import httpx

        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        parsed = urlparse(endpoint_url)
        host = parsed.hostname or ""

        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": httpx.Timeout(timeout_seconds),
            "follow_redirects": True,
        }
        if host in ("127.0.0.1", "localhost", "::1"):
            kwargs["proxy"] = None
            kwargs["transport"] = httpx.AsyncHTTPTransport(proxy=None)

        return httpx.AsyncClient(**kwargs)

    @asynccontextmanager
    async def _open_transport(
        self,
        endpoint_url: str,
        auth_token: str,
        timeout_seconds: int,
    ):
        """Open an MCP transport, yielding (read_stream, write_stream).

        For HTTP transport:
          streamable_http_client(url, http_client=...) yields a 3-tuple:
            (read_stream, write_stream, get_session_id_callback)
          When we supply http_client, the SDK does NOT close it — we must.

        For stdio transport:
          stdio_client(params) yields a 2-tuple:
            (read_stream, write_stream)
        """
        transport_type, target = self._parse_endpoint(endpoint_url)

        if transport_type == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            parts = target.split()
            server_params = StdioServerParameters(
                command=parts[0],
                args=parts[1:] if len(parts) > 1 else [],
            )
            async with stdio_client(server_params) as (read_stream, write_stream):
                yield read_stream, write_stream
        else:
            from mcp.client.streamable_http import streamable_http_client

            http_client = self._make_http_client(target, auth_token, timeout_seconds)
            # We own the httpx client lifecycle since we created it.
            # streamable_http_client only manages clients it creates itself.
            async with http_client:
                async with streamable_http_client(
                    target, http_client=http_client
                ) as (read_stream, write_stream, _get_session_id):
                    yield read_stream, write_stream

    async def _discover_capabilities(self, session) -> MCPServerCapabilities:
        """Discover what the server offers: tools, resources, prompts."""
        caps = MCPServerCapabilities()

        # Discover tools
        try:
            tools_result = await asyncio.wait_for(session.list_tools(), timeout=30)
            caps.tools = [
                {
                    "name": t.name,
                    "description": getattr(t, "description", "") or "",
                    "input_schema": getattr(t, "inputSchema", {}) or {},
                }
                for t in tools_result.tools
            ]
            logger.info(f"MCP: Discovered {len(caps.tools)} tools: {[t['name'] for t in caps.tools]}")
        except Exception as e:
            logger.warning(f"MCP: tools/list failed: {e}")

        # Discover resources (optional — many servers don't have these)
        try:
            resources_result = await asyncio.wait_for(session.list_resources(), timeout=15)
            caps.resources = [
                {
                    "uri": str(r.uri),
                    "name": getattr(r, "name", "") or "",
                    "description": getattr(r, "description", "") or "",
                }
                for r in resources_result.resources
            ]
            logger.info(f"MCP: Discovered {len(caps.resources)} resources")
        except Exception as e:
            logger.debug(f"MCP: resources/list not supported: {e}")

        # Discover prompts (optional)
        try:
            prompts_result = await asyncio.wait_for(session.list_prompts(), timeout=15)
            caps.prompts = [
                {
                    "name": p.name,
                    "description": getattr(p, "description", "") or "",
                    "arguments": [
                        {"name": a.name, "required": getattr(a, "required", False)}
                        for a in (getattr(p, "arguments", []) or [])
                    ],
                }
                for p in prompts_result.prompts
            ]
            logger.info(f"MCP: Discovered {len(caps.prompts)} prompts")
        except Exception as e:
            logger.debug(f"MCP: prompts/list not supported: {e}")

        return caps

    def _select_best_tool(
        self,
        caps: MCPServerCapabilities,
        preferred_tool: str = "",
    ) -> Optional[dict]:
        """Select the best tool to route test prompts through.

        Priority:
        1. User-specified tool name (if it exists)
        2. Tool whose name/description suggests it accepts questions/queries
        3. First available tool
        """
        if not caps.tools:
            return None

        if preferred_tool:
            for tool in caps.tools:
                if tool["name"] == preferred_tool:
                    return tool
            logger.warning(
                f"MCP: Preferred tool '{preferred_tool}' not found in: "
                f"{[t['name'] for t in caps.tools]}"
            )

        # Look for question/query-oriented tools
        query_keywords = ["ask", "question", "query", "search", "chat", "message", "prompt"]
        for tool in caps.tools:
            name_lower = tool["name"].lower()
            desc_lower = tool.get("description", "").lower()
            for kw in query_keywords:
                if kw in name_lower or kw in desc_lower:
                    return tool

        return caps.tools[0]

    def _find_prompt_param(self, tool: dict) -> str:
        """Identify which tool parameter should receive the test prompt.

        Examines the tool's input schema to find the parameter most
        likely to accept a natural language query.
        """
        schema = tool.get("input_schema", {})
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        prompt_keywords = ["question", "query", "prompt", "message", "text", "input", "content"]

        # Check required params first
        for kw in prompt_keywords:
            for param_name in required:
                if kw in param_name.lower():
                    return param_name

        # Then all params
        for kw in prompt_keywords:
            for param_name in properties:
                if kw in param_name.lower():
                    return param_name

        # Fall back to first string-type required param
        for param_name in required:
            prop = properties.get(param_name, {})
            if prop.get("type") == "string":
                return param_name

        # Fall back to first string param
        for param_name, prop in properties.items():
            if prop.get("type") == "string":
                return param_name

        # Last resort
        if properties:
            return list(properties.keys())[0]
        if required:
            return required[0]
        return "question"

    def _extract_text_from_result(self, result) -> str:
        """Extract text from an MCP CallToolResult."""
        if hasattr(result, "content"):
            content = result.content
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                return "\n".join(text_parts)
            elif isinstance(content, str):
                return content

        if isinstance(result, dict):
            content = result.get("content", "")
            if isinstance(content, list):
                return "\n".join(
                    b.get("text", str(b)) if isinstance(b, dict) else str(b)
                    for b in content
                )
            return str(content)

        return str(result)

    async def _run_with_sdk(
        self,
        endpoint_url: str,
        message: str,
        auth_token: str,
        timeout_seconds: int,
        preferred_tool: str,
        static_params: dict,
        message_param_override: Optional[str],
    ) -> AgentResponse:
        """Core method: connect via SDK, discover, and call a tool."""
        from mcp import ClientSession

        start_time = time.monotonic()

        async with self._open_transport(endpoint_url, auth_token, timeout_seconds) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                # Step 1: Initialize — protocol handshake
                logger.info(f"MCP: Initializing session with {endpoint_url}")
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=timeout_seconds,
                )

                caps = MCPServerCapabilities()
                if hasattr(init_result, "serverInfo") and init_result.serverInfo:
                    caps.server_name = getattr(init_result.serverInfo, "name", "")
                    caps.server_version = getattr(init_result.serverInfo, "version", "")
                caps.protocol_version = getattr(init_result, "protocolVersion", "")

                logger.info(
                    f"MCP: Connected to {caps.server_name} {caps.server_version} "
                    f"(protocol {caps.protocol_version})"
                )

                # Step 2: Discover capabilities
                discover_caps = await self._discover_capabilities(session)
                caps.tools = discover_caps.tools
                caps.resources = discover_caps.resources
                caps.prompts = discover_caps.prompts

                # Step 3: Select tool
                tool = self._select_best_tool(caps, preferred_tool)

                if tool is None:
                    elapsed = int((time.monotonic() - start_time) * 1000)
                    return AgentResponse(
                        text="",
                        elapsed_ms=elapsed,
                        status="connection_error",
                        raw_response={
                            "server": caps.server_name,
                            "capabilities": {
                                "tools": len(caps.tools),
                                "resources": len(caps.resources),
                                "prompts": len(caps.prompts),
                            },
                        },
                        error_message=(
                            f"MCP server '{caps.server_name}' has no tools. "
                            f"Resources: {len(caps.resources)}, Prompts: {len(caps.prompts)}"
                        ),
                    )

                # Step 4: Build arguments and call tool
                prompt_param = message_param_override or self._find_prompt_param(tool)
                arguments = dict(static_params)
                arguments[prompt_param] = message

                logger.info(
                    f"MCP: Calling '{tool['name']}' — "
                    f"prompt → '{prompt_param}', args: {json.dumps(arguments)[:200]}"
                )

                result = await asyncio.wait_for(
                    session.call_tool(tool["name"], arguments=arguments),
                    timeout=timeout_seconds,
                )

                elapsed = int((time.monotonic() - start_time) * 1000)
                response_text = self._extract_text_from_result(result)

                logger.info(
                    f"MCP: Response from '{tool['name']}': "
                    f"{len(response_text)} chars in {elapsed}ms"
                )

                return AgentResponse(
                    text=response_text,
                    elapsed_ms=elapsed,
                    status="completed",
                    raw_response={
                        "server": caps.server_name,
                        "server_version": caps.server_version,
                        "protocol_version": caps.protocol_version,
                        "tool_used": tool["name"],
                        "tool_arguments": arguments,
                        "discovered_tools": [t["name"] for t in caps.tools],
                        "discovered_resources": len(caps.resources),
                        "discovered_prompts": len(caps.prompts),
                    },
                    error_message=None,
                )

    async def send(
        self,
        endpoint_url: str,
        message: str,
        auth_method: str = "none",
        auth_token: str = "",
        timeout_seconds: int = 60,
        **kwargs,
    ) -> AgentResponse:
        """Send a test prompt to any MCP server.

        This is the main entry point. It:
        1. Connects using the official MCP SDK (handles all transport details)
        2. Initializes and discovers capabilities automatically
        3. Selects the best tool and routes the test prompt through it
        4. Returns the response

        The web form only needs the endpoint URL. Everything else is auto-discovered.

        Optional kwargs:
            mcp_tool_name: Preferred tool name (auto-detected if empty)
            mcp_tool_params: Dict of static params (e.g., {"repoName": "owner/repo"})
        """
        start_time = time.monotonic()
        preferred_tool = kwargs.get("mcp_tool_name", "")
        static_params = kwargs.get("mcp_tool_params", {})

        if isinstance(static_params, str):
            try:
                static_params = json.loads(static_params) if static_params else {}
            except json.JSONDecodeError:
                static_params = {}

        # Extract message_param override if set
        message_param_override = static_params.pop("message_param", None)

        try:
            return await asyncio.wait_for(
                self._run_with_sdk(
                    endpoint_url=endpoint_url,
                    message=message,
                    auth_token=auth_token,
                    timeout_seconds=timeout_seconds,
                    preferred_tool=preferred_tool,
                    static_params=static_params,
                    message_param_override=message_param_override,
                ),
                timeout=timeout_seconds + 10,  # outer safety timeout
            )

        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - start_time) * 1000)
            logger.error(f"MCP: TIMEOUT after {timeout_seconds}s")
            return AgentResponse(
                text="",
                elapsed_ms=elapsed,
                status="timeout",
                raw_response=None,
                error_message=f"MCP operation timed out after {timeout_seconds}s",
            )
        except ImportError as e:
            elapsed = int((time.monotonic() - start_time) * 1000)
            logger.error(f"MCP: SDK not installed: {e}")
            return AgentResponse(
                text="",
                elapsed_ms=elapsed,
                status="connection_error",
                raw_response=None,
                error_message=(
                    f"MCP Python SDK not installed. "
                    f"Run: pip install 'mcp[cli]' --break-system-packages. "
                    f"Error: {e}"
                ),
            )
        except Exception as e:
            elapsed = int((time.monotonic() - start_time) * 1000)
            logger.error(f"MCP: {type(e).__name__}: {e}", exc_info=True)
            return AgentResponse(
                text="",
                elapsed_ms=elapsed,
                status="connection_error",
                raw_response=None,
                error_message=f"MCP error: {type(e).__name__}: {str(e)}",
            )
