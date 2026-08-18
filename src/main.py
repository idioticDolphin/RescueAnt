import argparse
from pathlib import Path


def _parse_args():
    parser = argparse.ArgumentParser(description="Run the RescueAnt crawl workflow.")
    parser.add_argument(
        "config_path", nargs="?", default=None,
        help="Path to a bot.config-style file (default: bot.config at the project root)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    # config_service.get_config() is called at *import time* by most other
    # model modules (fetching_service, category_service, ...), so a custom
    # config has to be loaded before model.orchestrator (or anything it
    # imports) gets imported below.
    import model.tools.config_service as config_service
    if args.config_path is not None:
        config_service.load_config(config_service._read_config(Path(args.config_path)))

    import model.orchestrator as orchestrator
    orchestrator.run()
