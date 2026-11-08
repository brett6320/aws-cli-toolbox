"""ns-cert-refresh: build a CA bundle including Netskope tenant certificates.

Netskope intercepts TLS with a tenant CA. Python (via truststore) trusts it
through the OS store, but the AWS CLI uses its own bundle, so it needs
AWS_CA_BUNDLE / ca_bundle pointing at a bundle that includes the tenant CA.
Dry-run by default.
"""

from __future__ import annotations

import argparse
import logging
import re
import shlex
import ssl
import sys
from pathlib import Path

from botocore import httpsession

from aws_toolbox import config

log = logging.getLogger(__name__)

AGENT_DIRS = {
    "darwin": [Path("/Library/Application Support/Netskope/STAgent/data")],
    "linux": [Path("/opt/netskope/stagent/data"), Path("/opt/netskope/stagent")],
}
CERT_NAMES = ("nscacert.pem", "nstenantcert.pem")
SYSTEM_BUNDLES = [
    Path("/etc/ssl/certs/ca-certificates.crt"),  # Debian/Ubuntu
    Path("/etc/pki/tls/certs/ca-bundle.crt"),    # RHEL/Fedora
    Path("/etc/ssl/cert.pem"),                   # macOS/Alpine
]
PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execute", action="store_true", help="Write the bundle (default: dry-run)")
    parser.add_argument("--apply", action="store_true", help="Alias for --execute")
    parser.add_argument("--output", help="Override [netskope] bundle_path")


def find_netskope_certs(extra: list[str], platform: str = sys.platform) -> list[Path]:
    found = []
    for entry in [Path(p).expanduser() for p in extra] + AGENT_DIRS.get(platform, []):
        if entry.is_file():
            found.append(entry)
        elif entry.is_dir():
            found += [entry / n for n in CERT_NAMES if (entry / n).is_file()]
    return list(dict.fromkeys(found))


def base_bundle() -> Path:
    for path in SYSTEM_BUNDLES:
        if path.is_file():
            return path
    return Path(httpsession.get_cert_path(True))


def pem_certs(text: str) -> list[str]:
    """Extract PEM certs, dropping any that ssl can't parse."""
    certs = []
    for pem in PEM_RE.findall(text):
        try:
            ssl.create_default_context().load_verify_locations(cadata=pem)
        except ssl.SSLError as e:
            log.warning("Skipping unparseable certificate: %s", e)
            continue
        certs.append(pem)
    return certs


def build_bundle(netskope: list[Path], base: Path | None) -> str:
    parts, seen = [], set()
    sources = list(netskope) + ([base] if base else [])
    for src in sources:
        try:
            certs = pem_certs(src.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            log.warning("Cannot read %s: %s", src, e)
            continue
        log.info("%d certificate(s) from %s", len(certs), src)
        for pem in certs:
            if pem not in seen:
                seen.add(pem)
                parts.append(pem)
    return "\n".join(parts) + "\n"


def run(args: argparse.Namespace, cfg: dict) -> int:
    execute = args.execute or args.apply
    ns = cfg["netskope"]
    certs = find_netskope_certs(ns["search_paths"])
    if not certs:
        log.error("No Netskope certificates found (is the agent installed?). Add paths to [netskope] search_paths.")
        return 1
    base = base_bundle() if ns["include_base_bundle"] else None
    bundle = build_bundle(certs, base)
    out = Path(args.output or ns["bundle_path"]).expanduser()

    if bundle == "\n":
        log.error("No readable certificates; not writing a bundle")
        return 1
    if out.exists() and out.read_text(encoding="utf-8") == bundle:
        log.info("%s already up to date", out)
    elif not execute:
        log.info("[DRY-RUN] Would write %d bytes to %s. Pass --execute or --apply to write.", len(bundle), out)
        return 0
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(bundle, encoding="utf-8")
        tmp.replace(out)
        log.info("[EXECUTE] Wrote %s", out)
    print(f"export AWS_CA_BUNDLE={shlex.quote(str(out))}   # or set ca_bundle = {out} in ~/.aws/config")
    return 0
