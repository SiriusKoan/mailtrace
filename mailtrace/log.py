import logging

from .config import load_config

config = load_config()
log_level = config.log_level

logger = logging.getLogger("mailtrace")
logger.setLevel(log_level)
stream_handler = logging.StreamHandler()
stream_handler.setLevel(log_level)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)
