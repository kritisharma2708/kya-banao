# Kya Banao? 🍽️

> *"What to cook?"* — An AI meal planning agent for two-person households, built on Swiggy MCP.

## What it does

Kya Banao? eliminates the daily mental load of deciding what to eat. It manages meals across three modes — cook days, Swiggy order days, and Instamart grocery runs — tailored to two people with different dietary preferences and goals.

- **Cook calendar awareness** — knows when the cook is off and automatically switches to order mode
- **Two-person profiles** — each person's preferences, restrictions, and health goals tracked separately
- **Pantry intelligence** — tracks what's at home, surfaces expiring items, auto-reorders staples
- **Discovery engine** — introduces 1 new snack, vegetable, or dish per week so you stop repeating
- **Proactive, not reactive** — suggests meals before you think to ask; sends a weekly plan every Sunday to both users via Telegram

## Built on

- [Swiggy MCP](https://mcp.swiggy.com) — Swiggy Food + Swiggy Instamart
- [Claude API](https://anthropic.com) — agent reasoning and conversation
- FastAPI — callback handler for Swiggy OAuth
- Telegram Bot API — user interface for both household members
- SQLite — token storage and user profiles
- Railway — deployment

## Project structure

```
kya-banao/
├── main.py          # FastAPI app — OAuth callback handler
├── spec.md          # Full product spec
├── requirements.txt
├── railway.toml     # Railway deployment config
└── .env.example     # Required environment variables
```

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in your keys in .env

uvicorn main:app --reload
```

## Status

Early stage — OAuth callback handler is live. Agent logic, Telegram interface, and Swiggy MCP integration coming next.
