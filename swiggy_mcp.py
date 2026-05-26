"""Swiggy Instamart MCP client used by the bot.

Loads tokens written by swiggy_login.py and exposes a thin call_tool()
wrapper. Auto-refresh writes updated tokens back to the same file, so
copying .swiggy_tokens.json to the server is the only manual step after
the one-time login.

If tokens are missing or refresh fails, callers see a clear error —
re-running swiggy_login.py on the laptop is the remedy. Browser-based
OAuth cannot run from Railway, so the redirect/callback handlers here
raise loudly if invoked.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientMetadata

from swiggy_login import REDIRECT_URI, SWIGGY_MCP_URL, TOKENS_FILE, FileTokenStorage


class SwiggyAuthRequired(RuntimeError):
    """Raised when tokens are missing or unusable. Re-run swiggy_login.py."""


async def _no_redirect(_authorization_url: str) -> None:
    raise SwiggyAuthRequired(
        "Swiggy OAuth needs a browser. Run `python swiggy_login.py` on your "
        "laptop, then copy .swiggy_tokens.json to the server."
    )


async def _no_callback() -> tuple[str, str | None]:
    raise SwiggyAuthRequired("Swiggy OAuth callback cannot run server-side.")


def _build_auth() -> OAuthClientProvider:
    storage = FileTokenStorage(TOKENS_FILE)
    client_metadata = OAuthClientMetadata(
        client_name="Kya Banao",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    return OAuthClientProvider(
        server_url=SWIGGY_MCP_URL,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
    )


def _unwrap(result: Any) -> dict[str, Any]:
    """Normalize MCP responses into {success: bool, data?: dict, error?: dict}.

    Swiggy changed their response format around 2026-05-26: structured payloads
    now live in `result.structuredContent` (proper MCP structured output) and
    `content[0].text` is a human-readable summary. Old code parsed text as JSON
    which now blows up. Order of preference:
      1) isError=True -> synthesize error envelope from the text summary.
      2) structuredContent present -> wrap as {success, data} for caller compat.
      3) Legacy JSON-in-text -> parse and return as-is.
      4) Anything else -> raise."""
    blocks = getattr(result, "content", None) or []
    text = blocks[0].text if blocks and getattr(blocks[0], "type", None) == "text" else ""

    if getattr(result, "isError", False):
        message = text.split("\n", 1)[0] if text else "Unknown error"
        return {"success": False, "error": {"message": message, "raw": text[:400]}}

    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        return {"success": True, "data": sc}

    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"success": True, "data": {"text": text}}

    raise RuntimeError(f"Unexpected MCP result shape: {result}")


async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call an Instamart MCP tool and return the unwrapped Swiggy payload."""
    if not TOKENS_FILE.exists():
        raise SwiggyAuthRequired(
            f"No tokens at {TOKENS_FILE}. Run `python swiggy_login.py` first."
        )

    auth = _build_auth()
    async with streamablehttp_client(SWIGGY_MCP_URL, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments or {})
            return _unwrap(result)


async def list_tools() -> list[Any]:
    """Discover available Instamart tools (debug helper)."""
    auth = _build_auth()
    async with streamablehttp_client(SWIGGY_MCP_URL, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


if __name__ == "__main__":
    import asyncio

    async def _smoke():
        tools = await list_tools()
        print(f"{len(tools)} tools available:")
        for t in tools:
            print(f"  - {t.name}: {t.description[:80] if t.description else ''}")
        print()
        print("Calling get_addresses ...")
        payload = await call_tool("get_addresses")
        print(json.dumps(payload, indent=2))

    asyncio.run(_smoke())
