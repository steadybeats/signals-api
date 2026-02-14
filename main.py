#!/usr/bin/env python3
"""
Phase 1C Signals Backend (FastAPI) — Cloud Edition
Deployed on Railway.app for persistent public URL.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from datetime import datetime
import json
import uuid
import os
import httpx
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List

app = FastAPI(title="Phase 1C Signals Backend", version="1.0.0")

# Configuration — cloud-friendly paths
DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp"))
SIGNALS_LOG = DATA_DIR / "signals-log.json"

# In-memory storage (Railway has ephemeral disk)
signals_store: Dict[str, Dict] = {}
approval_status: Dict[str, str] = {}

# Approved assets (Phase 1 watchlist)
APPROVED_ASSETS = {
    "BTC", "BTCUSD", "BTCUSDT", "XBT", "XBTUSD",
    "ETH", "ETHUSD", "ETHUSDT",
    "XRP", "XRPUSD", "XRPUSDT",
    "ADA", "ADAUSD", "ADAUSDT",
    "SOL", "SOLUSD", "SOLUSDT",
    "DOGE", "DOGEUSD", "DOGEUSDT",
    "LTC", "LTCUSD", "LTCUSDT",
    "BNB", "BNBUSD", "BNBUSDT",
    "AVAX", "AVAXUSD", "AVAXUSDT",
    "FTM", "FTMUSD", "FTMUSDT",
}

# Risk rules
RR_RATIO_MIN = 1.5
RR_RATIO_MAX = 4.0
CONFIDENCE_AUTO_APPROVE = 8
CONFIDENCE_PENDING = 6

# Telegram config
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-100376135844447")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "NOT_CONFIGURED")


async def send_telegram(message: str, parse_mode: str = "HTML"):
    """Send message to Telegram channel"""
    if TELEGRAM_BOT_TOKEN == "NOT_CONFIGURED":
        print(f"[TELEGRAM SKIP] Bot token not configured. Message: {message[:100]}")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "text": message,
                "parse_mode": parse_mode,
            })
            if resp.status_code == 200:
                print(f"[TELEGRAM OK] Sent to {TELEGRAM_CHANNEL_ID}")
                return True
            else:
                print(f"[TELEGRAM ERR] {resp.status_code}: {resp.text}")
                return False
    except Exception as e:
        print(f"[TELEGRAM ERR] {e}")
        return False


def format_signal_telegram(sig: Dict) -> str:
    """Format signal for Telegram message"""
    emoji = "🟢" if sig["signal_type"] == "LONG" else "🔴"
    status_emoji = {"APPROVED": "✅", "PENDING": "⏳", "REJECTED": "❌"}.get(sig["status"], "❓")
    return (
        f"{emoji} <b>{sig['signal_type']} {sig['asset']}</b> {status_emoji}\n"
        f"📊 Entry: <code>{sig['entry_price']}</code>\n"
        f"🛑 Stop: <code>{sig['stop_loss']}</code>\n"
        f"🎯 Target: <code>{sig['take_profit']}</code>\n"
        f"📐 RR: <code>{sig['rr_ratio']}</code> | Score: <code>{sig['confidence_score']}/10</code>\n"
        f"🆔 <code>{sig['id']}</code>\n"
        f"⏰ {sig['timestamp']}"
    )


def log_signal(signal_record: Dict):
    """Append signal to JSON log file"""
    try:
        if SIGNALS_LOG.exists():
            data = json.loads(SIGNALS_LOG.read_text())
        else:
            data = []
        data.append(signal_record)
        if len(data) > 500:
            data = data[-500:]
        SIGNALS_LOG.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[LOG ERR] {e}")


class SignalProcessor:
    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self, payload: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
        self.errors.clear()
        self.warnings.clear()

        required = ["asset", "signal_type", "entry_price", "stop_loss", "take_profit", "rr_ratio", "confidence_score"]
        for field in required:
            if field not in payload:
                self.errors.append(f"Missing required field: {field}")

        if self.errors:
            return False, {"errors": self.errors}

        asset = payload.get("asset", "").upper()
        if asset not in APPROVED_ASSETS:
            self.errors.append(f"Asset '{asset}' not in approved watchlist")

        signal_type = payload.get("signal_type", "").upper()
        if signal_type not in ["LONG", "SHORT"]:
            self.errors.append(f"signal_type must be LONG or SHORT, got '{signal_type}'")

        try:
            entry = float(payload.get("entry_price"))
            stop = float(payload.get("stop_loss"))
            target = float(payload.get("take_profit"))
            if signal_type == "LONG":
                if entry >= target:
                    self.errors.append(f"LONG: entry ({entry}) must be < target ({target})")
                if entry <= stop:
                    self.errors.append(f"LONG: entry ({entry}) must be > stop ({stop})")
            elif signal_type == "SHORT":
                if entry <= target:
                    self.errors.append(f"SHORT: entry ({entry}) must be > target ({target})")
                if entry >= stop:
                    self.errors.append(f"SHORT: entry ({entry}) must be < stop ({stop})")
        except (TypeError, ValueError):
            self.errors.append("Prices must be numeric")

        try:
            rr = float(payload.get("rr_ratio"))
            if rr < RR_RATIO_MIN:
                self.warnings.append(f"RR ratio {rr} below minimum {RR_RATIO_MIN}")
        except (TypeError, ValueError):
            self.errors.append("RR ratio must be numeric")

        try:
            confidence = int(payload.get("confidence_score"))
            if confidence < 0 or confidence > 10:
                self.errors.append(f"Confidence score must be 0-10, got {confidence}")
        except (TypeError, ValueError):
            self.errors.append("Confidence score must be integer")

        if self.errors:
            return False, {"errors": self.errors, "warnings": self.warnings}
        return True, {"valid": True, "warnings": self.warnings}

    @staticmethod
    def determine_status(confidence_score: int, rr_ratio: float) -> str:
        if confidence_score >= CONFIDENCE_AUTO_APPROVE and rr_ratio >= 2.0:
            return "APPROVED"
        elif confidence_score >= CONFIDENCE_PENDING:
            return "PENDING"
        else:
            return "REJECTED"


processor = SignalProcessor()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
        "telegram_configured": TELEGRAM_BOT_TOKEN != "NOT_CONFIGURED",
    }


@app.post("/signals/ingest")
async def ingest_signal(request: Request):
    try:
        payload = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Translate Pine Script webhook format
    if "symbol" in payload and "asset" not in payload:
        entry = float(payload.get("entry", 0))
        stop = float(payload.get("stop", 0))
        tp1 = float(payload.get("tp1", 0))
        side = payload.get("side", "LONG").upper()
        risk = abs(entry - stop) if entry and stop else 1
        reward = abs(tp1 - entry) if tp1 and entry else 0
        rr = round(reward / risk, 2) if risk > 0 else 0
        payload = {
            "asset": payload.get("symbol", ""),
            "signal_type": side,
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": tp1,
            "rr_ratio": rr,
            "confidence_score": int(payload.get("score", 0)),
            "timeframe": payload.get("timeframe", ""),
            "strategy": payload.get("strategy", "Universal Signal Engine"),
        }

    valid, validation_result = processor.validate(payload)
    if not valid:
        return JSONResponse(status_code=400, content={
            "status": "rejected",
            "reason": "validation_failed",
            "errors": validation_result.get("errors"),
        })

    confidence = int(payload.get("confidence_score"))
    rr_ratio = float(payload.get("rr_ratio"))
    status = processor.determine_status(confidence, rr_ratio)

    signal_id = f"SIG-{str(uuid.uuid4())[:8].upper()}"
    timestamp = datetime.utcnow().isoformat() + "Z"

    signal_record = {
        "id": signal_id,
        "timestamp": timestamp,
        "asset": payload.get("asset").upper(),
        "signal_type": payload.get("signal_type").upper(),
        "entry_price": float(payload.get("entry_price")),
        "stop_loss": float(payload.get("stop_loss")),
        "take_profit": float(payload.get("take_profit")),
        "rr_ratio": rr_ratio,
        "confidence_score": confidence,
        "status": status,
    }

    signals_store[signal_id] = signal_record
    approval_status[signal_id] = status
    log_signal(signal_record)

    # Send to Telegram
    telegram_msg = format_signal_telegram(signal_record)
    await send_telegram(telegram_msg)

    return JSONResponse(
        status_code=200 if status != "REJECTED" else 202,
        content={
            "status": "accepted",
            "signal_id": signal_id,
            "approval_status": status,
            "message": f"Signal {signal_id} {status.lower()}",
        },
    )


@app.get("/signals/pending")
async def get_pending_signals():
    pending = [s for s in signals_store.values() if approval_status.get(s["id"]) == "PENDING"]
    return {"count": len(pending), "signals": pending}


@app.get("/signals/approved")
async def get_approved_signals():
    approved = [s for s in signals_store.values() if approval_status.get(s["id"]) == "APPROVED"]
    return {"count": len(approved), "signals": approved}


@app.post("/signals/{signal_id}/approve")
async def approve_signal(signal_id: str):
    if signal_id not in signals_store:
        raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found")
    if approval_status.get(signal_id) != "PENDING":
        raise HTTPException(status_code=400, detail=f"Signal {signal_id} is not pending")
    approval_status[signal_id] = "APPROVED"
    signals_store[signal_id]["status"] = "APPROVED"
    return {"status": "approved", "signal_id": signal_id}


@app.post("/signals/{signal_id}/reject")
async def reject_signal(signal_id: str, reason: Optional[str] = None):
    if signal_id not in signals_store:
        raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found")
    if approval_status.get(signal_id) != "PENDING":
        raise HTTPException(status_code=400, detail=f"Signal {signal_id} is not pending")
    approval_status[signal_id] = "REJECTED"
    signals_store[signal_id]["status"] = "REJECTED"
    return {"status": "rejected", "signal_id": signal_id, "reason": reason}


@app.get("/signals/{signal_id}")
async def get_signal(signal_id: str):
    if signal_id not in signals_store:
        raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found")
    return signals_store[signal_id]


@app.get("/signals")
async def list_signals(status: Optional[str] = None, limit: int = 50):
    signals = list(signals_store.values())
    if status:
        signals = [s for s in signals if approval_status.get(s["id"]) == status]
    return {"count": len(signals), "signals": signals[:limit]}


@app.get("/watchlist")
async def get_watchlist():
    return {"approved_assets": sorted(list(APPROVED_ASSETS)), "count": len(APPROVED_ASSETS)}


@app.get("/stats")
async def get_stats():
    total = len(signals_store)
    return {
        "total_signals": total,
        "approved": len([s for s in signals_store.values() if approval_status.get(s["id"]) == "APPROVED"]),
        "pending": len([s for s in signals_store.values() if approval_status.get(s["id"]) == "PENDING"]),
        "rejected": len([s for s in signals_store.values() if approval_status.get(s["id"]) == "REJECTED"]),
        "telegram_configured": TELEGRAM_BOT_TOKEN != "NOT_CONFIGURED",
    }


@app.get("/")
async def root():
    return {
        "name": "Phase 1C Signals Backend",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "health": "GET /health",
            "ingest": "POST /signals/ingest",
            "pending": "GET /signals/pending",
            "approved": "GET /signals/approved",
            "stats": "GET /stats",
        },
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
