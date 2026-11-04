import logging

from aiohttp import web

from .channel import Channel
from .config import Config
from .server import create_app


def main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger(__name__).info("Channel %s, serving on %s:%d", cfg.channel_url, cfg.host, cfg.port)
    web.run_app(create_app(Channel(cfg)), host=cfg.host, port=cfg.port, print=None)


if __name__ == "__main__":
    main()
