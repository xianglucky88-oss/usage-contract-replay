from .models import PipelineResult

def build_result(project: str, data: dict) -> dict:
    result = PipelineResult(project=project, data=data)
    return {"project": result.project, "status": "ok", **result.data}
