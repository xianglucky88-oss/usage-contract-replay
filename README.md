# Usage Contract Replay

Replay usage events against versioned SaaS contracts and produce deterministic invoices.

## Modules

- `src/usage_contract_replay/models.py`: immutable result contract.
- `src/usage_contract_replay/parser.py`: input validation and record extraction.
- `src/usage_contract_replay/engine.py`: domain calculation only.
- `src/usage_contract_replay/report.py`: stable output envelope.
- `src/usage_contract_replay/pipeline.py`: orchestration boundary.
- `src/usage_contract_replay/cli.py`: deterministic JSON CLI.

## Run

```bash
PYTHONPATH=src python -m usage_contract_replay.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.
