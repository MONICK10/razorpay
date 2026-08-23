"""FastAPI app for the interactive simulator. Every endpoint delegates to
simulator.state.SessionState, which in turn calls engine.pipeline directly
-- this file has no matching logic of its own.

Run: python -m simulator
"""
from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.ai_client import get_client
from simulator import seed
from simulator.state import SessionState

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The Anthropic SDK's first import costs several seconds (lazy,
    # one-time per process). Pay it now, at server boot, instead of
    # stalling the first drafted action mid-demo.
    get_client()
    yield


app = FastAPI(title="AI Finance Controller -- Simulator", lifespan=lifespan)
state = SessionState()


class PaymentIn(BaseModel):
    amount_paise: int
    virtual_account: str | None = None
    payer_name: str | None = None
    narration: str = ""
    method: str = "NEFT"
    # Set by Play mode when streaming the real submission batch, so the
    # ledger shows the same payment_id/timestamp as the actual dataset
    # instead of a fresh PAY-SIM-#### and "now". Manual entry and presets
    # leave these unset and get auto-generated ids as before.
    payment_id: str | None = None
    created_at: str | None = None


class DialIn(BaseModel):
    threshold: int


class UploadIn(BaseModel):
    csv_text: str


class ActionSendIn(BaseModel):
    action_id: str


@app.get("/scenarios")
def get_scenarios() -> list[dict]:
    return seed.SCENARIOS


@app.post("/reset")
def reset() -> dict:
    return state.reset()


@app.post("/reset_batch")
def reset_batch() -> dict:
    """Swap the live world for the real 60-payment submission dataset
    (engine/generate_data.py) -- what Play mode streams through."""
    return state.reset_batch()


@app.get("/state")
def get_state() -> dict:
    return state.snapshot()


@app.post("/payment")
def submit_payment(payment: PaymentIn) -> dict:
    fields = payment.model_dump(exclude={"payment_id", "created_at"})
    return state.submit_payment(fields, payment_id=payment.payment_id,
                                 created_at=payment.created_at)


@app.post("/dial")
def set_dial(dial: DialIn) -> dict:
    return state.apply_dial(dial.threshold)


@app.post("/actions/send")
def send_action(body: ActionSendIn) -> dict:
    """Marks a drafted action (QUERY_CUSTOMER / SEND_STATEMENT) as sent.
    Nothing is actually sent -- this only flips the on-screen status, per
    the brief: show the draft, don't send it."""
    return {"action_id": body.action_id, "state": state.send_action(body.action_id)}


@app.post("/upload")
def upload(payload: UploadIn) -> dict:
    """CSV columns: amount_paise, virtual_account, payer_name, narration,
    method -- same fields as POST /payment's body, processed in row order
    through the same submit_payment() path (re-queue included)."""
    reader = csv.DictReader(io.StringIO(payload.csv_text))
    results = []
    for row in reader:
        fields = {
            "amount_paise": int(row["amount_paise"]),
            "virtual_account": row.get("virtual_account") or None,
            "payer_name": row.get("payer_name") or None,
            "narration": row.get("narration", ""),
            "method": row.get("method") or "NEFT",
        }
        results.append(state.submit_payment(fields))
    resolved_on_requeue = [pid for r in results for pid in r["resolved_on_requeue"]]
    return {
        "processed": len(results),
        "resolved_on_requeue": resolved_on_requeue,
        "results": results,
        "state": state.snapshot(),
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
