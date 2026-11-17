import argparse
import json
from pathlib import Path
from .pipeline import run

def main() -> None:
    parser = argparse.ArgumentParser(description='Replay usage events against versioned SaaS contracts and produce deterministic invoices.')
    parser.add_argument("input", nargs="?", default=str(Path(__file__).parents[2] / "fixtures" / "input.json"))
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.input).read_text())
    except (OSError, json.JSONDecodeError) as error:
        # A malformed input file is a deterministic error envelope, not a
        # traceback; the exit code still signals failure to a caller.
        print(json.dumps({"project": "usage-contract-replay", "status": "error",
                          "report_version": 1,
                          "error": {"code": "input_unreadable", "message": str(error)}},
                         ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(run(payload), ensure_ascii=False, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
