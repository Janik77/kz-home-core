import subprocess
import sys


def test_application_info_visible_under_uvicorn_default_logging():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
import logging.config
from uvicorn.config import LOGGING_CONFIG
from app.core.logging import configure_logging
logging.config.dictConfig(LOGGING_CONFIG)
configure_logging()
configure_logging()
logging.getLogger('app.transports.mqtt').info(
    'MQTT connected; inbound subscriptions restored')
""",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stderr.count("MQTT connected; inbound subscriptions restored") == 1
