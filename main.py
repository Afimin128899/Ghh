from __future__ import annotations

from fastapi import FastAPI

from session_shop import __version__


app = FastAPI(title="Session Shop health API", version=__version__)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "session-shop",
        "version": __version__,
        "message": "The Telegram bot runs from app.py and requires a long-lived worker.",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "version": __version__}
