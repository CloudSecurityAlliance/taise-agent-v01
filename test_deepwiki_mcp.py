#!/usr/bin/env python3
"""
Diagnostic script for testing MCP connectivity using the official SDK.
Tests the full lifecycle: connect → initialize → discover → call tool.

Usage:
    pip install 'mcp[cli]' --break-system-packages
    python3 test_deepwiki_mcp.py [endpoint_url]

Default endpoint: https://mcp.deepwiki.com/mcp
"""

import asyncio
import json
import sys
import time


async def test_mcp_server(endpoint_url: str):
    """Test the full MCP lifecycle against a remote server."""
    print("=" * 60)
    print("TAISE-Agent: MCP Universal Connector Diagnostic")
    print("=" * 60)
    print(f"Endpoint: {endpoint_url}")

    # Check SDK is installed
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        print(f"MCP SDK imported successfully")
    except ImportError as e:
        print(f"\nERROR: MCP SDK not installed!")
        print(f"Run: pip install 'mcp[cli]' --break-system-packages")
        print(f"Import error: {e}")
        sys.exit(1)

    print()

    # Use proper async with nesting for clean resource management
    print("--- Step 1: Connect ---")
    start = time.monotonic()
    try:
        async with streamable_http_client(endpoint_url) as (read_stream, write_stream, _):
            elapsed = time.monotonic() - start
            print(f"  Connected in {elapsed:.2f}s")

            # Step 2: Initialize session
            print("\n--- Step 2: Initialize Session ---")
            async with ClientSession(read_stream, write_stream) as session:
                start = time.monotonic()
                init_result = await asyncio.wait_for(session.initialize(), timeout=30)
                elapsed = time.monotonic() - start

                # SDK uses camelCase (matching JSON-RPC wire format)
                si = init_result.serverInfo
                print(f"  Initialized in {elapsed:.2f}s")
                print(f"  Server: {si.name if si else 'unknown'} {getattr(si, 'version', '')}")
                print(f"  Protocol: {init_result.protocolVersion}")
                print(f"  Capabilities: {init_result.capabilities}")

                # Step 3: Discover tools
                print("\n--- Step 3: Discover Tools ---")
                tools = []
                try:
                    start = time.monotonic()
                    tools_result = await asyncio.wait_for(session.list_tools(), timeout=30)
                    elapsed = time.monotonic() - start
                    tools = tools_result.tools
                    print(f"  Found {len(tools)} tools in {elapsed:.2f}s:")
                    for t in tools:
                        schema = getattr(t, 'inputSchema', {}) or {}
                        params = list((schema.get('properties', {}) or {}).keys())
                        required = schema.get('required', [])
                        print(f"    - {t.name}: {getattr(t, 'description', '')[:80]}")
                        print(f"      params: {params}, required: {required}")
                except Exception as e:
                    print(f"  tools/list failed: {type(e).__name__}: {e}")

                # Step 3b: Discover resources
                print("\n--- Step 3b: Discover Resources ---")
                try:
                    resources_result = await asyncio.wait_for(session.list_resources(), timeout=15)
                    print(f"  Found {len(resources_result.resources)} resources")
                    for r in resources_result.resources[:5]:
                        print(f"    - {r.uri}: {getattr(r, 'name', '')}")
                except Exception as e:
                    print(f"  resources/list: {type(e).__name__}: {e} (may not be supported)")

                # Step 3c: Discover prompts
                print("\n--- Step 3c: Discover Prompts ---")
                try:
                    prompts_result = await asyncio.wait_for(session.list_prompts(), timeout=15)
                    print(f"  Found {len(prompts_result.prompts)} prompts")
                    for p in prompts_result.prompts[:5]:
                        print(f"    - {p.name}: {getattr(p, 'description', '')[:80]}")
                except Exception as e:
                    print(f"  prompts/list: {type(e).__name__}: {e} (may not be supported)")

                # Step 4: Call a tool
                if tools:
                    print("\n--- Step 4: Call Tool ---")
                    # Pick the best tool for a question
                    target_tool = tools[0]
                    for t in tools:
                        if any(kw in t.name.lower() for kw in ["ask", "question", "query", "search"]):
                            target_tool = t
                            break

                    # Build arguments from the tool's input schema
                    schema = getattr(target_tool, 'inputSchema', {}) or {}
                    properties = schema.get('properties', {}) or {}
                    required = schema.get('required', [])

                    # Find the prompt parameter
                    prompt_param = None
                    for kw in ["question", "query", "prompt", "message", "text"]:
                        for pname in properties:
                            if kw in pname.lower():
                                prompt_param = pname
                                break
                        if prompt_param:
                            break
                    if not prompt_param and required:
                        prompt_param = required[0]
                    if not prompt_param and properties:
                        prompt_param = list(properties.keys())[0]

                    arguments = {prompt_param: "What is this project about?"}

                    # Add ALL other required params with sensible defaults
                    for pname in required:
                        if pname not in arguments:
                            prop = properties.get(pname, {})
                            if "repo" in pname.lower() or "name" in pname.lower():
                                arguments[pname] = "modelcontextprotocol/servers"
                            elif prop.get("type") == "string":
                                arguments[pname] = "test"
                            else:
                                arguments[pname] = "test"

                    print(f"  Tool: {target_tool.name}")
                    print(f"  Arguments: {json.dumps(arguments)}")

                    try:
                        start = time.monotonic()
                        result = await asyncio.wait_for(
                            session.call_tool(target_tool.name, arguments=arguments),
                            timeout=60,
                        )
                        elapsed = time.monotonic() - start

                        # Extract text
                        text_parts = []
                        if hasattr(result, 'content'):
                            for block in result.content:
                                if hasattr(block, 'text'):
                                    text_parts.append(block.text)
                        text = "\n".join(text_parts)

                        print(f"  Response in {elapsed:.2f}s ({len(text)} chars)")
                        print(f"  Preview: {text[:300]}...")
                        print(f"\n  SUCCESS!")
                    except asyncio.TimeoutError:
                        print(f"  TIMEOUT after 60s")
                    except Exception as e:
                        print(f"  FAILED: {type(e).__name__}: {e}")
                        import traceback; traceback.print_exc()
                else:
                    print("\n--- Step 4: Skipped (no tools found) ---")

    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

    print("\n" + "=" * 60)
    print("Diagnostic complete.")
    print("=" * 60)


def main():
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "https://mcp.deepwiki.com/mcp"
    asyncio.run(test_mcp_server(endpoint))


if __name__ == "__main__":
    main()
