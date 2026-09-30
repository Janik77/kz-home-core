"""Application logging, independent of Uvicorn's server-only loggers."""

import logging


def configure_logging() -> None:
    logger = logging.getLogger("app")
    # Respect an operator-supplied application/root logging configuration.
    if logger.hasHandlers():
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
