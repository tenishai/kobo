"""API error copy stays plain: no em dashes in string literals that
reach users. Scans the JSONResponse error bodies in main.py by source
inspection, comments stripped."""
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "kobo_api" / "main.py"


def test_no_em_dashes_in_error_literals():
    stripped = re.sub(r"#[^\n]*", "", SOURCE.read_text())
    offenders = [
        line.strip()
        for line in stripped.splitlines()
        if "—" in line and ('"error"' in line or "error':" in line or '"detail"' in line)
    ]
    assert offenders == [], offenders
