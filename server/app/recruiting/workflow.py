from collections.abc import Iterable


def normalized_workflow_rounds(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(name, str) or not name for name in value):
        return None
    return tuple(value)


def next_interview_round(value: object, completed_rounds: Iterable[str]) -> str | None:
    rounds = normalized_workflow_rounds(value)
    if rounds is None:
        return None
    completed = set(completed_rounds)
    return next((name for name in rounds if name not in completed), None)
