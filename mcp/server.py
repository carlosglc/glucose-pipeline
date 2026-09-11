"""
Minimal MCP server exposing Medtronic CareLink data.

Fetches live from CareLink on each call. No persistence — that comes later.

Environment:
    CARELINK_CLIENT_PATH  directory containing carelink_client2.py
    CARELINK_TOKEN_FILE   absolute path to logindata.json
"""

import os
import sys
import time
from datetime import datetime

from mcp.server.mcpserver import MCPServer

CLIENT_PATH = os.environ.get(
    "CARELINK_CLIENT_PATH",
    os.path.expanduser("~/Projects/carelink-python-client"),
)
TOKEN_FILE = os.environ.get(
    "CARELINK_TOKEN_FILE",
    os.path.join(CLIENT_PATH, "logindata.json"),
)

sys.path.insert(0, CLIENT_PATH)
import carelink_client2  # noqa: E402

mcp = MCPServer("glucose")

# CareLink returns the same 24h window on every call, so repeated tool
# calls within one conversation would re-fetch identical data. Cache
# briefly to stay polite to an undocumented endpoint.
_CACHE_TTL_SECONDS = 120
_cache: dict = {"data": None, "at": 0.0}


def _fetch() -> dict:
    """Return patientData, using a short-lived cache."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL_SECONDS:
        return _cache["data"]

    client = carelink_client2.CareLinkClient(tokenFile=TOKEN_FILE)
    if not client.init():
        raise RuntimeError(
            "CareLink authentication failed. The refresh token may have "
            "expired — re-run carelink_carepartner_api_login.py."
        )

    recent = client.getRecentData()
    if not recent or "patientData" not in recent:
        raise RuntimeError("CareLink returned no patient data.")

    data = recent["patientData"]

    # CareLink emits sg=0 for readings that don't exist yet — a pending
    # entry stamped with server time, off the 5-minute grid. Zero is not
    # a glucose value; normalize it to None at the boundary so no
    # downstream consumer has to know this.
    for s in data.get("sgs", []):
        if not s.get("sg"):
            s["sg"] = None
    if not data.get("lastSG", {}).get("sg"):
        data.setdefault("lastSG", {})["sg"] = None

    _cache.update(data=data, at=now)


def _parse(ts: str) -> datetime:
    """CareLink timestamps are naive local time (device timezone)."""
    return datetime.fromisoformat(ts)


def _within(items: list, hours: float) -> list:
    """Filter items to the last N hours, based on the newest timestamp
    present in the payload rather than wall-clock now — the payload may
    lag behind if the pump hasn't uploaded recently."""
    if not items:
        return []
    newest = max(_parse(i["timestamp"]) for i in items)
    cutoff = newest.timestamp() - hours * 3600
    return [i for i in items if _parse(i["timestamp"]).timestamp() >= cutoff]


@mcp.tool()
def get_current_glucose() -> dict:
    """Current glucose reading with trend direction, plus live device state.

    Use this for questions about right now: "what's my glucose?",
    "am I dropping?", "how much insulin is active?"
    """
    d = _fetch()
    return {
        "glucose_mgdl": d.get("lastSG", {}).get("sg"),
        "trend": d.get("lastSGTrend"),
        "reading_time": d.get("lastSG", {}).get("timestamp"),
        "timezone": d.get("clientTimeZoneName"),
        "active_insulin_units": d.get("activeInsulin", {}).get("amount"),
        "smartguard": d.get("therapyAlgorithmState", {}).get("autoModeShieldState"),
        "pump_suspended": d.get("pumpSuspended"),
        "sensor_ok": d.get("gstCommunicationState"),
        "sensor_age_hours": d.get("sensorDurationHours"),
        "reservoir_units": d.get("reservoirRemainingUnits"),
    }


@mcp.tool()
def get_glucose_summary(hours: float = 24) -> dict:
    """Summary statistics over a recent window (max 24 hours).

    Use this for questions like "how was my day?", "how's my control been?",
    "did I go low overnight?"
    """
    d = _fetch()
    window = _within(d.get("sgs", []), hours)
    values = [s["sg"] for s in window if s.get("sg")]

    if not values:
        return {"error": "No sensor readings in that window."}

    in_range = [v for v in values if 70 <= v <= 180]
    return {
        "window_hours": hours,
        "from": window[0]["timestamp"],
        "to": window[-1]["timestamp"],
        "readings": len(values),
        "gaps": len(window) - len(values),
        "average_mgdl": round(sum(values) / len(values), 1),
        "min_mgdl": min(values),
        "max_mgdl": max(values),
        "time_in_range_pct": round(100 * len(in_range) / len(values), 1),
        "time_below_70_pct": round(
            100 * len([v for v in values if v < 70]) / len(values), 1
        ),
        "time_above_180_pct": round(
            100 * len([v for v in values if v > 180]) / len(values), 1
        ),
        "lowest_excursion": min(values) if min(values) < 70 else None,
    }


@mcp.tool()
def get_glucose_readings(hours: float = 6) -> dict:
    """Individual glucose readings, one every five minutes.

    Use when the shape of the curve matters — spikes, rate of change,
    what happened around a specific time. Keep the window small.
    """
    d = _fetch()
    window = _within(d.get("sgs", []), hours)
    return {
        "timezone": d.get("clientTimeZoneName"),
        "count": len(window),
        "readings": [
            {"time": s["timestamp"], "mgdl": s.get("sg")} for s in window
        ],
    }


@mcp.tool()
def get_therapy_events(hours: float = 24) -> dict:
    """Insulin doses, meals, and pump events in a recent window.

    Use this to explain *why* glucose moved: boluses, carbs, auto-basal
    activity, low-glucose suspends.
    """
    d = _fetch()
    window = _within(d.get("markers", []), hours)

    events = []
    for m in window:
        vals = m.get("data", {}).get("dataValues", {})
        event = {"time": m["timestamp"], "type": m["type"]}

        if m["type"] == "INSULIN":
            event["units"] = vals.get("deliveredFastAmount")
            event["reason"] = vals.get("activationType")
        elif m["type"] == "MEAL":
            event["carbs_grams"] = vals.get("amount")
        elif m["type"] == "AUTO_BASAL_DELIVERY":
            event["units"] = vals.get("bolusAmount")
        elif vals:
            event["data"] = vals

        events.append(event)

    auto = [e for e in events if e["type"] == "AUTO_BASAL_DELIVERY"]
    auto_total = sum(float(e.get("units") or 0) for e in auto)

    return {
        "timezone": d.get("clientTimeZoneName"),
        "total_events": len(events),
        "auto_basal_total_units": round(auto_total, 2),
        "events": [e for e in events if e["type"] != "AUTO_BASAL_DELIVERY"],
        "note": (
            f"{len(auto)} auto-basal micro-deliveries totalling "
            f"{auto_total:.2f}U are summarized rather than listed."
        ),
    }


if __name__ == "__main__":
    mcp.run()
