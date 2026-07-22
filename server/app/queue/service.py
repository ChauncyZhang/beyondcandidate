from collections.abc import Callable
from datetime import timedelta
import re

SAFE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

def normalize_safe_code(value: object) -> str:
    return value if isinstance(value, str) and SAFE_CODE_PATTERN.fullmatch(value) else "internal_error"

class JobError(Exception):
    def __init__(self, safe_code: str) -> None:
        self.safe_code = normalize_safe_code(safe_code); super().__init__(self.safe_code)
class RetryableJobError(JobError): pass
class PermanentJobError(JobError): pass

def retry_delay(attempt: int, *, base_seconds: int = 5, maximum_seconds: int = 300, jitter: Callable[[int], int]) -> timedelta:
    seconds = base_seconds * 2 ** max(0, attempt - 1) + jitter(attempt)
    return timedelta(seconds=min(maximum_seconds, max(0, seconds)))
