"""HTTP entry point. Channel-agnostic: a paid gateway (Sendblue, WhatsApp) later
posts to /inbound instead of the local iMessage bridge.

    uvicorn dibs.api:app --reload
"""

from fastapi import FastAPI
from pydantic import BaseModel

from . import db
from .agent import run_turn
from .catalog import Catalog
from . import llm as models

app = FastAPI(title="Dibs")
conn = db.connect()
catalog = Catalog.load()
_llm = None


class Inbound(BaseModel):
    handle: str
    text: str
    conv_id: str | None = None
    is_group: bool = False


@app.get("/health")
def health() -> dict:
    return {"ok": True, "venues": len(catalog.venues), "deals": len(catalog.deals)}


@app.post("/inbound")
def inbound(msg: Inbound) -> dict:
    global _llm
    _llm = _llm or models.for_job("chat")
    alerts: list[str] = []
    reply = run_turn(conn, catalog, _llm, msg.conv_id or f"api;-;{msg.handle}", msg.handle, msg.text,
                     alerts.append, is_group=msg.is_group)
    return {"reply": reply, "operator_alerts": alerts}
