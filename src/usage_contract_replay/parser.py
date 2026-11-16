def load_records(payload: dict) -> list[dict]:
    rows = payload.get("events", [])
    if not isinstance(rows, list):
        raise ValueError("events must be a list")
    return [dict(row) for row in rows if isinstance(row, dict)]
