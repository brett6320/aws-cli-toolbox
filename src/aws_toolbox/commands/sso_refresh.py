"""aws-sso-refresh: log in to every distinct SSO start URL in ~/.aws/config.

The main org (or main profile) logs in first and opens the browser. Other
start URLs are logged in with --no-browser so the user can open the printed
URL in an alternate browser profile/container. Each [orgs.<name>] sso_session
is a target even before any profile uses it.

boto3 has no public SSO device-login helper, so this drives `aws sso login`,
which also maintains the token cache the CLI and boto3 read.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

from botocore.exceptions import BotoCoreError, ClientError

from aws_toolbox import awsconfig, config
from aws_toolbox.session import get_client, get_session

log = logging.getLogger(__name__)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="Log in even if the cached token is still valid")
    parser.add_argument("--main-org", help="Override [sso] main_org")
    parser.add_argument("--main-profile", help="Override [sso] main_org/main_profile with a profile")
    parser.add_argument("--list", action="store_true", help="List login targets and token status, then exit")


def plan_logins(
    profiles: list[awsconfig.SsoProfile], main_profile: str | None = None, main_url: str | None = None
) -> tuple[list[awsconfig.SsoProfile], str | None]:
    """One target per token cache entry, main start URL first.

    The main URL is `main_profile`'s start URL if given, else `main_url`.
    Returns (targets, main start URL). Dedupe is by cache key, not URL: an
    sso-session and a direct-URL profile on the same URL cache separately.
    """
    by_key: dict[str, awsconfig.SsoProfile] = {}
    if main_profile:
        main = next((p for p in profiles if p.name == main_profile), None)
        if main is None:
            log.warning("Main profile %r not found or has no SSO settings", main_profile)
            main_url = None
        else:
            by_key[main.cache_key] = main
            main_url = main.start_url
    for p in profiles:
        by_key.setdefault(p.cache_key, p)
    targets = list(by_key.values())
    if main_url:
        main_url = main_url.rstrip("/")
        # Main URL's logins open the browser; keep them ahead of the rest.
        targets.sort(key=lambda p: p.start_url != main_url)
    return targets, main_url


def org_session_targets(
    parser, orgs: dict[str, dict], profiles: list[awsconfig.SsoProfile]
) -> list[awsconfig.SsoProfile]:
    """Each org's [sso-session] that no profile uses yet, as its own target."""
    used = {p.cache_key for p in profiles}
    out = []
    for name, org in orgs.items():
        session = org["sso_session"]
        if not session or session in used:
            continue
        target = awsconfig.sso_session_target(parser, session)
        if target is None:
            log.warning("[orgs.%s] sso_session %r not found in ~/.aws/config", name, session)
            continue
        used.add(session)
        out.append(target)
    return out


def org_start_url(parser, org: dict) -> str | None:
    if org["sso_session"]:
        target = awsconfig.sso_session_target(parser, org["sso_session"])
        return target.start_url if target else None
    return org["sso_start_url"].rstrip("/") or None


def credentials_work(profile: awsconfig.SsoProfile) -> bool:
    """True if the profile can call STS now.

    The cached access token may be expired while botocore can still renew
    it with the refresh token, so this is the real test before prompting.
    Only meaningful for profiles that name an account and role.
    """
    if not (profile.account_id and profile.role_name):
        return False
    try:
        get_client("sts", get_session(profile.name, profile.region or None)).get_caller_identity()
    except (BotoCoreError, ClientError) as e:
        log.debug("Credential check for %s failed: %s", profile.name, e)
        return False
    return True


def run(args: argparse.Namespace, cfg: dict) -> int:
    min_remaining = timedelta(minutes=int(cfg["sso"]["min_remaining_minutes"]))
    cache_dir = config.aws_dir() / "sso" / "cache"

    parser = awsconfig.read(config.aws_config_path())
    try:
        orgs = config.orgs(cfg)
    except ValueError as e:
        log.error("%s: %s", config.config_path(), e)
        return 2
    profiles = awsconfig.sso_profiles(parser)
    profiles += org_session_targets(parser, orgs, profiles)
    main_org = args.main_org or cfg["sso"]["main_org"]
    if args.main_profile or not main_org:
        targets, main_url = plan_logins(profiles, main_profile=args.main_profile or cfg["sso"]["main_profile"])
    elif main_org not in orgs:
        log.error("main_org %r is not an [orgs.*] table in %s", main_org, config.config_path())
        return 2
    else:
        main_url = org_start_url(parser, orgs[main_org])
        if main_url is None:
            log.warning("Can't resolve [orgs.%s] start URL; no login will open the browser", main_org)
        targets, main_url = plan_logins(profiles, main_url=main_url)
    if not targets:
        log.error("No SSO profiles found in %s", config.aws_config_path())
        return 1

    aws = shutil.which("aws")
    if aws is None and not args.list:
        log.error("AWS CLI v2 not found on PATH")
        return 1

    now = datetime.now(timezone.utc)
    failures = 0
    for target in targets:
        expiry = awsconfig.token_expiry(target, cache_dir)
        valid = expiry is not None and expiry - now > min_remaining
        is_main = target.start_url == main_url
        if args.list:
            if expiry is None:
                status = "missing"
            elif expiry <= now:
                status = "expired"
            else:
                status = f"{'valid' if valid else 'refresh due'}, expires {expiry:%Y-%m-%d %H:%M %Z}"
            label = f"(sso-session) {target.name}" if target.session_only else target.name
            print(f"{'*' if is_main else ' '} {label:30} {target.start_url}  [{status}]")
            continue
        if not args.force:
            if valid:
                log.info("Token for %s (%s) valid until %s; skipping", target.start_url, target.name, expiry)
                continue
            if credentials_work(target):
                log.info("Credentials for %s (%s) still work (token refreshed); skipping",
                         target.start_url, target.name)
                continue

        cmd = [aws, "sso", "login", "--sso-session" if target.session_only else "--profile", target.name]
        if not is_main:
            cmd.append("--no-browser")
            log.info("Logging in to %s via %s — open the URL below in the matching browser profile",
                     target.start_url, target.name)
        else:
            log.info("Logging in to %s via %s (browser will open)", target.start_url, target.name)
        # Inherit stdio so the CLI's URL/code prompt reaches the user directly.
        result = subprocess.run(cmd)
        if result.returncode != 0:
            log.error("Login failed for %s (exit %d)", target.name, result.returncode)
            failures += 1
    return 1 if failures else 0
