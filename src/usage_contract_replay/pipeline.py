from .parser import load_records
from .engine import calculate
from .report import build_result

def run(payload: dict) -> dict:
    records = load_records(payload)
    return build_result('usage-contract-replay', calculate(records, payload))
