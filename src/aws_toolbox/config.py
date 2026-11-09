"""Toolbox configuration (~/.aws/toolbox.toml)."""

from __future__ import annotations

import copy
import logging
import os
import re
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict[str, dict[str, Any]] = {
    "defaults": {"region": "us-east-1", "output": "json"},
    "sso": {"main_profile": "main-org", "main_org": "", "min_remaining_minutes": 30},
    "assume_role": {"session_name": "", "duration_seconds": 3600},
    "sync": {
        "management_profile": "main-org",
        "sso_session": "",
        "sso_start_url": "",
        "sso_region": "us-east-1",
        "readonly_role": "ReadOnlyAccess",
        "admin_role": "AdministratorAccess",
        "admin_suffix": "-adm",
        "profile_prefix": "",
        "exclude_accounts": [],
        # Assume-role mode (set source_profile and/or role_source_profiles):
        # accounts come from the org tree,
        # short names/domains/environments from the account-mapping table at
        # ddb_table_arn, and one role_arn profile per role, chained from the
        # role's role_source_profiles entry, else source_profile. roles = [] means BG_DEFAULT_ROLES, else the role_source_profiles roles. role_suffixes maps a
        # role to its profile-name suffix ("" = none); unmapped roles get
        # "-<role name>".
        "source_profile": "",
        # Table settings. Empty (or include_inactive unset) falls back to the
        # matching BG_* environment variable, then the built-in default.
        "ddb_table_arn": "",
        "ddb_profile": "",
        "ddb_role_name": "",
        "attr_account_id": "",
        "attr_short_name": "",
        "attr_ou": "",
        "attr_environment": "",
        "include_inactive": None,
        "roles": [],
        "role_suffixes": {},
        # true: only accounts with a short name in the table get profiles
        # (no fallback to the account name).
        "require_short_name": False,
        # true: accounts with a short name also get profiles named after the
        # account (e.g. acme-ops and acme-operations). Short names win clashes.
        "account_name_aliases": False,
        # Per-role source profile, e.g. { "Admin" = "acme-billing-adm" }; roles
        # not listed chain from source_profile, which may be left empty when
        # every role is listed here. Per org, never inherited.
        "role_source_profiles": {},
    },
    "netskope": {
        "bundle_path": "~/.aws/ca-bundle.pem",
        "search_paths": [],
        "include_base_bundle": True,
    },
    "orgs": {},
}

ORG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# [sync] keys every [orgs.<name>] inherits. Identity keys (portal, management
# profile, prefix, exclusions) are never inherited, so a missing per-org value
# can't point one org's profiles at another org's portal.
INHERITED_SYNC_KEYS = ("readonly_role", "admin_role", "admin_suffix", "roles", "role_suffixes",
                       "require_short_name", "account_name_aliases", "ddb_role_name", "attr_account_id", "attr_short_name",
                       "attr_ou", "attr_environment", "include_inactive")


def aws_dir() -> Path:
    return Path(os.environ.get("AWS_TOOLBOX_AWS_DIR", "~/.aws")).expanduser()


def config_path() -> Path:
    override = os.environ.get("AWS_TOOLBOX_CONFIG")
    return Path(override).expanduser() if override else aws_dir() / "toolbox.toml"


def aws_config_path() -> Path:
    override = os.environ.get("AWS_CONFIG_FILE")
    return Path(override).expanduser() if override else aws_dir() / "config"


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load toolbox config merged over DEFAULTS. Missing file -> defaults."""
    path = path or config_path()
    cfg = copy.deepcopy(DEFAULTS)
    if not path.exists():
        log.debug("No toolbox config at %s; using defaults", path)
        return cfg
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    for section, values in data.items():
        if not isinstance(values, dict):
            raise ValueError(f"{path}: top-level key {section!r} must be a table")
        cfg.setdefault(section, {}).update(values)
    log.debug("Loaded toolbox config from %s", path)
    return cfg


def orgs(cfg: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Sync settings per organization, keyed by org name.

    Without any [orgs.<name>] tables, [sync] is a single unnamed org ("").
    """
    defined = cfg.get("orgs") or {}
    tables = {"sync": cfg["sync"], **{f"orgs.{n}": v for n, v in defined.items() if isinstance(v, dict)}}
    for where, values in tables.items():
        if "env_file" in values:
            raise ValueError(f"[{where}]: env_file was removed; set ddb_table_arn (and any other BG_* "
                             "values, e.g. ddb_profile) in toolbox.toml instead")
    if not defined:
        return {"": dict(cfg["sync"])}
    # Per-org keys that would otherwise be silently ignored here; the last two
    # also decide the mode, so missing them quietly yields SSO profiles.
    for key in ("ddb_table_arn", "ddb_profile", "source_profile", "role_source_profiles"):
        if cfg["sync"].get(key):
            raise ValueError(f"[sync] {key} isn't used once [orgs.*] tables exist; "
                             f"set it in each [orgs.<name>] instead")
    out = {}
    for name, values in defined.items():
        if not ORG_NAME_RE.match(name):
            raise ValueError(f"[orgs.{name}]: org names may use letters, digits, '.', '_' and '-'")
        if not isinstance(values, dict):
            raise ValueError(f"[orgs.{name}] must be a table")
        unknown = sorted(set(values) - set(DEFAULTS["sync"]))
        if unknown:
            raise ValueError(f"[orgs.{name}]: unknown keys {', '.join(unknown)}")
        if not values.get("management_profile"):
            raise ValueError(f"[orgs.{name}]: management_profile is required")
        if bool(values.get("sso_session")) == bool(values.get("sso_start_url")):
            raise ValueError(f"[orgs.{name}]: set exactly one of sso_session or sso_start_url")
        if values.get("sso_start_url") and not values.get("sso_region"):
            raise ValueError(f"[orgs.{name}]: sso_start_url needs sso_region")
        merged = copy.deepcopy(DEFAULTS["sync"])
        merged.update({k: cfg["sync"][k] for k in INHERITED_SYNC_KEYS})
        merged.update(values)
        out[name] = merged
    # Two orgs on one portal or management profile would list the same accounts twice.
    for key in ("sso_session", "sso_start_url", "management_profile"):
        seen: dict[str, str] = {}
        for name, org in out.items():
            value = str(org[key]).rstrip("/")
            if value and value in seen:
                raise ValueError(f"[orgs.{seen[value]}] and [orgs.{name}] share {key} {value!r}")
            seen.setdefault(value, name)
    return out


def example_text() -> str:
    return resources.files("aws_toolbox").joinpath("toolbox.example.toml").read_text()
