from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class PipelineResult:
    project: str
    data: dict[str, Any]
