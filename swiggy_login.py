"""One-time OAuth login against Swiggy Instamart MCP.

Run on Kriti's laptop:
    source venv/bin/activate
    python swiggy_login.py

Opens a browser to Swiggy's login, captures the OAuth callback on
http://127.0.0.1:8765/callback, persists tokens to .swiggy_tokens.json.
The bot loads those tokens on Railway and auto-refreshes them.
"""

import asyncio
import json
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

SWIGGY_MCP_URL = "https://mcp.swiggy.com/im"
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"
# Tokens path is env-var configurable so Railway can point at the persistent
# volume (e.g. SWIGGY_TOKENS_PATH=/data/.swiggy_tokens.json).
TOKENS_FILE = Path(os.getenv("SWIGGY_TOKENS_PATH", str(Path(__file__).parent / ".swiggy_tokens.json")))


class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens and dynamic-registration client info to one JSON file.

    Railway bootstrap: if the file doesn't exist but SWIGGY_TOKENS_JSON env var
    is set, write that JSON to disk on first read. Lets Kriti paste tokens once
    via Railway dashboard after a fresh laptop re-auth."""

    def __init__(self, path: Path):
        self.path = path
        self._bootstrap_from_env()

    def _bootstrap_from_env(self) -> None:
        if self.path.exists():
            print(f"[swiggy_login] tokens file already at {self.path}, skipping env-var seed", flush=True)
            return
        seed = os.getenv("SWIGGY_TOKENS_JSON")
        if not seed:
            print(f"[swiggy_login] no tokens at {self.path} and SWIGGY_TOKENS_JSON env var not set", flush=True)
            return
        try:
            json.loads(seed)
        except json.JSONDecodeError as e:
            print(f"[swiggy_login] SWIGGY_TOKENS_JSON is malformed ({e}), not seeding. Length={len(seed)}, first 40 chars={seed[:40]!r}", flush=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(seed)
        os.chmod(self.path, 0o600)
        print(f"[swiggy_login] seeded {self.path} from SWIGGY_TOKENS_JSON env var ({len(seed)} bytes)", flush=True)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, default=str))
        os.chmod(self.path, 0o600)

    async def get_tokens(self) -> OAuthToken | None:
        data = self._read()
        return OAuthToken(**data["tokens"]) if "tokens" in data else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read()
        return OAuthClientInformationFull(**data["client_info"]) if "client_info" in data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


class DBTokenStorage(TokenStorage):
    """Multi-tenant token storage: tokens live in the SQLite tenant_tokens
    table keyed by chat_id. Used by the running bot to serve many households
    from a single deployment."""

    def __init__(self, chat_id: int):
        # Imported here to keep the swiggy_login module usable as a standalone
        # CLI script that doesn't touch the bot's DB.
        import db  # noqa: PLC0415
        self.chat_id = chat_id
        self._db = db

    async def get_tokens(self) -> OAuthToken | None:
        tokens, _ = self._db.get_tenant_tokens(self.chat_id)
        return OAuthToken(**tokens) if tokens else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        _, client_info = self._db.get_tenant_tokens(self.chat_id)
        self._db.set_tenant_tokens(self.chat_id, tokens.model_dump(mode="json"), client_info)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        _, client_info = self._db.get_tenant_tokens(self.chat_id)
        return OAuthClientInformationFull(**client_info) if client_info else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        tokens, _ = self._db.get_tenant_tokens(self.chat_id)
        self._db.set_tenant_tokens(self.chat_id, tokens, client_info.model_dump(mode="json"))


class _CallbackHandler(BaseHTTPRequestHandler):
    captured: dict = {}

    def do_GET(self):  # noqa: N802 — http.server interface
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            _CallbackHandler.captured["code"] = params.get("code", [None])[0]
            _CallbackHandler.captured["state"] = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif;text-align:center;padding-top:80px'>"
                b"<h2>Swiggy connected.</h2><p>You can close this tab and return to your terminal.</p>"
                b"</body></html>"
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):  # noqa: A003
        pass


async def _redirect_handler(authorization_url: str) -> None:
    print("\nOpening browser for Swiggy login.")
    print(f"If it doesn't open, paste this into your browser:\n  {authorization_url}\n")
    webbrowser.open(authorization_url)


async def _callback_handler() -> tuple[str, str | None]:
    _CallbackHandler.captured.clear()
    server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    print(f"Waiting for callback on {REDIRECT_URI} ...")
    Thread(target=server.handle_request, daemon=True).start()
    while "code" not in _CallbackHandler.captured:
        await asyncio.sleep(0.1)
    server.server_close()
    code = _CallbackHandler.captured["code"]
    if not code:
        raise RuntimeError("OAuth callback returned no authorization code")
    return code, _CallbackHandler.captured.get("state")


async def main() -> None:
    storage = FileTokenStorage(TOKENS_FILE)
    client_metadata = OAuthClientMetadata(
        client_name="Kya Banao",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    auth = OAuthClientProvider(
        server_url=SWIGGY_MCP_URL,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=_redirect_handler,
        callback_handler=_callback_handler,
    )

    async with streamablehttp_client(SWIGGY_MCP_URL, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"\nLogged in. Discovered {len(tools.tools)} Instamart tools:")
            for t in tools.tools:
                print(f"  - {t.name}")
            print(f"\nTokens saved to {TOKENS_FILE}")
            print("Copy this file to Railway as a secret to deploy.")


if __name__ == "__main__":
    asyncio.run(main())
