"""aws-assume-role: assume ROLE in the current account or an alternate one.

Credentials go to stdout as shell `export` lines (or JSON) for the
`aws-assume-role` shell function to eval. Logs go to stderr.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import re
import shlex

from botocore.exceptions import ClientError

from aws_toolbox.session import get_client, get_session

log = logging.getLogger(__name__)

ACCOUNT_RE = re.compile(r"^\d{12}$")
SESSION_NAME_BAD = re.compile(r"[^\w+=,.@-]")
ENV_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_CREDENTIAL_EXPIRATION")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("role", help="Role name, or full role ARN")
    parser.add_argument("account", nargs="?", help="12-digit account ID (default: current account)")
    parser.add_argument("--profile", help="Source profile (default: current credentials)")
    parser.add_argument("--session-name", help="Role session name (default: [assume_role] session_name or username)")
    parser.add_argument("--duration", type=int, help="Session duration in seconds")
    parser.add_argument("--format", choices=("sh", "fish", "json"), default="sh", help="Output format")


def default_session_name() -> str:
    """Local username, sanitized to RoleSessionName's charset/length."""
    try:
        user = getpass.getuser()
    except (OSError, KeyError):  # no passwd entry (some containers)
        return "aws-toolbox"
    return SESSION_NAME_BAD.sub("-", user)[:64] or "aws-toolbox"


def build_role_arn(role: str, account: str | None, caller_arn: str) -> str:
    if role.startswith("arn:"):
        if account:
            raise ValueError("Pass either a role ARN or an account, not both")
        return role
    partition = caller_arn.split(":")[1]
    caller_account = caller_arn.split(":")[4]
    account = account or caller_account
    if not ACCOUNT_RE.match(account):
        raise ValueError(f"Account must be a 12-digit ID, got {account!r}")
    return f"arn:{partition}:iam::{account}:role/{role}"


def render(creds: dict, fmt: str, role_arn: str) -> str:
    values = {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
        "AWS_CREDENTIAL_EXPIRATION": creds["Expiration"].isoformat(),
    }
    if fmt == "json":
        return json.dumps({"RoleArn": role_arn, **values}, indent=2)
    if fmt == "fish":
        lines = ["set -e AWS_PROFILE;"]
        lines += [f"set -gx {k} {shlex.quote(v)};" for k, v in values.items()]
        return "\n".join(lines)
    # Profile must go, or the CLI prefers it over the env credentials.
    lines = ["unset AWS_PROFILE;"]
    lines += [f"export {k}={shlex.quote(v)};" for k, v in values.items()]
    return "\n".join(lines)


def run(args: argparse.Namespace, cfg: dict) -> int:
    session = get_session(args.profile, cfg["defaults"]["region"])
    sts = get_client("sts", session)
    try:
        caller = sts.get_caller_identity()
        role_arn = build_role_arn(args.role, args.account, caller["Arn"])
    except ValueError as e:
        log.error("%s", e)
        return 2
    except ClientError as e:
        log.error("AWS error %s: %s", e.response["Error"]["Code"], e.response["Error"]["Message"])
        return 1

    session_name = args.session_name or cfg["assume_role"]["session_name"] or default_session_name()
    duration = args.duration or int(cfg["assume_role"]["duration_seconds"])
    log.info("Assuming %s as %s (session %s, %ds)", role_arn, caller["Arn"], session_name, duration)
    try:
        resp = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name, DurationSeconds=duration)
    except ClientError as e:
        log.error("AWS error %s: %s", e.response["Error"]["Code"], e.response["Error"]["Message"])
        return 1

    print(render(resp["Credentials"], args.format, role_arn))
    log.info("Credentials valid until %s", resp["Credentials"]["Expiration"])
    return 0
