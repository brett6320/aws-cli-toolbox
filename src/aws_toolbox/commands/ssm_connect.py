"""aws-ssm-connect: open a Session Manager shell on an instance.

The account comes from the profile. With no profile, pick one from
~/.aws/config; with no instance, pick one of the profile's online
SSM-managed instances. An instance is an ID (i-..., mi-...) or a Name tag.
Picking uses fzf when it is installed, else a numbered menu.

The session runs `aws ssm start-session`, which needs the Session Manager
plugin (session-manager-plugin) on PATH.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError, SSOError, TokenRetrievalError

from aws_toolbox import awsconfig, config
from aws_toolbox.session import get_client, get_session

log = logging.getLogger(__name__)

INSTANCE_ID_RE = re.compile(r"^(i|mi)-[0-9a-f]{8,17}$")
EC2_FILTER_CHUNK = 200


@dataclass
class Instance:
    id: str
    name: str  # Name tag, else the agent's ComputerName, else ""
    platform: str
    ip: str

    def label(self) -> str:
        name = " ".join((self.name or "-").split())  # no tabs/newlines: fzf output is parsed by line and tab
        return f"{self.id:20} {name:40} {self.platform or '-':10} {self.ip or '-'}"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("profile", nargs="?", help="Profile to connect with (default: pick one)")
    parser.add_argument("instance", nargs="?", help="Instance ID or Name tag (default: pick one)")
    parser.add_argument("--region", help="Region (default: AWS_REGION/AWS_DEFAULT_REGION, else the profile's, "
                                         "else [defaults] region)")
    parser.add_argument("--list", action="store_true", help="List the profile's online instances, then exit")


def choose(items: list[str], prompt: str) -> int | None:
    """Index of the item the user picks, or None if they cancel."""
    if not items:
        return None
    if len(items) == 1:
        return 0
    if not sys.stdin.isatty():
        log.error("Can't prompt for %s: stdin is not a terminal", prompt)
        return None
    if shutil.which("fzf"):
        numbered = "\n".join(f"{i}\t{item}" for i, item in enumerate(items))
        result = subprocess.run(
            ["fzf", "--height=40%", "--reverse", f"--prompt={prompt}> ", "--with-nth=2..", "--delimiter=\t"],
            input=numbered, stdout=subprocess.PIPE, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return int(result.stdout.split("\t", 1)[0])
    for i, item in enumerate(items, 1):
        print(f"{i:3}) {item}", file=sys.stderr)
    while True:
        # Prompt on stderr: stdout carries only results.
        print(f"{prompt} [1-{len(items)}]: ", end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip()
        except EOFError:
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            return int(answer) - 1
        print(f"Enter a number from 1 to {len(items)}, or nothing to cancel", file=sys.stderr)


def list_instances(session, region: str) -> list[Instance]:
    """Online SSM-managed instances, with EC2 Name tags when ec2 allows it."""
    ssm = get_client("ssm", session, region)
    infos = []
    for page in ssm.get_paginator("describe_instance_information").paginate(
            Filters=[{"Key": "PingStatus", "Values": ["Online"]}]):
        infos.extend(page["InstanceInformationList"])
    names = {}
    ec2_ids = [i["InstanceId"] for i in infos if i["InstanceId"].startswith("i-")]
    ec2 = get_client("ec2", session, region) if ec2_ids else None
    # A filter, unlike InstanceIds, doesn't fail the whole call on one
    # unknown ID (an instance SSM still lists just after termination).
    for start in range(0, len(ec2_ids), EC2_FILTER_CHUNK):
        chunk = ec2_ids[start:start + EC2_FILTER_CHUNK]
        try:
            for page in ec2.get_paginator("describe_instances").paginate(
                    Filters=[{"Name": "instance-id", "Values": chunk}]):
                for res in page["Reservations"]:
                    for inst in res["Instances"]:
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                        if tags.get("Name"):
                            names[inst["InstanceId"]] = tags["Name"]
        except (BotoCoreError, ClientError) as e:
            log.warning("Can't read EC2 Name tags (%s); showing agent computer names", e)
            break
    out = [Instance(id=i["InstanceId"], name=names.get(i["InstanceId"]) or i.get("ComputerName", ""),
                    platform=i.get("PlatformName", ""), ip=i.get("IPAddress", "")) for i in infos]
    return sorted(out, key=lambda i: (i.name.lower(), i.id))


def resolve_instance(wanted: str | None, instances: list[Instance], where: str = "") -> Instance | None:
    """Instance for `wanted` (ID or exact name), picking among matches."""
    if wanted:
        candidates = [i for i in instances if wanted in (i.id, i.name)]
        if not candidates:
            log.error("No online SSM-managed instance with ID or name %r%s", wanted, where)
            return None
    else:
        candidates = instances
        if not candidates:
            log.error("No online SSM-managed instances%s", where)
            return None
    picked = choose([i.label() for i in candidates], "instance")
    return None if picked is None else candidates[picked]


def run(args: argparse.Namespace, cfg: dict) -> int:
    if args.profile and not args.instance and INSTANCE_ID_RE.match(args.profile):
        args.instance, args.profile = args.profile, None  # `aws-ssm-connect i-...`: pick the profile
    profile = args.profile
    if not profile:
        names = sorted(awsconfig.profile_names(awsconfig.read(config.aws_config_path())))
        if not names:
            log.error("No profiles in %s", config.aws_config_path())
            return 1
        picked = choose(names, "profile")
        if picked is None:
            return 1
        profile = names[picked]

    try:
        session = get_session(profile, cfg["defaults"]["region"])
    except BotoCoreError as e:
        log.error("%s", e)
        return 1
    region = args.region or session.region_name
    if not region:
        log.error("No region for %s: pass --region, or set region on the profile or in [defaults]", profile)
        return 2

    # A known ID needs no lookup (and no ssm:DescribeInstanceInformation).
    if args.instance and INSTANCE_ID_RE.match(args.instance) and not args.list:
        target, label = args.instance, args.instance
    else:
        try:
            instances = list_instances(session, region)
        except (BotoCoreError, ClientError) as e:
            log.error("Can't list instances with %s in %s: %s", profile, region, e)
            if isinstance(e, (SSOError, TokenRetrievalError)):
                log.error("Try aws-sso-refresh, then run this again")
            return 1
        if args.list:
            for i in instances:
                print(i.label())
            return 0
        picked = resolve_instance(args.instance, instances, f" for {profile} in {region}")
        if picked is None:
            return 1
        target, label = picked.id, f"{picked.id} ({picked.name})" if picked.name else picked.id

    aws = shutil.which("aws")
    if aws is None:
        log.error("AWS CLI v2 not found on PATH")
        return 1
    if shutil.which("session-manager-plugin") is None:
        log.error("session-manager-plugin not found on PATH; install the Session Manager plugin: "
                  "https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html")
        return 1
    cmd = [aws, "ssm", "start-session", "--target", target, "--profile", profile, "--region", region]
    log.info("Connecting to %s with %s in %s", label, profile, region)
    sys.stdout.flush()
    sys.stderr.flush()
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # Python ignores it; don't pass that on
    # Replace this process so the plugin owns the terminal and Ctrl-C.
    os.execv(aws, cmd)
    return 1  # not reached
