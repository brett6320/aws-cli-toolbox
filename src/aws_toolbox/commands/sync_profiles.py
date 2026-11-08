"""aws-sync-profiles: build profiles for every account in each Organization.

SSO mode (default): each active account gets `<name>` (read-only role) and
`<name><admin_suffix>` (privileged role) as SSO profiles.

Assume-role mode (org sets source_profile): accounts come from the org tree,
short names, business domains and environments from the account-mapping
DynamoDB table (same .env keys as the switch-role bookmarks generator), and each
account gets one `<short name>-<role>` profile per role, chained from
source_profile with role_arn.

Each org's profiles are written to its own marked block in ~/.aws/config;
everything outside those blocks is left untouched. Read-only against AWS.
Dry-run by default.
"""

from __future__ import annotations

import argparse
import configparser
import difflib
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from aws_toolbox import account_meta, awsconfig, config
from aws_toolbox.session import get_client, get_session
log = logging.getLogger(__name__)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execute", action="store_true", help="Write ~/.aws/config (default: dry-run)")
    parser.add_argument("--apply", action="store_true", help="Alias for --execute")
    parser.add_argument("--org", action="append", help="Only sync this org (repeatable; default: all)")
    parser.add_argument("--profile", help="Override the org's management_profile (needs a single org)")
    parser.add_argument("--force", action="store_true", help="Write even if no accounts were found")
    meta = parser.add_argument_group("assume-role mode (override the org's env_file; need a single org)")
    meta.add_argument("--ddb-table-arn", help="Account metadata table ARN (BM_DDB_TABLE_ARN)")
    meta.add_argument("--ddb-profile", help="Profile for the table's account (BM_DDB_PROFILE)")
    meta.add_argument("--ddb-role-name", help="Role assumed in the table's account (BM_DDB_ROLE_NAME)")
    meta.add_argument("--attr-account-id", help="Table attribute: account number (BM_ATTR_ACCOUNT_ID)")
    meta.add_argument("--attr-short-name", help="Table attribute: short name (BM_ATTR_SHORT_NAME)")
    meta.add_argument("--attr-ou", help="Table attribute: business domain (BM_ATTR_OU)")
    meta.add_argument("--attr-environment", help="Table attribute: environment (BM_ATTR_ENVIRONMENT)")
    meta.add_argument("--roles", help="Comma-separated roles, one profile each (BM_DEFAULT_ROLES)")
    meta.add_argument("--include-inactive", action="store_const", const=True,
                      help="Include suspended/closed accounts (BM_INCLUDE_INACTIVE)")


# Flags that map straight onto account_meta settings.
META_FLAGS = ("ddb_table_arn", "ddb_profile", "ddb_role_name", "attr_account_id", "attr_short_name",
              "attr_ou", "attr_environment", "roles", "include_inactive")


def list_accounts(org_client) -> list[dict]:
    accounts = []
    for page in org_client.get_paginator("list_accounts").paginate():
        for acct in page["Accounts"]:
            # `State` supersedes the deprecated `Status`.
            if (acct.get("State") or acct.get("Status")) == "ACTIVE":
                accounts.append({"id": acct["Id"], "name": acct["Name"]})
    # Tie-break on ID so duplicate names map to the same profiles every run.
    return sorted(accounts, key=lambda a: (a["name"].lower(), a["id"]))


def _profile_section(name: str, account_id: str, role: str, org: dict, defaults: dict) -> str:
    lines = [f"[profile {name}]"]
    if org["sso_session"]:
        lines.append(f"sso_session = {org['sso_session']}")
    else:
        lines.append(f"sso_start_url = {org['sso_start_url']}")
        lines.append(f"sso_region = {org['sso_region']}")
    lines += [
        f"sso_account_id = {account_id}",
        f"sso_role_name = {role}",
        f"region = {defaults['region']}",
        f"output = {defaults['output']}",
    ]
    return "\n".join(lines)


def build_block(accounts: list[dict], org: dict, defaults: dict, reserved: set[str]) -> str:
    excluded = {str(a) for a in org["exclude_accounts"]}
    suffix = org["admin_suffix"]
    sections, taken = [], set(reserved)
    for acct in accounts:
        if acct["id"] in excluded:
            log.debug("Excluding %s (%s)", acct["name"], acct["id"])
            continue
        base = awsconfig.profile_name_for(acct["name"], org["profile_prefix"])
        if base == org["profile_prefix"]:
            base = f"{org['profile_prefix']}{acct['id']}"  # name had no usable characters
        # Names aren't unique in an Organization, and "Dev Adm" collides with
        # "Dev" + suffix; fall back to an ID-qualified name on any clash.
        if base in taken or base + suffix in taken:
            log.warning("Profile name %s for %s (%s) is taken; using %s-%s",
                        base, acct["name"], acct["id"], base, acct["id"])
            base = f"{base}-{acct['id']}"
        for name, role in ((base, org["readonly_role"]), (base + suffix, org["admin_role"])):
            if name in taken:
                log.warning("Skipping %s: name already in use", name)
                continue
            taken.add(name)
            sections.append(_profile_section(name, acct["id"], role, org, defaults))
    return "\n\n".join(sections)


SUFFIX_RE = re.compile(r"^(-[a-z0-9]+)*$")


def role_suffixes(roles: list[str], mapping: dict) -> list[tuple[str, str]]:
    """(role, profile-name suffix) per role: mapping[role], else "-<role slug>"."""
    if not isinstance(mapping, dict):
        raise ValueError("role_suffixes must be a table of role = \"suffix\"")
    out, seen = [], {}
    for role in roles:
        suffix = mapping.get(role, f"-{awsconfig.profile_name_for(role)}")
        if not isinstance(suffix, str) or not SUFFIX_RE.match(suffix):
            raise ValueError(f"role_suffixes: {role} = {suffix!r}; use \"\" or lowercase like \"-ro\"")
        if suffix in seen:
            raise ValueError(f"roles {seen[suffix]} and {role} would both use suffix {suffix!r}")
        seen[suffix] = role
        out.append((role, suffix))
    return out


def _comment_text(value) -> str:
    # Table values end up in ~/.aws/config: a newline would start a new line
    # (e.g. a [profile] with credential_process). Collapse all control chars.
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()


def build_role_block(accounts: list[dict], org: dict, defaults: dict, roles: list[tuple[str, str]],
                     partition: str, reserved: set[str]) -> str:
    """Assume-role profiles: one per (account, role), chained from org["source_profile"].

    `roles` is [(role name, profile-name suffix)] from role_suffixes().
    """
    excluded = {str(a) for a in org["exclude_accounts"]}
    prefix = org["profile_prefix"]
    kept = [a for a in accounts if a["id"] not in excluded]

    def base_for(acct: dict) -> str:
        base = awsconfig.profile_name_for(acct["shortName"], prefix)
        return base if base != prefix else f"{prefix}{acct['id']}"  # no usable characters

    # Short codes come from a shared table and can repeat. Every account in a
    # clash gets its ID, so no account silently inherits another's plain name.
    counts: dict[str, int] = {}
    for acct in kept:
        counts[base_for(acct)] = counts.get(base_for(acct), 0) + 1
    sections, taken = [], set(reserved)
    expected: dict[str, set[str]] = {}
    for acct in sorted(kept, key=lambda a: (a["shortName"].lower(), a["id"])):
        base = base_for(acct)
        names = [(f"{base}{suffix}", role) for role, suffix in roles]
        if counts[base] > 1 or any(n in taken for n, _ in names):
            log.warning("Profile name %s for %s (%s) is shared or taken; using %s-%s",
                        base, acct["shortName"], acct["id"], base, acct["id"])
            base = f"{base}-{acct['id']}"
            names = [(f"{base}{suffix}", role) for role, suffix in roles]
        comment = (f"# {_comment_text(acct['name'])} ({acct['id']}) | domain: {_comment_text(acct['domain'])} | "
                   f"env: {acct['env'] or '-'}")
        for name, role in names:
            if name in taken:
                log.warning("Skipping %s: name already in use", name)
                continue
            taken.add(name)
            expected[f"profile {name}"] = {"role_arn", "source_profile", "region", "output"}
            sections.append("\n".join([
                comment,
                f"[profile {name}]",
                f"role_arn = arn:{partition}:iam::{acct['id']}:role/{role}",
                f"source_profile = {org['source_profile']}",
                f"region = {defaults['region']}",
                f"output = {defaults['output']}",
            ]))
    block = "\n\n".join(sections)
    # Belt and braces: the block must contain exactly what we meant to write.
    parser = awsconfig.read_text(block)
    actual = {sec: set(parser[sec]) for sec in parser.sections()}
    if actual != expected:
        raise ValueError("Generated profiles contain unexpected sections or keys; not writing")
    return block


def _atomic_write(path: Path, text: str) -> None:
    target = path.resolve()  # write through symlinks (dotfile managers)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        os.unlink(tmp)
        raise


def _label(name: str) -> str:
    return f"[orgs.{name}]" if name else "[sync]"


def _check_org(name: str, org: dict, text: str, profile: str | None) -> str | None:
    """Why this org's settings can't be used against `text`, or None if they can.

    `profile` is the management profile in effect (--profile or the org's).
    """
    if not org["sso_session"] and not org["sso_start_url"]:
        return "set sso_session or sso_start_url"
    if "example.awsapps.com" in org["sso_start_url"]:
        return "sso_start_url is still the example placeholder"
    parser = awsconfig.read_text(text)
    if org["sso_session"]:
        if f"[sso-session {org['sso_session']}]" not in text:
            return f"[sso-session {org['sso_session']}] not found in ~/.aws/config; add it first"
        target = awsconfig.sso_session_target(parser, org["sso_session"])
        if target is None:
            return f"[sso-session {org['sso_session']}] has no sso_start_url"
        url = target.start_url
    else:
        url = org["sso_start_url"].rstrip("/")
    source = org["source_profile"]
    if source:
        if source not in awsconfig.reserved_profile_names(text, name):
            return f"source_profile {source} must be defined in ~/.aws/config outside this org's managed block"
        if awsconfig.profile_start_url(parser, source) != url:
            # Every generated profile chains from it, so it has to be this org's.
            return f"source_profile {source} doesn't sign in at this org's portal {url}"
    if not name:
        return None  # [sync]-only setup: no other org to confuse it with
    if profile not in awsconfig.profile_names(parser):
        return f"management profile {profile} is not defined in ~/.aws/config"
    # Listing one org's accounts with another org's profile would mislabel them.
    mgmt_url = awsconfig.profile_start_url(parser, profile)
    if mgmt_url is None:
        return (f"can't tell which portal management profile {profile} signs in at; "
                "use an SSO profile (or a role chained from one) for this org")
    if mgmt_url != url:
        return f"management profile {profile} signs in at {mgmt_url}, not this org's portal {url}"
    return None


def meta_settings(org: dict, args: argparse.Namespace) -> dict:
    """account_meta settings for an assume-role org: flags > toml > env > env_file > defaults."""
    env_path = None
    if org["env_file"]:
        env_path = Path(org["env_file"]).expanduser()
        if not env_path.is_absolute():
            env_path = config.config_path().parent / env_path  # relative to toolbox.toml
        if not env_path.is_file():
            raise ValueError(f"env_file {env_path} not found")
    kwargs = {k: getattr(args, k) for k in META_FLAGS if getattr(args, k, None) is not None}
    roles = org["roles"]
    if isinstance(roles, str):
        roles = roles.split(",")
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise ValueError("roles must be a list of role names")
    roles = [r.strip() for r in roles if r.strip()]
    if roles and "roles" not in kwargs:
        kwargs["roles"] = roles
    if args.profile or org["management_profile"]:
        kwargs["profile"] = args.profile or org["management_profile"]
    dotenv = account_meta.read_dotenv(env_path)
    for var in account_meta.env_overrides(dotenv):
        log.warning("%s from the environment overrides a different value in %s", var, env_path)
    settings = account_meta.resolve_settings(kwargs, dotenv)
    if not settings["roles"]:
        raise ValueError("no roles configured (roles, BM_DEFAULT_ROLES or --roles)")
    settings["role_suffixes"] = role_suffixes(settings["roles"], org["role_suffixes"])
    return settings


def _sync_role_org(name: str, org: dict, text: str, settings: dict, args: argparse.Namespace,
                   cfg: dict) -> str | None:
    """Assume-role mode: org tree + metadata table -> role_arn profiles."""
    s = settings
    log.debug("%s: org profile %s, table %s", _label(name), s["profile"], s["ddb_table_arn"])
    try:
        org_session = account_meta.get_session(s["profile"], s["region"])
        tree = account_meta.fetch_org(get_client("organizations", org_session), s["include_inactive"])
        ddb_session = account_meta.get_ddb_session(s, org_session)
        meta, dupes = account_meta.fetch_metadata(
            get_client("dynamodb", ddb_session, s["ddb_region"]),
            s["ddb_table"],
            {"account_id": s["attr_account_id"], "short_name": s["attr_short_name"],
             "ou": s["attr_ou"], "environment": s["attr_environment"]},
        )
        partition = org_session.get_partition_for_region(s["region"])
    except ClientError as e:
        log.error("%s: AWS error %s: %s", _label(name), e.response["Error"]["Code"],
                  e.response["Error"]["Message"])
        return None
    except (BotoCoreError, RuntimeError) as e:
        log.error("%s: %s", _label(name), e)
        return None
    tree = {**tree, "accounts": {k: v for k, v in tree["accounts"].items() if k not in dupes}}
    accounts = account_meta.merge(tree, meta)
    matched = sum(1 for a in accounts if a["hasMetadata"])
    if accounts and not matched and not args.force:
        # Wrong table/BM_ATTR_* names would otherwise rename every profile.
        log.error("%s: no account matched a row in %s; check BM_DDB_TABLE_ARN and BM_ATTR_* "
                  "(use --force to generate names from account names)", _label(name), s["ddb_table"])
        return None
    if org["require_short_name"]:
        named = [a for a in accounts if str(meta.get(a["id"], {}).get("shortName") or "").strip()]
        log.info("%s: %d of %d accounts have a short name in the table; skipping the rest",
                 _label(name), len(named), len(accounts))
        accounts = named
    block = build_role_block(accounts, org, cfg["defaults"], s["role_suffixes"], partition,
                             awsconfig.reserved_profile_names(text, name))
    if not block and not args.force:
        log.error("%s: no profiles to write; refusing to empty its managed block (use --force)", _label(name))
        return None
    return awsconfig.replace_managed_block(text, block, name)


def _sync_org(name: str, org: dict, text: str, args: argparse.Namespace, cfg: dict) -> str | None:
    """`text` with the org's block regenerated, or None if the org failed."""
    profile = args.profile or org["management_profile"]
    log.debug("%s: listing accounts with profile %s", _label(name), profile)
    try:
        client = get_client("organizations", get_session(profile, cfg["defaults"]["region"]))
        accounts = list_accounts(client)
    except ClientError as e:
        log.error("%s: AWS error %s: %s", _label(name), e.response["Error"]["Code"],
                  e.response["Error"]["Message"])
        return None
    except BotoCoreError as e:
        log.error("%s: %s", _label(name), e)
        return None
    log.info("%s: found %d active accounts", _label(name), len(accounts))
    block = build_block(accounts, org, cfg["defaults"], awsconfig.reserved_profile_names(text, name))
    if not block and not args.force:
        log.error("%s: no profiles to write; refusing to empty its managed block (use --force)", _label(name))
        return None
    return awsconfig.replace_managed_block(text, block, name)


def run(args: argparse.Namespace, cfg: dict) -> int:
    execute = args.execute or args.apply
    try:
        orgs = config.orgs(cfg)
    except ValueError as e:
        log.error("%s: %s", config.config_path(), e)
        return 2
    selected = args.org or list(orgs)
    unknown = [o for o in selected if o not in orgs]
    if unknown:
        log.error("Unknown org(s) %s; defined in %s: %s", ", ".join(unknown), config.config_path(),
                  ", ".join(n for n in orgs if n) or "none ([sync] only)")
        return 2
    if args.profile and len(selected) != 1:
        log.error("--profile needs exactly one org; add --org NAME")
        return 2
    meta_flags = [f"--{k.replace('_', '-')}" for k in META_FLAGS if getattr(args, k, None) is not None]
    if meta_flags and len(selected) != 1:
        log.error("%s need exactly one org; add --org NAME", ", ".join(meta_flags))
        return 2
    if meta_flags and not orgs[selected[0]]["source_profile"]:
        log.error("%s only apply to an org with source_profile set", ", ".join(meta_flags))
        return 2
    settings = {}
    for name in selected:
        if orgs[name]["source_profile"]:
            try:
                settings[name] = meta_settings(orgs[name], args)
            except (ValueError, TypeError) as e:
                log.error("%s in %s: %s", _label(name), config.config_path(), e)
                return 2

    path = config.aws_config_path()
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if "" not in orgs and awsconfig.has_managed_block(current):
        # Its names would stay reserved and every migrated profile would get an
        # account-ID suffix.
        log.error("%s still has the unnamed managed block from a [sync]-only setup. Back up "
                  "the file, delete that block (BEGIN to END marker), make sure each org's "
                  "management profile is defined outside it, then run this again.", path)
        return 2
    errors = [(n, e) for n in selected
              if (e := _check_org(n, orgs[n], current, args.profile or orgs[n]["management_profile"]))]
    for name, err in errors:
        log.error("%s in %s: %s", _label(name), config.config_path(), err)
    if errors:
        return 2

    updated, failures = current, 0
    for name in selected:
        if name in settings:
            result = _sync_role_org(name, orgs[name], updated, settings[name], args, cfg)
        else:
            result = _sync_org(name, orgs[name], updated, args, cfg)
        if result is None:
            failures += 1  # leave this org's block as it was; other orgs still sync
        else:
            updated = result
    try:
        configparser.RawConfigParser().read_string(updated)  # botocore parses strictly
    except configparser.Error as e:
        log.error("Generated config would not parse; not writing: %s", e)
        return 1

    if updated == current:
        if not failures:
            log.info("%s already up to date", path)
        return 1 if failures else 0
    diff = difflib.unified_diff(current.splitlines(True), updated.splitlines(True),
                                fromfile=str(path), tofile=f"{path} (new)")
    sys.stdout.writelines(diff)

    if not execute:
        log.info("[DRY-RUN] Would update %s. Pass --execute or --apply to write.", path)
        return 1 if failures else 0
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(path, backup)
        log.info("[EXECUTE] Backed up %s to %s", path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, updated)
    log.info("[EXECUTE] Wrote %s", path)
    return 1 if failures else 0
