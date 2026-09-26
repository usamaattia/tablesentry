"""tablesentry: lightweight data quality monitoring for SQL tables."""

__version__ = "0.1.0"

from .models import CheckResult, Status  # noqa: E402

__all__ = ["CheckResult", "Status", "__version__"]
