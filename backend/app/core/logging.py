"""Logging setup.

Deliberately small. The one non-obvious rule enforced here: the ``password``
and ``token`` fields are scrubbed by the formatter's filter so a careless
``logger.info("body=%s", body)`` in a future route handler cannot leak
credentials into Railway's log stream.
"""

from __future__ import annotations

import logging
import sys

_SENSITIVE_KEYS = ("password", "password_hash", "token", "secret", "authorization")


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):
            for key in _SENSITIVE_KEYS:
                if key in record.args:
                    record.args = {**record.args, key: "***redacted***"}
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s :: %(message)s")
    )
    handler.addFilter(SensitiveDataFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # SQLAlchemy's engine logger is very chatty at INFO.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
