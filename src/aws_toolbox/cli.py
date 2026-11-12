"""Command-line entry points.

`aws-toolbox <command>` dispatches to every command; each command also has
its own console script (aws-sso-refresh, ...). Logs go to stderr so stdout
stays clean for eval-able / JSON output.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from types import ModuleType

from aws_toolbox import __version__, config
from aws_toolbox.commands import assume_role, ns_cert, sso_refresh, ssm_connect, sts_decode, sync_profiles

log = logging.getLogger("aws_toolbox")

COMMANDS: dict[str, tuple[ModuleType, str]] = {
    "sso-refresh": (sso_refresh, "Log in to every SSO start URL in ~/.aws/config"),
    "assume-role": (assume_role, "Assume a role; prints shell exports"),
    "sts-decode": (sts_decode, "Decode an STS authorization failure message"),
    "sync-profiles": (sync_profiles, "Generate profiles for all Organization accounts"),
    "ssm-connect": (ssm_connect, "Open a Session Manager shell on an instance"),
    "ns-cert-refresh": (ns_cert, "Build a CA bundle including Netskope certs"),
}


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Warnings and errors only")


LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
LOG_COLORS = {logging.WARNING: "\033[33m", logging.ERROR: "\033[31m", logging.CRITICAL: "\033[1;31m"}


class ColorFormatter(logging.Formatter):
    """Colors warning and error lines (yellow, red)."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        color = LOG_COLORS.get(record.levelno)
        return f"{color}{text}\033[0m" if color else text


def _use_color(stream) -> bool:
    # https://no-color.org: any non-empty NO_COLOR disables color
    return not os.environ.get("NO_COLOR") and hasattr(stream, "isatty") and stream.isatty()


def _setup_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    formatter = ColorFormatter if _use_color(sys.stderr) else logging.Formatter
    handler.setFormatter(formatter(LOG_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S"))
    logging.basicConfig(level=level, handlers=[handler])
    if not args.verbose:
        logging.getLogger("botocore").setLevel(logging.WARNING)


def _cmd_init(args: argparse.Namespace) -> int:
    path = config.config_path()
    if path.exists() and not args.force:
        log.error("%s already exists (use --force to overwrite)", path)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.example_text())
    log.info("Wrote %s — edit it for your environment", path)
    return 0


def _dispatch(module: ModuleType, args: argparse.Namespace) -> int:
    _setup_logging(args)
    try:
        cfg = config.load()
        return module.run(args, cfg)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # last-resort: one clean line unless -v
        if args.verbose:
            raise
        log.error("%s: %s", type(e).__name__, e)
        return 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aws-toolbox", description="AWS CLI and boto3 toolbox")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help=f"Write a starter config to {config.config_path()}")
    init.add_argument("--force", action="store_true", help="Overwrite an existing config")
    _add_common(init)

    for name, (module, help_text) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text, description=module.__doc__)
        module.add_arguments(p)
        _add_common(p)
        p.set_defaults(module=module)

    args = parser.parse_args(argv)
    if args.command == "init":
        _setup_logging(args)
        sys.exit(_cmd_init(args))
    sys.exit(_dispatch(args.module, args))


def _standalone(name: str, argv: list[str] | None = None) -> None:
    module, _ = COMMANDS[name]
    parser = argparse.ArgumentParser(
        prog=f"aws-{name}" if not name.startswith("ns-") else name,
        description=module.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    module.add_arguments(parser)
    _add_common(parser)
    sys.exit(_dispatch(module, parser.parse_args(argv)))


def sso_refresh_main() -> None:
    _standalone("sso-refresh")


def sts_decode_main() -> None:
    _standalone("sts-decode")


def sync_profiles_main() -> None:
    _standalone("sync-profiles")


def ssm_connect_main() -> None:
    _standalone("ssm-connect")


def ns_cert_refresh_main() -> None:
    _standalone("ns-cert-refresh")
