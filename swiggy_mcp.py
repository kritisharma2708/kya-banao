"""Swiggy Instamart MCP client used by the bot — multi-tenant.

Each call is scoped to a chat_id so tokens are looked up per household from
the tenant_tokens table. Backward-compat path: if a chat_id is None, fall
back to the legacy single-tenant FileTokenStorage so the older single-tenant
Railway deployment and the standalone __main__ smoke test keep working
during the multi-tenant migration.

Browser-based OAuth cannot run from Railway, so the redirect/callback
handlers raise loudly if invoked — re-auth is always done on a laptop.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientMetadata

import db
from swiggy_login import (
    REDIRECT_URI,
    SWIGGY_MCP_URL,
    TOKENS_FILE,
    DBTokenStorage,
    FileTokenStorage,
)


class SwiggyAuthRequired(RuntimeError):
    """Raised when tokens are missing or unusable for the given chat."""


async def _no_redirect(_authorization_url: str) -> None:
    raise SwiggyAuthRequired(
        "Swiggy OAuth needs a browser. Run `python swiggy_login.py` on a "
        "laptop, then paste the resulting JSON via /import_tokens."
    )


async def _no_callback() -> tuple[str, str | None]:
    raise SwiggyAuthRequired("Swiggy OAuth callback cannot run server-side.")


_CLIENT_METADATA = OAuthClientMetadata(
    client_name="Kya Banao",
    redirect_uris=[REDIRECT_URI],
    grant_types=["authorization_code", "refresh_token"],
    response_types=["code"],
    token_endpoint_auth_method="none",
)


def _build_auth(chat_id: int | None) -> OAuthClientProvider:
    """Per-chat auth provider. chat_id=None routes through the legacy
    FileTokenStorage so single-tenant fallback continues to work."""
    if chat_id is None:
        storage = FileTokenStorage(TOKENS_FILE)
    else:
        storage = DBTokenStorage(chat_id)
    return OAuthClientProvider(
        server_url=SWIGGY_MCP_URL,
        client_metadata=_CLIENT_METADATA,
        storage=storage,
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
    )


def _unwrap(result: Any) -> dict[str, Any]:
    """Normalize MCP responses into {success: bool, data?: dict, error?: dict}.

    Swiggy changed their response format around 2026-05-26: structured payloads
    now live in `result.structuredContent` and `content[0].text` is a human-
    readable summary. Order of preference:
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


def _tokens_present(chat_id: int | None) -> bool:
    if chat_id is None:
        return TOKENS_FILE.exists()
    return db.has_tenant_tokens(chat_id)


async def call_tool(
    chat_id: int | None,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call an Instamart MCP tool on behalf of one chat (household).

    Pass chat_id=None only for legacy single-tenant flows (the standalone
    __main__ smoke test, the swiggy_mcp script). Production callers always
    have a chat_id."""
    auth = _build_auth(chat_id)
    if not _tokens_present(chat_id):
        raise SwiggyAuthRequired(
            f"No Swiggy tokens for chat {chat_id}. Run `python swiggy_login.py` "
            "on a laptop and paste the resulting JSON here via /import_tokens."
        )

    async with streamablehttp_client(SWIGGY_MCP_URL, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments or {})
            return _unwrap(result)


async def list_tools(chat_id: int | None = None) -> list[Any]:
    """Discover available Instamart tools. Mostly used as a debug helper."""
    auth = _build_auth(chat_id)
    async with streamablehttp_client(SWIGGY_MCP_URL, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


if __name__ == "__main__":
    import asyncio

    async def _smoke():
        # Single-tenant path: uses local .swiggy_tokens.json
        tools = await list_tools(chat_id=None)
        print(f"{len(tools)} tools available:")
        for t in tools:
            print(f"  - {t.name}: {t.description[:80] if t.description else ''}")
        print()
        print("Calling get_addresses ...")
        payload = await call_tool(None, "get_addresses")
        print(json.dumps(payload, indent=2))

    asyncio.run(_smoke())
