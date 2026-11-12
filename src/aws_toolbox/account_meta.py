"""Account list and metadata, shared with the switch-role bookmarks generator.

Ported from its aws_bookmarks.py (sha256 0b78c68a3789…),
which isn't an installable package. Keep the two in step: same BG_* keys, same
defaults, same normalization. Deliberate differences, all marked "differs:":
  - no .env file: settings come from toolbox.toml (passed in as kwargs), then
    the environment, so each org can have its own table in one run;
  - resolve_settings takes only the settings sync uses, and the table is
    optional (no ddb_table_arn: the ddb_* values are None);
  - fetch_metadata reports account IDs with more than one row so the caller
    can skip them (the generator uses the last row);
  - clients come from aws_toolbox.session (3 adaptive retries, not 8), and the
    assumed-role session name is "aws-cli-toolbox".
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, Mapping

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from aws_toolbox.session import get_client

log = logging.getLogger(__name__)

ENVIRONMENTS = ("Lab", "Sbx", "Dev", "Tst", "Prd", "Bil")
ENV_ALIASES = {
    "lab": "Lab",
    "sbx": "Sbx",
    "sandbox": "Sbx",
    "dev": "Dev",
    "development": "Dev",
    "tst": "Tst",
    "test": "Tst",
    "testing": "Tst",
    "prd": "Prd",
    "prod": "Prd",
    "production": "Prd",
    "bil": "Bil",
    "billing": "Bil",
}
LEGACY_DOMAIN = "Legacy"
INACTIVE_STATES = {"SUSPENDED", "PENDING_CLOSURE", "CLOSED"}

TABLE_ARN_RE = re.compile(
    r"^arn:(?P<partition>[^:]+):dynamodb:(?P<region>[^:]+):(?P<account>\d{12}):table/(?P<table>[^/]+)$"
)

# setting -> (env var, default); the subset of the generator's SETTINGS that sync uses
SETTINGS = {
    "profile": ("AWS_PROFILE", None),
    "region": ("AWS_REGION", "us-east-1"),
    "ddb_table_arn": ("BG_DDB_TABLE_ARN", None),
    "ddb_profile": ("BG_DDB_PROFILE", None),
    "ddb_role_name": ("BG_DDB_ROLE_NAME", "OrganizationAccountAccessRole"),
    "attr_account_id": ("BG_ATTR_ACCOUNT_ID", "accountNumber"),
    "attr_short_name": ("BG_ATTR_SHORT_NAME", "accountShortCode"),
    "attr_ou": ("BG_ATTR_OU", "ou"),
    "attr_environment": ("BG_ATTR_ENVIRONMENT", "environment"),
    "roles": ("BG_DEFAULT_ROLES", "ReadOnly,Administrator"),
    "include_inactive": ("BG_INCLUDE_INACTIVE", "false"),
}


def get_session(profile: str | None, region: str) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


def resolve_settings(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """kwargs > environment > defaults.

    differs: no .env file; the caller passes toolbox.toml values in kwargs.
    """
    unknown = set(kwargs) - set(SETTINGS)
    if unknown:
        raise TypeError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    # settings were prefixed BM_ before; they're ignored now, so say so
    legacy = sorted(k for k in os.environ
                    if k.startswith("BM_") and "BG_" + k[3:] in {v for v, _ in SETTINGS.values()})
    if legacy:
        log.warning("Ignoring %s: settings are now prefixed BG_ (rename to %s)",
                    ", ".join(legacy), ", ".join("BG_" + k[3:] for k in legacy))
    cfg: dict[str, Any] = {}
    for key, (env_var, default) in SETTINGS.items():
        value = kwargs.get(key)
        if value is None:
            value = os.environ.get(env_var) or default
        cfg[key] = value
    if isinstance(cfg["roles"], str):
        cfg["roles"] = [r.strip() for r in cfg["roles"].split(",") if r.strip()]
    if isinstance(cfg["include_inactive"], str):
        cfg["include_inactive"] = cfg["include_inactive"].lower() in ("1", "true", "yes")
    if not cfg["ddb_table_arn"]:
        # differs: the table is optional; without one, sync names profiles after accounts.
        cfg["ddb_table"] = cfg["ddb_account_id"] = cfg["ddb_region"] = None
        return cfg
    m = TABLE_ARN_RE.match(cfg["ddb_table_arn"].strip())
    if not m:
        raise ValueError(f"Not a DynamoDB table ARN: {cfg['ddb_table_arn']}")
    cfg["ddb_table"], cfg["ddb_account_id"], cfg["ddb_region"] = m["table"], m["account"], m["region"]
    return cfg


def normalize_account_id(value: Any) -> str:
    """DDB may store IDs as numbers, dropping leading zeros."""
    text = str(value).strip()
    if text.endswith(".0"):  # Decimal/float rendering
        text = text[:-2]
    return text.zfill(12)


def normalize_env(value: Any) -> str | None:
    if value is None:
        return None
    return ENV_ALIASES.get(str(value).strip().lower())


def _paginate(client, op: str, key: str, **params) -> Iterable[dict]:
    for page in client.get_paginator(op).paginate(**params):
        yield from page.get(key, [])


def fetch_org(org, include_inactive: bool = False) -> dict[str, Any]:
    """Walk the org tree. Returns {"tree": node, "accounts": {id: info}}.

    node = {"id", "name", "type": "ROOT"|"OU", "children": [node], "accounts": [id]};
    the root node also has "orgId" (o-...), or None if it can't be read.
    """
    accounts: dict[str, dict[str, Any]] = {}
    skipped = 0

    def walk(parent_id: str, name: str, node_type: str, path: list[str]) -> dict[str, Any]:
        nonlocal skipped
        node = {"id": parent_id, "name": name, "type": node_type, "children": [], "accounts": []}
        for acct in _paginate(org, "list_accounts_for_parent", "Accounts", ParentId=parent_id):
            state = acct.get("State") or acct.get("Status")
            if state in INACTIVE_STATES and not include_inactive:
                skipped += 1
                continue
            accounts[acct["Id"]] = {
                "id": acct["Id"],
                "name": acct.get("Name", acct["Id"]),
                "email": acct.get("Email", ""),
                "state": state,
                "orgPath": path,
            }
            node["accounts"].append(acct["Id"])
        ous = list(_paginate(org, "list_organizational_units_for_parent", "OrganizationalUnits", ParentId=parent_id))
        for ou in sorted(ous, key=lambda o: o["Name"].lower()):
            log.debug("Walking OU %s (%s)", ou["Name"], ou["Id"])
            node["children"].append(walk(ou["Id"], ou["Name"], "OU", path + [ou["Name"]]))
        return node

    roots = list(_paginate(org, "list_roots", "Roots"))
    if not roots:
        raise RuntimeError("No organization root found")
    root = roots[0]
    log.info("Reading org structure from root %s", root["Id"])
    tree = walk(root["Id"], root.get("Name", "Root"), "ROOT", [])
    # the root ARN embeds the org ID (arn:aws:organizations::<mgmt>:root/o-xxxx/r-xxxx), so no
    # extra permission is needed; DescribeOrganization is only a fallback
    m = re.search(r":root/(o-[a-z0-9]+)/", root.get("Arn", ""))
    tree["orgId"] = m.group(1) if m else None
    if not tree["orgId"]:
        try:
            tree["orgId"] = org.describe_organization()["Organization"]["Id"]
        except ClientError as e:
            log.warning("Can't read organization ID (%s); omitting it", e.response["Error"]["Code"])
    log.info("Found %d accounts (%d inactive skipped)", len(accounts), skipped)
    return {"tree": tree, "accounts": accounts}


def _caller_account(session: boto3.Session) -> str:
    return get_client("sts", session).get_caller_identity()["Account"]


def get_ddb_session(cfg: Mapping[str, Any], org_session: boto3.Session) -> boto3.Session:
    """Session in the table's account: ddb_profile, else the org session (same account) or an
    assumed ddb_role_name. Refuses credentials for any other account."""
    account = cfg["ddb_account_id"]
    if cfg["ddb_profile"]:
        log.debug("DDB via profile %s", cfg["ddb_profile"])
        session = get_session(cfg["ddb_profile"], cfg["ddb_region"])
    elif _caller_account(org_session) == account:
        log.debug("DDB via the Organizations session")
        session = org_session
    else:
        partition = org_session.get_partition_for_region(cfg["ddb_region"])
        role_arn = f"arn:{partition}:iam::{account}:role/{cfg['ddb_role_name']}"
        log.debug("DDB via assume-role %s", role_arn)
        creds = get_client("sts", org_session).assume_role(
            RoleArn=role_arn, RoleSessionName="aws-cli-toolbox"
        )["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=cfg["ddb_region"],
        )
    actual = _caller_account(session)
    if actual != account:
        raise RuntimeError(f"DynamoDB credentials are for account {actual}, but the table is in {account}; "
                           "fix --ddb-profile / BG_DDB_PROFILE")
    return session


def fetch_metadata(ddb, table: str, attrs: Mapping[str, str]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Scan the table (read-only) -> ({account_id: {shortName, ou, envRaw}}, duplicated IDs).

    differs: an account ID on more than one row is left out of the mapping and
    returned in the second value, instead of keeping the last row.
    """
    names = {f"#a{i}": a for i, a in enumerate(attrs.values())}
    deser = TypeDeserializer()
    out: dict[str, dict[str, Any]] = {}
    dupes: set[str] = set()
    log.info("Scanning DynamoDB table %s", table)
    for item in _paginate(
        ddb,
        "scan",
        "Items",
        TableName=table,
        ProjectionExpression=", ".join(names),
        ExpressionAttributeNames=names,
    ):
        row = {k: deser.deserialize(v) for k, v in item.items()}
        acct_id = row.get(attrs["account_id"])
        if acct_id in (None, ""):
            continue
        acct_id = normalize_account_id(acct_id)
        if acct_id in out or acct_id in dupes:
            dupes.add(acct_id)
            out.pop(acct_id, None)
            continue
        out[acct_id] = {
            "shortName": row.get(attrs["short_name"]),
            "ou": row.get(attrs["ou"]),
            "envRaw": row.get(attrs["environment"]),
        }
    for acct_id in sorted(dupes):
        log.warning("Account %s appears in more than one table row; ignoring its rows", acct_id)
    log.info("Loaded metadata for %d accounts", len(out))
    return out, dupes


def _blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def merge(org: Mapping[str, Any], meta: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Org accounts enriched with shortName, domain and env, sorted by shortName."""
    merged = []
    for acct_id, info in org["accounts"].items():
        m = meta.get(acct_id, {})
        ou = (str(m["ou"]).strip() if m.get("ou") not in (None, "") else "")
        env_raw = m.get("envRaw")
        env = normalize_env(env_raw)
        if env_raw not in (None, "") and env is None:
            log.warning("Account %s: unrecognised environment %r", acct_id, env_raw)
        merged.append(
            {
                **info,
                # short codes are lowercase by convention; the account-name fallback keeps its case
                "shortName": str(m["shortName"]).strip().lower() if not _blank(m.get("shortName")) else info["name"],
                "domain": ou or LEGACY_DOMAIN,
                "env": env,
                "envRaw": None if env_raw is None else str(env_raw),
                "hasMetadata": bool(m),
            }
        )
    orphans = set(meta) - set(org["accounts"])
    if orphans:
        log.warning("%d DDB entries match no active org account: %s", len(orphans), ", ".join(sorted(orphans)[:10]))
    missing = sum(1 for a in merged if not a["hasMetadata"])
    if missing:
        log.warning("%d org accounts have no DDB metadata (shown as %s)", missing, LEGACY_DOMAIN)
    return sorted(merged, key=lambda a: a["shortName"].lower())
