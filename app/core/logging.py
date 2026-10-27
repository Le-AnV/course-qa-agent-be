"""Application-wide logging setup.

Uvicorn configures its own loggers, but application loggers inherit the root
level. Without this, per-turn timing lines such as the scope-gate and rewrite
durations stay hidden at the default WARNING level.
"""

import logging

from app.config.config import settings

_HTTP_LOGGERS = ("httpx", "httpcore", "urllib3", "google_genai")


def configure_logging() -> None:
    """Apply ``LOG_LEVEL`` to the root logger once, without replacing handlers."""

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # basicConfig is a no-op when a handler already exists (uvicorn, pytest);
    # set the level explicitly so application INFO lines remain visible.
    logging.getLogger().setLevel(level)
    for name in _HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
