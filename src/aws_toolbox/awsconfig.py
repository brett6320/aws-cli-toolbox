"""Read ~/.aws/config and maintain a toolbox-managed block inside it."""

from __future__ import annotations

import configparser
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BEGIN_MARKER = "# BEGIN aws-toolbox managed profiles (do not edit; regenerate with aws-sync-profiles)"
END_MARKER = "# END aws-toolbox managed profiles"


@dataclass
class SsoProfile:
    name: str
    start_url: str  # normalized (no trailing slash), for comparing profiles
    region: str
    session: str | None  # sso-session name, if the profile uses one
    account_id: str | None = None
    role_name: str | None = None
    raw_start_url: str | None = None  # exactly as written in the config

    @property
    def cache_key(self) -> str:
        """Key the AWS CLI hashes to name the SSO token cache file."""
        return self.session or self.raw_start_url or self.start_url


def read(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    if path.exists():
        parser.read(path, encoding="utf-8")
    return parser


def read_text(text: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    parser.read_string(text)
    return parser


def _profile_name(section: str) -> str | None:
    if section == "default":
        return "default"
    if section.startswith("profile "):
        return section[len("profile "):].strip()
    return None


def profile_names(parser: configparser.RawConfigParser) -> set[str]:
    return {n for s in parser.sections() if (n := _profile_name(s))}


def sso_profiles(parser: configparser.RawConfigParser) -> list[SsoProfile]:
    """All profiles with SSO settings, either direct or via an [sso-session]."""
    out = []
    for section in parser.sections():
        name = _profile_name(section)
        if name is None:
            continue
        opts = parser[section]
        session = opts.get("sso_session")
        if session:
            sess_section = f"sso-session {session}"
            if not parser.has_section(sess_section):
                continue
            start_url = parser[sess_section].get("sso_start_url")
            region = parser[sess_section].get("sso_region", "")
        else:
            start_url = opts.get("sso_start_url")
            region = opts.get("sso_region", "")
        if not start_url:
            continue
        out.append(SsoProfile(
            name=name,
            start_url=start_url.rstrip("/"),
            raw_start_url=start_url,
            region=region,
            session=session,
            account_id=opts.get("sso_account_id"),
            role_name=opts.get("sso_role_name"),
        ))
    return out


def sso_session_target(parser: configparser.RawConfigParser, session: str) -> SsoProfile | None:
    """An [sso-session]'s start URL and region, as an SsoProfile named after the session."""
    section = f"sso-session {session}"
    if not parser.has_section(section) or not parser[section].get("sso_start_url"):
        return None
    start_url = parser[section]["sso_start_url"]
    return SsoProfile(name=session, start_url=start_url.rstrip("/"), raw_start_url=start_url,
                      region=parser[section].get("sso_region", ""), session=session)


def profile_start_url(parser: configparser.RawConfigParser, name: str, _depth: int = 0) -> str | None:
    """Normalized start URL a profile ultimately signs in at, following source_profile.

    None when it doesn't end at SSO (static keys, credential_process, ...).
    """
    section = "default" if name == "default" else f"profile {name}"
    if _depth > 10 or not parser.has_section(section):
        return None
    opts = parser[section]
    if opts.get("sso_session"):
        target = sso_session_target(parser, opts["sso_session"])
        return target.start_url if target else None
    if opts.get("sso_start_url"):
        return opts["sso_start_url"].rstrip("/")
    if opts.get("source_profile"):
        return profile_start_url(parser, opts["source_profile"], _depth + 1)
    return None


def token_expiry(profile: SsoProfile, cache_dir: Path) -> datetime | None:
    """Expiry of the cached SSO access token, or None if absent/unreadable."""
    digest = hashlib.sha1(profile.cache_key.encode("utf-8")).hexdigest()
    path = cache_dir / f"{digest}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data["expiresAt"]
    except (OSError, ValueError, KeyError):
        return None
    # CLI writes e.g. 2026-01-01T00:00:00Z or with an offset/UTC suffix
    raw = raw.replace("UTC", "+00:00").replace("Z", "+00:00")
    try:
        expiry = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)


def profile_name_for(account_name: str, prefix: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", account_name.lower()).strip("-")
    if prefix and slug.startswith(prefix.lower()):
        slug = slug[len(prefix):]  # "acme-prod" with prefix "acme-" stays "acme-prod"
    return f"{prefix}{slug}"


def markers(org: str = "") -> tuple[str, str]:
    """BEGIN/END marker lines for an org's managed block ("" = the unnamed block)."""
    if not org:
        return BEGIN_MARKER, END_MARKER
    return (f"# BEGIN aws-toolbox managed profiles: {org} (do not edit; regenerate with aws-sync-profiles)",
            f"# END aws-toolbox managed profiles: {org}")


def _find_block(lines: list[str], org: str) -> tuple[int, int] | None:
    # Whole-line matches: the unnamed END marker is a prefix of every org's.
    begin, end = markers(org)
    starts = [i for i, line in enumerate(lines) if line.rstrip() == begin]
    ends = [i for i, line in enumerate(lines) if line.rstrip() == end]
    if len(starts) > 1 or len(ends) > 1:
        raise ValueError("Multiple managed blocks found; fix ~/.aws/config by hand")
    if not starts and not ends:
        return None
    if not (starts and ends) or ends[0] < starts[0]:
        raise ValueError("Managed block markers are unbalanced; fix ~/.aws/config by hand")
    return starts[0], ends[0]


def managed_block(text: str, org: str = "") -> str:
    """The org's managed block contents (between the markers), or "" if there is none."""
    lines = text.splitlines(keepends=True)
    span = _find_block(lines, org)
    return "".join(lines[span[0] + 1:span[1]]) if span else ""


def has_managed_block(text: str, org: str = "") -> bool:
    return _find_block(text.splitlines(keepends=True), org) is not None


def replace_managed_block(text: str, block: str, org: str = "") -> str:
    """Return config text with the org's managed block replaced (or appended)."""
    begin, end = markers(org)
    managed = f"{begin}\n{block.rstrip()}\n{end}\n"
    lines = text.splitlines(keepends=True)
    span = _find_block(lines, org)
    if span:
        tail = "".join(lines[span[1] + 1:]).lstrip("\n")
        return "".join(lines[:span[0]]) + managed + (("\n" + tail) if tail else "")
    if not text or text.endswith("\n\n"):
        sep = ""
    else:
        sep = "\n" if text.endswith("\n") else "\n\n"
    return text + sep + managed


def reserved_profile_names(text: str, org: str = "") -> set[str]:
    """Profile names defined outside the org's managed block (incl. other orgs' blocks)."""
    lines = text.splitlines(keepends=True)
    span = _find_block(lines, org)
    if span:
        lines = lines[:span[0]] + lines[span[1] + 1:]
    return profile_names(read_text("".join(lines)))
