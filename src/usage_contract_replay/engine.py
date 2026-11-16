def calculate(rows, payload):
    contract=payload["contract"]; total=sum(int(r["units"]) for r in rows); billable=max(0,total-int(contract.get("free",0))); amount=round(billable*float(contract["unit_price"])-float(contract.get("refund",0)),2)
    return {"account":rows[0]["account"] if rows else None,"total_units":total,"billable_units":billable,"amount":amount,"event_count":len(rows)}
