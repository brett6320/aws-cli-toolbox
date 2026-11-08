"""aws-sts-decode: decode an encoded authorization failure message.

DecodeAuthorizationMessage must be called from the account that produced the
message. With an account ID, a matching SSO profile from ~/.aws/config is
used — read-only first, then the admin profile on AccessDenied.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from botocore.exceptions import ClientError

from aws_toolbox import awsconfig, config
from aws_toolbox.commands.assume_role import ACCOUNT_RE
from aws_toolbox.session import get_client, get_session

log = logging.getLogger(__name__)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("message", nargs="?", help="Encoded message (default: read stdin)")
    parser.add_argument("account", nargs="?", help="Account ID that produced the message")
    parser.add_argument("--profile", help="Profile to decode with (overrides account lookup)")
    parser.add_argument("--raw", action="store_true", help="Print the decoded JSON without reformatting")


def candidate_profiles(account: str, cfg: dict) -> list[str]:
    """Profiles for the account: read-only role first, admin role last."""
    try:
        admin_suffixes = tuple({org["admin_suffix"] for org in config.orgs(cfg).values()})
    except ValueError as e:
        log.warning("Ignoring [orgs.*] (%s); using [sync] admin_suffix", e)
        admin_suffixes = (cfg["sync"]["admin_suffix"],)
    names = [p.name for p in awsconfig.sso_profiles(awsconfig.read(config.aws_config_path()))
             if p.account_id == account]
    return sorted(names, key=lambda n: n.endswith(admin_suffixes))


def decode(message: str, profile: str | None, region: str) -> str:
    sts = get_client("sts", get_session(profile, region))
    return sts.decode_authorization_message(EncodedMessage=message)["DecodedMessage"]


def run(args: argparse.Namespace, cfg: dict) -> int:
    if args.message and not args.account and ACCOUNT_RE.match(args.message):
        args.account, args.message = args.message, None  # `aws-sts-decode ACCOUNT < msg`
    if not args.message and sys.stdin.isatty():
        log.info("Paste the encoded message, then Ctrl-D")
    message = (args.message or sys.stdin.read()).strip()
    if not message:
        log.error("No encoded message given")
        return 2
    region = cfg["defaults"]["region"]

    if args.profile or not args.account:
        profiles: list[str | None] = [args.profile]
    else:
        profiles = list(candidate_profiles(args.account, cfg))
        if not profiles:
            log.warning("No profile found for account %s; using current credentials", args.account)
            profiles = [None]

    for i, profile in enumerate(profiles):
        log.debug("Decoding with profile %s", profile or "<current>")
        try:
            decoded = decode(message, profile, region)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDenied", "AccessDeniedException") and i < len(profiles) - 1:
                log.info("Profile %s denied; trying next", profile)
                continue
            log.error("AWS error %s: %s", code, e.response["Error"]["Message"])
            return 1
        print(decoded if args.raw else json.dumps(json.loads(decoded), indent=2))
        return 0
    return 1
