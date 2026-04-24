from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import sqlite3
import os

app = FastAPI(title="Kya Banao?")

DB_PATH = os.getenv("DATABASE_URL", "kya_banao.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            user_id TEXT PRIMARY KEY,
            access_token TEXT,
            refresh_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


@app.on_event("startup")
async def startup():
    init_db()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Kya Banao?"}


@app.get("/callback", response_class=HTMLResponse)
async def swiggy_callback(code: str = None, state: str = None, error: str = None):
    if error:
        return "<html><body><h2>Authorization failed. Please try again.</h2></body></html>"

    # TODO: exchange code for access token via Swiggy MCP OAuth and store against state (user_id)

    return """
    <html>
    <body style="font-family: sans-serif; text-align: center; padding-top: 80px;">
        <h2>Swiggy connected successfully!</h2>
        <p>You can close this tab and return to the chat.</p>
    </body>
    </html>
    """
