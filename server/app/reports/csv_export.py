import csv
from io import StringIO


DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_csv_cell(value: object) -> str:
    rendered = "" if value is None else str(value)
    return "'" + rendered if rendered.startswith(DANGEROUS_PREFIXES) else rendered


def render_application_csv(rows: list[dict[str, object]]) -> bytes:
    output = StringIO(newline="")
    fields = ("application_id", "job_id", "candidate_id", "candidate_name", "stage", "source", "created_at")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: sanitize_csv_cell(row.get(field)) for field in fields})
    return output.getvalue().encode("utf-8-sig")
