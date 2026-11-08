"""Shared boto3 session/client helpers. Truststore is injected by the package __init__."""

from __future__ import annotations

import boto3
from botocore.config import Config

_CLIENT_CONFIG = Config(retries={"max_attempts": 3, "mode": "adaptive"})


def get_session(profile: str | None = None, default_region: str | None = None) -> boto3.Session:
    """Session for `profile`; `default_region` applies only if the profile/env has none."""
    session = boto3.Session(profile_name=profile)
    if session.region_name is None and default_region:
        session = boto3.Session(profile_name=profile, region_name=default_region)
    return session


def get_client(service: str, session: boto3.Session, region: str | None = None):
    return session.client(service, region_name=region or session.region_name, config=_CLIENT_CONFIG)
