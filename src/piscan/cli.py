import argparse
import sys
from pathlib import Path

from piscan.config import ConfigError, load_config

DEFAULT_CONFIG = Path("/etc/piscan.toml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="piscan")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("serve", "run the web UI and ingest loop"),
        ("check", "check config and connectivity"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        load_config(args.config)
    except ConfigError as e:
        print(f"piscan: {e}", file=sys.stderr)
        return 1
    # serve and check are implemented in a later task
    return 0
