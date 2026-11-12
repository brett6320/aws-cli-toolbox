import argparse
from unittest import mock

import boto3
import pytest
from botocore.exceptions import UnauthorizedSSOTokenError
from botocore.stub import Stubber

from aws_toolbox import config
from aws_toolbox.commands import ssm_connect
from aws_toolbox.commands.ssm_connect import Instance


def _args(**kw):
    base = dict(profile=None, instance=None, region=None, list=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _which(name):
    return f"/usr/bin/{name}"


INSTANCES = [
    Instance("i-0123456789abcdef0", "web", "Amazon Linux", "10.0.0.1"),
    Instance("i-0fedcba9876543210", "web", "Amazon Linux", "10.0.0.2"),
    Instance("mi-0123456789abcdef0", "onprem", "Ubuntu", "192.168.0.5"),
]


def test_list_instances_uses_name_tags_and_falls_back_to_computer_name():
    session = boto3.Session(region_name="us-east-1")
    ssm = session.client("ssm")
    ec2 = session.client("ec2")
    with Stubber(ssm) as s, Stubber(ec2) as e:
        s.add_response("describe_instance_information", {"InstanceInformationList": [
            {"InstanceId": "i-0123456789abcdef0", "ComputerName": "ip-10-0-0-1", "PlatformName": "Amazon Linux",
             "IPAddress": "10.0.0.1"},
            {"InstanceId": "mi-0123456789abcdef0", "ComputerName": "onprem", "PlatformName": "Ubuntu"},
        ]}, {"Filters": [{"Key": "PingStatus", "Values": ["Online"]}]})
        e.add_response("describe_instances", {"Reservations": [{"Instances": [
            {"InstanceId": "i-0123456789abcdef0", "Tags": [{"Key": "Name", "Value": "web"}]}]}]},
            {"Filters": [{"Name": "instance-id", "Values": ["i-0123456789abcdef0"]}]})
        with mock.patch.object(ssm_connect, "get_client", side_effect=lambda svc, *a: {"ssm": ssm, "ec2": ec2}[svc]):
            out = ssm_connect.list_instances(session, "us-east-1")
    assert [(i.id, i.name) for i in out] == [("mi-0123456789abcdef0", "onprem"), ("i-0123456789abcdef0", "web")]


def test_list_instances_without_ec2_access_keeps_computer_names():
    session = boto3.Session(region_name="us-east-1")
    ssm = session.client("ssm")
    ec2 = session.client("ec2")
    with Stubber(ssm) as s, Stubber(ec2) as e:
        s.add_response("describe_instance_information", {"InstanceInformationList": [
            {"InstanceId": "i-0123456789abcdef0", "ComputerName": "ip-10-0-0-1"}]})
        e.add_client_error("describe_instances", "UnauthorizedOperation")
        with mock.patch.object(ssm_connect, "get_client", side_effect=lambda svc, *a: {"ssm": ssm, "ec2": ec2}[svc]):
            out = ssm_connect.list_instances(session, "us-east-1")
    assert [i.name for i in out] == ["ip-10-0-0-1"]


def test_list_instances_name_lookup_is_chunked(monkeypatch):
    monkeypatch.setattr(ssm_connect, "EC2_FILTER_CHUNK", 1)
    session = boto3.Session(region_name="us-east-1")
    ssm = session.client("ssm")
    ec2 = session.client("ec2")
    with Stubber(ssm) as s, Stubber(ec2) as e:
        s.add_response("describe_instance_information", {"InstanceInformationList": [
            {"InstanceId": "i-0123456789abcdef0"}, {"InstanceId": "i-0fedcba9876543210"}]})
        for iid, name in (("i-0123456789abcdef0", "a"), ("i-0fedcba9876543210", "b")):
            e.add_response("describe_instances", {"Reservations": [{"Instances": [
                {"InstanceId": iid, "Tags": [{"Key": "Name", "Value": name}]}]}]},
                {"Filters": [{"Name": "instance-id", "Values": [iid]}]})
        with mock.patch.object(ssm_connect, "get_client", side_effect=lambda svc, *a: {"ssm": ssm, "ec2": ec2}[svc]):
            out = ssm_connect.list_instances(session, "us-east-1")
    assert [i.name for i in out] == ["a", "b"]


def test_label_flattens_tabs_and_newlines():
    assert "\t" not in Instance("i-0123456789abcdef0", "a\tb\nc", "", "").label()
    assert "\n" not in Instance("i-0123456789abcdef0", "a\tb\nc", "", "").label()


def test_resolve_instance_by_name_picks_among_duplicates():
    with mock.patch.object(ssm_connect, "choose", return_value=1) as choose:
        assert ssm_connect.resolve_instance("web", INSTANCES).id == "i-0fedcba9876543210"
    assert len(choose.call_args.args[0]) == 2


def test_resolve_instance_unknown_name(caplog):
    assert ssm_connect.resolve_instance("nope", INSTANCES) is None
    assert "No online SSM-managed instance" in caplog.text


def test_choose_single_item_needs_no_prompt():
    assert ssm_connect.choose(["only"], "x") == 0
    assert ssm_connect.choose([], "x") is None


def test_choose_numbered_menu_prompts_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(ssm_connect.shutil, "which", lambda name: None)
    answers = iter(["9", "2"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert ssm_connect.choose(["a", "b", "c"], "x") == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "x [1-3]: " in out.err


def test_choose_fzf_returns_picked_index(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(ssm_connect.shutil, "which", _which)
    done = mock.Mock(returncode=0, stdout="2\tc\n")
    with mock.patch.object(ssm_connect.subprocess, "run", return_value=done) as run:
        assert ssm_connect.choose(["a", "b", "c"], "x") == 2
    assert run.call_args.kwargs["input"] == "0\ta\n1\tb\n2\tc"
    done.returncode = 130
    with mock.patch.object(ssm_connect.subprocess, "run", return_value=done):
        assert ssm_connect.choose(["a", "b", "c"], "x") is None


def test_choose_without_terminal_fails(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert ssm_connect.choose(["a", "b"], "x") is None


def test_run_with_instance_id_skips_lookup_and_execs_cli(isolated_aws, monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    (isolated_aws / "config").write_text("[profile acct]\nregion = eu-west-1\n")
    with mock.patch.object(ssm_connect, "list_instances") as lst, \
         mock.patch.object(ssm_connect.shutil, "which", side_effect=_which), \
         mock.patch.object(ssm_connect.os, "execv") as execv:
        ssm_connect.run(_args(profile="acct", instance="i-0123456789abcdef0"), config.load())
    lst.assert_not_called()
    execv.assert_called_once_with("/usr/bin/aws", [
        "/usr/bin/aws", "ssm", "start-session", "--target", "i-0123456789abcdef0",
        "--profile", "acct", "--region", "eu-west-1"])


def test_run_picks_profile_then_instance(isolated_aws):
    (isolated_aws / "config").write_text("[profile a]\n[profile b]\n")
    with mock.patch.object(ssm_connect, "choose", side_effect=[1, 0]) as choose, \
         mock.patch.object(ssm_connect, "list_instances", return_value=INSTANCES[2:]), \
         mock.patch.object(ssm_connect.shutil, "which", side_effect=_which), \
         mock.patch.object(ssm_connect.os, "execv") as execv:
        ssm_connect.run(_args(region="us-west-2"), config.load())
    assert choose.call_args_list[0].args == (["a", "b"], "profile")
    cmd = execv.call_args.args[1]
    assert cmd[cmd.index("--target") + 1] == "mi-0123456789abcdef0"
    assert cmd[cmd.index("--profile") + 1] == "b"
    assert cmd[cmd.index("--region") + 1] == "us-west-2"


def test_run_lone_instance_id_picks_profile(isolated_aws):
    (isolated_aws / "config").write_text("[profile a]\n[profile b]\n")
    with mock.patch.object(ssm_connect, "choose", return_value=0), \
         mock.patch.object(ssm_connect.shutil, "which", side_effect=_which), \
         mock.patch.object(ssm_connect.os, "execv") as execv:
        ssm_connect.run(_args(profile="i-0123456789abcdef0"), config.load())
    cmd = execv.call_args.args[1]
    assert cmd[cmd.index("--target") + 1] == "i-0123456789abcdef0"
    assert cmd[cmd.index("--profile") + 1] == "a"


def test_run_without_region_fails_cleanly(isolated_aws, monkeypatch, caplog):
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    (isolated_aws / "config").write_text("[profile acct]\n")
    with mock.patch.object(ssm_connect.os, "execv") as execv:
        assert ssm_connect.run(_args(profile="acct", instance="i-0123456789abcdef0"),
                               _cfg_no_region()) == 2
    execv.assert_not_called()
    assert "--region" in caplog.text


def _cfg_no_region():
    cfg = config.load()
    cfg["defaults"]["region"] = ""
    return cfg


def test_run_list_prints_instances(isolated_aws, capsys):
    (isolated_aws / "config").write_text("[profile acct]\n")
    with mock.patch.object(ssm_connect, "list_instances", return_value=INSTANCES), \
         mock.patch.object(ssm_connect.os, "execv") as execv:
        assert ssm_connect.run(_args(profile="acct", list=True), config.load()) == 0
    execv.assert_not_called()
    assert capsys.readouterr().out.count("\n") == 3


def test_run_expired_sso_suggests_refresh(isolated_aws, caplog):
    (isolated_aws / "config").write_text("[profile acct]\n")
    with mock.patch.object(ssm_connect, "list_instances", side_effect=UnauthorizedSSOTokenError()):
        assert ssm_connect.run(_args(profile="acct"), config.load()) == 1
    assert "aws-sso-refresh" in caplog.text


def test_run_unknown_profile(isolated_aws, caplog):
    assert ssm_connect.run(_args(profile="ghost", instance="i-0123456789abcdef0"), config.load()) == 1
    assert "ghost" in caplog.text


@pytest.mark.parametrize("missing", ["aws", "session-manager-plugin"])
def test_run_requires_cli_and_plugin(isolated_aws, caplog, missing):
    (isolated_aws / "config").write_text("[profile acct]\n")
    with mock.patch.object(ssm_connect.shutil, "which", side_effect=lambda n: None if n == missing else _which(n)), \
         mock.patch.object(ssm_connect.os, "execv") as execv:
        assert ssm_connect.run(_args(profile="acct", instance="i-0123456789abcdef0"), config.load()) == 1
    execv.assert_not_called()
    assert missing in caplog.text
