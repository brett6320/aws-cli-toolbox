"""aws-sync-profiles: build profiles for every account in each Organization.

SSO mode (default): each active account gets `<name>` (read-only role) and
`<name><admin_suffix>` (privileged role) as SSO profiles.

Assume-role mode (org sets source_profile and/or role_source_profiles):
accounts come from the org tree, short names, business domains and environments
from the account-mapping DynamoDB table (same settings as the switch-role
bookmarks generator), and each account gets one `<short name>-<role>` profile
per role, chained with role_arn from the role's entry in role_source_profiles,
else from source_profile.

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

from botocore import exceptions as botocore_exc
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
    meta = parser.add_argument_group("assume-role mode (override the org's toolbox.toml keys; need a single org)")
    meta.add_argument("--ddb-table-arn", help="Account metadata table ARN (BG_DDB_TABLE_ARN)")
    meta.add_argument("--ddb-profile", help="Profile for the table's account (BG_DDB_PROFILE)")
    meta.add_argument("--ddb-role-name", help="Role assumed in the table's account (BG_DDB_ROLE_NAME)")
    meta.add_argument("--attr-account-id", help="Table attribute: account number (BG_ATTR_ACCOUNT_ID)")
    meta.add_argument("--attr-short-name", help="Table attribute: short name (BG_ATTR_SHORT_NAME)")
    meta.add_argument("--attr-ou", help="Table attribute: business domain (BG_ATTR_OU)")
    meta.add_argument("--attr-environment", help="Table attribute: environment (BG_ATTR_ENVIRONMENT)")
    meta.add_argument("--roles", help="Comma-separated roles, one profile each (BG_DEFAULT_ROLES)")
    meta.add_argument("--include-inactive", action="store_const", const=True,
                      help="Include suspended/closed accounts (BG_INCLUDE_INACTIVE)")


# Flags that map straight onto account_meta settings.
META_FLAGS = ("ddb_table_arn", "ddb_profile", "ddb_role_name", "attr_account_id", "attr_short_name",
              "attr_ou", "attr_environment", "roles", "include_inactive")
# toolbox.toml keys with the same names; unset ones fall back to BG_* variables.
PER_ORG_TABLE_KEYS = ("ddb_table_arn", "ddb_profile")
META_TOML_KEYS = ("ddb_table_arn", "ddb_profile", "ddb_role_name", "attr_account_id", "attr_short_name",
                  "attr_ou", "attr_environment", "include_inactive")


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
    """Assume-role profiles: one per (account, role), chained from org["source_profile"]
    or, for a role listed in org["role_source_profiles"], from that profile.

    `roles` is [(role name, profile-name suffix)] from role_suffixes().
    """
    sources = org.get("role_source_profiles") or {}
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
    taken = set(reserved)
    planned: list[tuple[dict, list[tuple[str, str]]]] = []
    for acct in sorted(kept, key=lambda a: (a["shortName"].lower(), a["id"])):
        base = base_for(acct)
        names = [(f"{base}{suffix}", role) for role, suffix in roles]
        if counts[base] > 1 or any(n in taken for n, _ in names):
            log.warning("Profile name %s for %s (%s) is shared or taken; using %s-%s",
                        base, acct["shortName"], acct["id"], base, acct["id"])
            base = f"{base}-{acct['id']}"
            names = [(f"{base}{suffix}", role) for role, suffix in roles]
        kept_names = []
        for name, role in names:
            if name in taken:
                log.warning("Skipping %s: name already in use", name)
                continue
            taken.add(name)
            kept_names.append((name, role))
        planned.append((acct, kept_names))

    if org.get("account_name_aliases"):
        # Aliases come second: a short name always wins, and an alias that would
        # clash with anything is skipped rather than given an ID.
        alias_bases: dict[str, int] = {}
        for acct, _ in planned:
            alias = awsconfig.profile_name_for(acct["name"], prefix)
            alias_bases[alias] = alias_bases.get(alias, 0) + 1
        for acct, names in planned:
            alias = awsconfig.profile_name_for(acct["name"], prefix)
            if alias == prefix or alias == base_for(acct):
                continue  # no usable characters, or the short name is the account name
            aliases = [(f"{alias}{suffix}", role) for role, suffix in roles]
            if alias_bases[alias] > 1 or any(n in taken for n, _ in aliases):
                log.warning("Not adding account-name profiles %s* for %s (%s): name shared or taken",
                            alias, acct["name"], acct["id"])
                continue
            taken.update(n for n, _ in aliases)
            names.extend(aliases)

    sections = []
    expected: dict[str, set[str]] = {}
    for acct, names in planned:
        comment = (f"# {_comment_text(acct['name'])} ({acct['id']}) | domain: {_comment_text(acct['domain'])} | "
                   f"env: {acct['env'] or '-'}")
        for name, role in names:
            expected[f"profile {name}"] = {"role_arn", "source_profile", "region", "output"}
            sections.append("\n".join([
                comment,
                f"[profile {name}]",
                f"role_arn = arn:{partition}:iam::{acct['id']}:role/{role}",
                f"source_profile = {sources.get(role, org['source_profile'])}",
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
    role_sources = org["role_source_profiles"]
    if not isinstance(role_sources, dict) or not all(isinstance(v, str) and v for v in role_sources.values()):
        return 'role_source_profiles must be a table of role = "profile"'
    sources = [("source_profile", org["source_profile"])] if org["source_profile"] else []
    sources += [(f"role_source_profiles.{role}", prof) for role, prof in role_sources.items()]
    for key, source in sources:
        if source not in awsconfig.reserved_profile_names(text, name):
            return f"{key} {source} must be defined in ~/.aws/config outside this org's managed block"
        if awsconfig.profile_start_url(parser, source) != url:
            # Generated profiles chain from it, so it has to be this org's.
            return f"{key} {source} doesn't sign in at this org's portal {url}"
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


def assume_role_mode(org: dict) -> bool:
    return bool(org["source_profile"] or org["role_source_profiles"])


def meta_settings(org: dict, args: argparse.Namespace, name: str = "") -> dict:
    """account_meta settings for an assume-role org: flags > toml > environment > defaults.

    With [orgs.*] tables (`name` set), the table and its profile come only from
    the org's own keys or flags: a BG_DDB_TABLE_ARN in the shell belongs to one org.
    """
    kwargs = {k: getattr(args, k) for k in META_FLAGS if getattr(args, k, None) is not None}
    for key in META_TOML_KEYS:
        value = org[key]
        if key == "include_inactive":
            if value is not None and not isinstance(value, bool):
                raise ValueError("include_inactive must be true or false")
        elif not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        else:
            value = value.strip()
        if key in kwargs:
            continue
        if value not in (None, ""):
            kwargs[key] = value
        elif name and key in PER_ORG_TABLE_KEYS:
            kwargs[key] = ""  # not None: keeps resolve_settings from reading the environment
    roles = org["roles"]
    if isinstance(roles, str):
        roles = roles.split(",")
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise ValueError("roles must be a list of role names")
    roles = [r.strip() for r in roles if r.strip()]
    if not isinstance(org["role_source_profiles"], dict):
        raise ValueError('role_source_profiles must be a table of role = "profile"')
    if not roles and "roles" not in kwargs and not os.environ.get("BG_DEFAULT_ROLES"):
        # No roles anywhere else: the roles given a first hop are the roles.
        roles = list(org["role_source_profiles"])
    if roles and "roles" not in kwargs:
        kwargs["roles"] = roles
    if args.profile or org["management_profile"]:
        kwargs["profile"] = args.profile or org["management_profile"]
    settings = account_meta.resolve_settings(kwargs)
    if not settings["roles"]:
        raise ValueError("no roles configured (roles, BG_DEFAULT_ROLES or --roles)")
    settings["role_suffixes"] = role_suffixes(settings["roles"], org["role_suffixes"])
    if not org["source_profile"]:
        unmapped = [r for r in settings["roles"] if r not in org["role_source_profiles"]]
        if unmapped:
            raise ValueError(f"no source_profile for role(s) {', '.join(unmapped)}; "
                             "set source_profile or add them to role_source_profiles")
    if not isinstance(org["require_short_name"], bool):
        raise ValueError("require_short_name must be true or false")
    if not isinstance(org["account_name_aliases"], bool):
        raise ValueError("account_name_aliases must be true or false")
    return settings


def generated_profiles_for(text: str, org: str, account_id: str, roles: list[str]) -> list[str]:
    """This org's generated profiles for `account_id`, in `roles` order."""
    parser = awsconfig.read_text(awsconfig.managed_block(text, org))
    by_role = {}
    for section in parser.sections():
        arn = parser[section].get("role_arn", "")
        m = re.match(r"^arn:[^:]+:iam::(\d{12}):role/(.+)$", arn)
        if m and m.group(1) == account_id and section.startswith("profile "):
            by_role.setdefault(m.group(2), section[len("profile "):])
    return [by_role[r] for r in roles if r in by_role]


# Failures that mean "these credentials can't read the table (yet)". Anything
# else (wrong account, missing table, bad ARN, network) is a real error.
ACCESS_ERROR_CODES = {
    "AccessDenied", "AccessDeniedException", "UnauthorizedException", "ForbiddenException",
    "UnrecognizedClientException", "InvalidClientTokenId", "ExpiredToken", "ExpiredTokenException",
}
ACCESS_BOTOCORE_ERRORS = (
    botocore_exc.NoCredentialsError, botocore_exc.PartialCredentialsError, botocore_exc.ProfileNotFound,
    botocore_exc.SSOTokenLoadError, botocore_exc.UnauthorizedSSOTokenError, botocore_exc.TokenRetrievalError,
    botocore_exc.CredentialRetrievalError,
)


class TableError(Exception):
    """The table read failed for a reason other than missing access."""


def read_table(name: str, s: dict, org_session, text: str) -> tuple[dict, set[str]] | None:
    """Scan the metadata table, or None if no credentials have access to it.

    Tries the configured access (BG_DDB_PROFILE, the org profile, or
    BG_DDB_ROLE_NAME), then each of this org's own generated profiles for the
    table's account. That second path is how a first run without table access
    is backfilled: run 1 generates the profiles, run 2 reads the table with
    them. Raises TableError for anything that isn't an access failure.
    """
    attempts = [("configured access", s)]
    for profile in generated_profiles_for(text, name, s["ddb_account_id"], s["roles"]):
        if profile != s["ddb_profile"]:
            attempts.append((f"generated profile {profile}", {**s, "ddb_profile": profile}))
    attrs = {"account_id": s["attr_account_id"], "short_name": s["attr_short_name"],
             "ou": s["attr_ou"], "environment": s["attr_environment"]}
    for how, settings in attempts:
        try:
            session = account_meta.get_ddb_session(settings, org_session)
            meta, dupes = account_meta.fetch_metadata(
                get_client("dynamodb", session, s["ddb_region"]), s["ddb_table"], attrs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code not in ACCESS_ERROR_CODES:
                raise TableError(f"{how}: {code}: {e.response['Error']['Message']}") from e
            log.warning("%s: no access to %s with %s: %s", _label(name), s["ddb_table"], how, code)
        except ACCESS_BOTOCORE_ERRORS as e:
            log.warning("%s: no access to %s with %s: %s", _label(name), s["ddb_table"], how, e)
        except (BotoCoreError, RuntimeError) as e:
            raise TableError(f"{how}: {e}") from e
        else:
            log.info("%s: read %s with %s", _label(name), s["ddb_table"], how)
            return meta, dupes
    return None


def _sync_role_org(name: str, org: dict, text: str, settings: dict, args: argparse.Namespace,
                   cfg: dict) -> str | None:
    """Assume-role mode: org tree + metadata table -> role_arn profiles."""
    s = settings
    log.debug("%s: org profile %s, table %s", _label(name), s["profile"], s["ddb_table_arn"])
    try:
        org_session = account_meta.get_session(s["profile"], s["region"])
        tree = account_meta.fetch_org(get_client("organizations", org_session), s["include_inactive"])
        partition = org_session.get_partition_for_region(s["region"])
    except ClientError as e:
        log.error("%s: AWS error %s: %s", _label(name), e.response["Error"]["Code"],
                  e.response["Error"]["Message"])
        return None
    except (BotoCoreError, RuntimeError) as e:
        log.error("%s: %s", _label(name), e)
        return None

    if not s["ddb_table_arn"]:
        log.warning("%s: no metadata table configured (ddb_table_arn); naming profiles after "
                    "their accounts%s", _label(name),
                    " and ignoring require_short_name" if org["require_short_name"] else "")
        table = None
    else:
        try:
            table = read_table(name, s, org_session, text)
        except TableError as e:
            log.error("%s: can't read the metadata table: %s", _label(name), e)
            return None
    if table is None and not s["ddb_table_arn"]:
        meta, dupes = {}, set()
    elif table is None:
        if awsconfig.has_managed_block(text, name):
            if not args.force:
                # A temporary access problem must not rename every existing profile.
                log.error("%s: no access to the metadata table; leaving existing profiles as they "
                          "are (use --force to rebuild them with account names)", _label(name))
                return None
            log.warning("%s: --force: rebuilding existing profiles with account names", _label(name))
        log.warning("%s: can't read the metadata table; naming profiles after their accounts. "
                    "Run again once a generated profile for account %s can read it, to switch "
                    "to short names.", _label(name), s["ddb_account_id"])
        meta, dupes = {}, set()
    else:
        meta, dupes = table
    tree = {**tree, "accounts": {k: v for k, v in tree["accounts"].items() if k not in dupes}}
    accounts = account_meta.merge(tree, meta)
    if table is not None:
        matched = sum(1 for a in accounts if a["hasMetadata"])
        if accounts and not matched and not args.force:
            # Wrong table/BG_ATTR_* names would otherwise rename every profile.
            log.error("%s: no account matched a row in %s; check BG_DDB_TABLE_ARN and BG_ATTR_* "
                      "(use --force to generate names from account names)", _label(name), s["ddb_table"])
            return None
        if org["require_short_name"]:
            # The table's own account always stays: its profiles are how later
            # runs read the table if the configured access fails.
            named = [a for a in accounts if a["id"] == s["ddb_account_id"]
                     or str(meta.get(a["id"], {}).get("shortName") or "").strip()]
            log.info("%s: %d of %d accounts have a short name in the table; skipping the rest",
                     _label(name), len(named), len(accounts))
            accounts = named
    if s["ddb_account_id"] and (s["ddb_account_id"] not in {a["id"] for a in accounts} or
                                s["ddb_account_id"] in {str(x) for x in org["exclude_accounts"]}):
        log.warning("%s: no profiles for the table's account %s (excluded, inactive, duplicate rows "
                    "or not in this org); later runs can't fall back to them to read the table",
                    _label(name), s["ddb_account_id"])
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
    log.info("%s: SSO mode (no source_profile or role_source_profiles): sso_role_name %s / %s",
             _label(name), org["readonly_role"], org["admin_role"])
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
    if meta_flags and not assume_role_mode(orgs[selected[0]]):
        log.error("%s only apply to an org with source_profile or role_source_profiles set", ", ".join(meta_flags))
        return 2
    settings = {}
    for name in selected:
        if assume_role_mode(orgs[name]):
            try:
                settings[name] = meta_settings(orgs[name], args, name)
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
