"""FastAPI app for the interactive simulator. Every endpoint delegates to
simulator.state.SessionState, which in turn calls engine.pipeline directly
-- this file has no matching logic of its own.

Run: python -m simulator
"""
from __future__ import annotations

import csv
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.ai_client import get_client
from simulator import ask_ledger, seed, webhook
from simulator.state import SessionState

STATIC_DIR = Path(__file__).parent / "static"
OUT_DIR = Path(__file__).parent.parent / "out"
LIVE_LOG_PATH = OUT_DIR / "live_webhook_log.jsonl"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The Anthropic SDK's first import costs several seconds (lazy,
    # one-time per process). Pay it now, at server boot, instead of
    # stalling the first drafted action mid-demo.
    get_client()
    yield


app = FastAPI(title="Tally -- Simulator", lifespan=lifespan)
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


class AskIn(BaseModel):
    question: str


class ProcessAskIn(BaseModel):
    question: str
    # [{"role": "user"|"assistant", "text": str}, ...] -- the last few
    # turns, so a follow-up like "why is that one still open?" can be
    # understood. Optional so a first question needs nothing extra.
    history: list[dict] | None = None
    # Which frozen frame to answer from -- the same view index the page's
    # own panels are currently showing (0 = opening, 64 = fully
    # processed). Without this the chatbot would always answer from the
    # fully-processed end state regardless of what step is on screen.
    step: int | None = None


class ExplainIn(BaseModel):
    payment_id: str


@app.get("/scenarios")
def get_scenarios() -> list[dict]:
    return seed.SCENARIOS


@app.post("/reset")
def reset() -> dict:
    return state.reset()


@app.post("/reset_batch")
def reset_batch() -> dict:
    """Swap the live world for the real submission dataset
    (engine/generate_data.py, 64 payments) -- what Play mode streams through."""
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


@app.post("/ask")
def ask(body: AskIn) -> dict:
    """Section C, "Ask the ledger": strictly read-only -- ask_ledger.answer()
    only ever reads state.snapshot()/state.dial_whatif(), never a mutating
    method, and returns plain text. See simulator/ask_ledger.py."""
    return ask_ledger.answer(body.question, state)


@app.post("/explain")
def explain(body: ExplainIn) -> dict:
    """Section D, "Explain this refusal": explains a decision already made
    (state.explain_context() is a read-only replay, see its docstring) --
    never changes one."""
    ctx = state.explain_context(body.payment_id)
    if ctx is None:
        raise HTTPException(status_code=404,
                             detail=f"{body.payment_id} is not a known exception")
    result = ask_ledger.explain_refusal(ctx)
    return {"payment_id": body.payment_id, **result}


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


def _live_enabled() -> bool:
    return bool(os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET")
                and os.environ.get("RAZORPAY_WEBHOOK_SECRET"))


def _log_webhook(entry: dict) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    with open(LIVE_LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


@app.get("/live/status")
def live_status() -> dict:
    """Whether the Live lane has everything it needs configured. The
    frontend hides the Live tab entirely when this is false, so the repo
    works unmodified for anyone who clones it with no .env at all."""
    return {"enabled": _live_enabled()}


@app.get("/live/state")
def live_state() -> dict:
    return state.live_snapshot()


@app.post("/live/action/send")
def live_action_send(body: ActionSendIn) -> dict:
    state.send_live_action(body.action_id)
    return state.live_snapshot()


@app.post("/webhook/razorpay")
async def razorpay_webhook(request: Request) -> dict:
    """Razorpay payment webhook -- verified, idempotent, logged, and fed
    through the exact same resolve_payment() the rest of the simulator
    uses (via state.submit_live_payment(), see simulator/state.py). 404s
    when RAZORPAY_WEBHOOK_SECRET isn't configured, so this endpoint is
    genuinely absent (not just permission-denied) for anyone who hasn't
    set up the Live lane -- there is no way to verify a signature without
    it, so leaving it reachable-but-rejecting would be a hazard, not a
    convenience."""
    webhook_secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=404, detail="not found")

    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")
    valid = webhook.verify_signature(raw_body, signature, webhook_secret)

    log_entry = {
        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "signature_present": signature is not None,
        "signature_valid": valid,
    }

    if not valid:
        log_entry["outcome"] = "rejected: missing or invalid signature"
        try:
            log_entry["raw_body"] = json.loads(raw_body)
        except Exception:
            log_entry["raw_body_unparseable"] = True
        _log_webhook(log_entry)
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        payload = json.loads(raw_body)
    except Exception:
        log_entry["outcome"] = "rejected: invalid JSON"
        _log_webhook(log_entry)
        raise HTTPException(status_code=400, detail="invalid JSON body")
    log_entry["payload"] = payload

    if not webhook.is_handled_event(payload):
        log_entry["outcome"] = f"ignored: event={payload.get('event')}"
        _log_webhook(log_entry)
        return {"status": "ignored", "event": payload.get("event")}

    try:
        payment = webhook.map_payload_to_payment(payload)
    except (KeyError, TypeError) as exc:
        log_entry["outcome"] = f"rejected: malformed payload ({exc})"
        _log_webhook(log_entry)
        raise HTTPException(status_code=400, detail="malformed payload")

    outcome = state.submit_live_payment(payment)
    log_entry["mapped_payment"] = payment
    log_entry["outcome"] = ("duplicate, already processed" if outcome["duplicate"]
                             else f"processed: resolved={outcome['resolved']}")
    _log_webhook(log_entry)

    # 200 even on a duplicate/ignored event -- Razorpay retries on any
    # non-2xx response, so acknowledging is how a repeat delivery stops.
    return {
        "status": "ok",
        "payment_id": outcome["payment_id"],
        "resolved": outcome["resolved"],
        "duplicate": outcome["duplicate"],
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _exception_by_code(results: dict) -> list[dict]:
    """results['exception_queue']['by_code'] reshaped into a flat, value-
    descending [{code, value_paise}, ...] -- everything scoring/score.py
    already computed (engine.l5_exceptions.build_queue), just re-shaped
    for a chart to consume directly."""
    by_code = results["exception_queue"]["by_code"]
    rows = [{"code": code, "value_paise": d["value_paise"]} for code, d in by_code.items()]
    rows.sort(key=lambda r: -r["value_paise"])
    return rows


def _chart_block(results: dict) -> dict:
    return {
        "buckets": results["buckets_at_default_threshold"],
        "exception_by_code": _exception_by_code(results),
        "threshold_curve": results["threshold_curve"],
        "default_threshold": results["default_threshold"],
    }


@app.get("/story/state")
def story_state() -> dict:
    """The full replay history (one frozen frame per step) plus how far the
    engine has played. Navigation on the page is pure history playback."""
    return state.story_view()


@app.post("/story/step")
def story_step() -> dict:
    """Play the next scripted payment (simulator/story_seed.py) through the
    same resolve_payment() + re-queue path every other world uses, then
    append its frame to the history."""
    return state.story_step()


@app.post("/story/reset")
def story_reset() -> dict:
    return state.reset_story()


@app.post("/story/action/send")
def story_action_send(body: ActionSendIn) -> dict:
    """Mark a drafted message as sent -- on screen only. Nothing leaves the
    building; this just flips the row so the walkthrough can show 'done'."""
    return state.send_story_action(body.action_id)


@app.get("/process/state")
def process_state() -> dict:
    """/app's processing flow: the arrival-screen numbers plus every frozen
    frame of the real 64-payment submission run (13 reordered to the front,
    see simulator/batch_seed.py). Navigation on the page is pure history
    playback, same contract as /story/state."""
    return state.process_view()


@app.post("/process/reset")
def process_reset() -> dict:
    return state.reset_process()


@app.post("/process/action/send")
def process_action_send(body: ActionSendIn) -> dict:
    return state.send_process_action(body.action_id)


@app.post("/process/ask")
def process_ask(body: ProcessAskIn) -> dict:
    """Ask the ledger, for /app's floating chat widget. Strictly
    read-only: state.ask_process() only ever reads (process_ask_context/
    build_customer_answer/explain_why_open), and the function that
    actually composes the reply (ask_ledger.compose_process_answer)
    receives only plain data, never a reference to `state` -- see the
    docstrings on both for the full guarantee. `step` pins the answer to
    the same frozen frame the page is currently showing, so asking at
    step 0 reports "nothing processed yet", not the final state."""
    return state.ask_process(body.question, body.history or [], body.step)


@app.get("/report_summary")
def report_summary() -> dict:
    """Read-only numbers for the landing page's tiles/charts and /app's
    top-of-page charts, sourced straight from out/results.json /
    out/results_holdout.json -- whatever scoring/score.py (and, for
    holdout, scoring/compare's inputs) last wrote, never hardcoded.
    Returns available=False if scoring hasn't been run yet in this
    checkout."""
    results_path = OUT_DIR / "results.json"
    if not results_path.exists():
        return {"available": False}
    with open(results_path) as f:
        results = json.load(f)
    summary = {
        "available": True,
        "payments": results["totals"]["payments"],
        "auto_posted": results["buckets_at_default_threshold"]["auto_posted"],
        "human_touches": results["human_touches"],
        "wrong_matches": len(results["mismatches"]),
        "match_rate": results["overall_match_rate"],
        "precision": results["precision"],
        "recall": results["recall"],
        "f1": results["f1"],
        "true_positives": results["true_positives"],
        "false_positives": results["false_positives"],
        "false_negatives": results["false_negatives"],
        **_chart_block(results),
    }
    holdout_path = OUT_DIR / "results_holdout.json"
    if holdout_path.exists():
        with open(holdout_path) as f:
            holdout = json.load(f)
        summary["holdout"] = {
            "match_rate": holdout["overall_match_rate"],
            "precision_at_default_threshold": holdout["precision_at_default_threshold"],
            "balance_accuracy": holdout["customer_status"]["balance_accuracy"],
            "precision": holdout["precision"],
            "recall": holdout["recall"],
            "f1": holdout["f1"],
            "true_positives": holdout["true_positives"],
            "false_positives": holdout["false_positives"],
            "false_negatives": holdout["false_negatives"],
            **_chart_block(holdout),
        }
    return summary


@app.get("/")
def landing() -> FileResponse:
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/app")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/story")
def story() -> FileResponse:
    """The Story world -- a narrated 13-payment walkthrough. Standalone
    page; shares no state with /app's Demo/Batch/Live views."""
    return FileResponse(STATIC_DIR / "story.html")


@app.get("/live")
def live_page() -> FileResponse:
    """The Live world -- real Razorpay test-mode payments, routed through
    POST /webhook/razorpay into the exact same resolve_payment() as every
    other world. Standalone page, same layout as /story (shops panel,
    decision trace, three result panels), except the payment list grows
    live instead of being a fixed script."""
    return FileResponse(STATIC_DIR / "live.html")
