import logging

from mailtrace.config import Config

logger = logging.getLogger("mailtrace")


def init_logger(config: Config):
    log_level = config.log_level
    logger.setLevel(log_level)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
