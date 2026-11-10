import argparse
import json
from datetime import datetime, timezone
from unittest import mock

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from aws_toolbox import config
from aws_toolbox.commands import assume_role, ns_cert, sts_decode, sync_profiles


def _cfg(**overrides):
    cfg = config.load()
    for section, values in overrides.items():
        cfg[section].update(values)
    return cfg


# --- assume-role -----------------------------------------------------------

def test_build_role_arn_defaults_to_caller_account():
    caller = "arn:aws:sts::123456789012:assumed-role/x/y"
    assert assume_role.build_role_arn("Admin", None, caller) == "arn:aws:iam::123456789012:role/Admin"
    assert assume_role.build_role_arn("Admin", "999999999999", caller) == "arn:aws:iam::999999999999:role/Admin"
    with pytest.raises(ValueError):
        assume_role.build_role_arn("Admin", "123", caller)


def test_render_sh_quotes_and_unsets_profile():
    creds = {"AccessKeyId": "AKIA", "SecretAccessKey": "s'cret", "SessionToken": "tok",
             "Expiration": datetime(2030, 1, 1, tzinfo=timezone.utc)}
    out = assume_role.render(creds, "sh", "arn")
    assert out.splitlines()[0] == "unset AWS_PROFILE;"
    assert "export AWS_SECRET_ACCESS_KEY='s'\"'\"'cret';" in out


@mock_aws
def test_assume_role_run(capsys):
    args = argparse.Namespace(role="Admin", account="210987654321", profile=None,
                              session_name="tester", duration=900, format="json")
    assert assume_role.run(args, _cfg()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["RoleArn"] == "arn:aws:iam::210987654321:role/Admin"
    assert out["AWS_ACCESS_KEY_ID"]


# --- sts-decode ------------------------------------------------------------

def test_sts_decode_falls_back_to_admin_profile(isolated_aws, capsys):
    (isolated_aws / "config").write_text(
        "[profile acct]\nsso_start_url = https://x/start\nsso_region = us-east-1\nsso_account_id = 111111111111\n\n"
        "[profile acct-adm]\nsso_start_url = https://x/start\nsso_region = us-east-1\nsso_account_id = 111111111111\n"
    )
    cfg = _cfg()
    assert sts_decode.candidate_profiles("111111111111", cfg) == ["acct", "acct-adm"]

    from botocore.exceptions import ClientError
    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "DecodeAuthorizationMessage")
    with mock.patch.object(sts_decode, "decode", side_effect=[denied, '{"allowed":false}']) as dec:
        args = argparse.Namespace(message="enc", account="111111111111", profile=None, raw=False)
        assert sts_decode.run(args, cfg) == 0
    assert [c.args[1] for c in dec.call_args_list] == ["acct", "acct-adm"]
    assert json.loads(capsys.readouterr().out) == {"allowed": False}


def test_sts_decode_single_account_positional_reads_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("enc\n"))
    with mock.patch.object(sts_decode, "candidate_profiles", return_value=["acct"]), \
         mock.patch.object(sts_decode, "decode", return_value="{}") as dec:
        args = argparse.Namespace(message="111111111111", account=None, profile=None, raw=True)
        assert sts_decode.run(args, _cfg()) == 0
    dec.assert_called_once_with("enc", "acct", "us-east-1")


def test_default_session_name_sanitizes(monkeypatch):
    monkeypatch.setattr(assume_role.getpass, "getuser", lambda: "CORP\\jane doe")
    assert assume_role.default_session_name() == "CORP-jane-doe"
    monkeypatch.setattr(assume_role.getpass, "getuser", mock.Mock(side_effect=OSError))
    assert assume_role.default_session_name() == "aws-toolbox"


def test_sts_decode_calls_api():
    client = boto3.client("sts", region_name="us-east-1")
    with Stubber(client) as stub:
        stub.add_response("decode_authorization_message", {"DecodedMessage": "{}"}, {"EncodedMessage": "enc"})
        with mock.patch.object(sts_decode, "get_client", return_value=client):
            assert sts_decode.decode("enc", None, "us-east-1") == "{}"


# --- sync-profiles ---------------------------------------------------------

@mock_aws
def test_sync_profiles_dry_run_then_execute(isolated_aws, capsys):
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    org.create_account(AccountName="Prod Payments", Email="p@example.com")
    path = isolated_aws / "config"
    path.write_text("[profile main-org]\nsso_start_url = https://x/start\nsso_region = us-east-1\n\n"
                    "[sso-session corp]\nsso_start_url = https://x/start\nsso_region = us-east-1\n")
    cfg = _cfg(sync={"sso_session": "corp", "management_profile": None})

    args = argparse.Namespace(execute=False, apply=False, org=None, profile=None, force=False)
    assert sync_profiles.run(args, cfg) == 0
    assert "[profile prod-payments-adm]" in capsys.readouterr().out
    assert "aws-toolbox managed" not in path.read_text()

    args.execute = True
    assert sync_profiles.run(args, cfg) == 0
    text = path.read_text()
    assert text.startswith("[profile main-org]")
    assert "[profile prod-payments]\nsso_session = corp" in text
    assert "sso_role_name = AdministratorAccess" in text
    assert list(isolated_aws.glob("config.bak-*"))


def _names(block):
    return [line[len("[profile "):-1] for line in block.splitlines() if line.startswith("[profile ")]


def test_build_block_disambiguates_duplicate_names():
    cfg = _cfg(sync={"sso_start_url": "https://x/start"})
    accounts = [{"id": "111111111111", "name": "Dev"}, {"id": "222222222222", "name": "dev"}]
    assert _names(sync_profiles.build_block(accounts, cfg["sync"], cfg["defaults"], reserved=set())) == [
        "dev", "dev-adm", "dev-222222222222", "dev-222222222222-adm"]


def test_build_block_never_emits_duplicate_or_reserved_sections():
    cfg = _cfg(sync={"sso_start_url": "https://x/start"})
    accounts = [{"id": "111111111111", "name": "Dev"}, {"id": "222222222222", "name": "Dev Adm"},
                {"id": "333333333333", "name": "生产"}]
    names = _names(sync_profiles.build_block(accounts, cfg["sync"], cfg["defaults"], reserved={"main-org", "dev-adm-adm"}))
    assert len(names) == len(set(names))
    assert "dev-adm-adm" not in names
    assert {"333333333333", "333333333333-adm"} <= set(names)
    assert "" not in names and "-adm" not in names


def test_list_accounts_prefers_state_and_sorts_stably():
    org = mock.Mock()
    org.get_paginator.return_value.paginate.return_value = [{"Accounts": [
        {"Id": "2", "Name": "dev", "State": "ACTIVE", "Status": "ACTIVE"},
        {"Id": "1", "Name": "Dev", "State": "ACTIVE"},
        {"Id": "3", "Name": "old", "State": "SUSPENDED", "Status": "ACTIVE"},
    ]}]
    assert [a["id"] for a in sync_profiles.list_accounts(org)] == ["1", "2"]


@pytest.mark.parametrize("sync, config_text", [
    ({"sso_start_url": "https://example.awsapps.com/start"}, ""),
    ({"sso_session": "missing"}, "[profile x]\nregion = us-east-1\n"),
])
def test_sync_profiles_rejects_bad_settings(isolated_aws, sync, config_text):
    (isolated_aws / "config").write_text(config_text)
    args = argparse.Namespace(execute=True, apply=False, org=None, profile=None, force=False)
    assert sync_profiles.run(args, _cfg(sync=sync)) == 2


def test_sync_profiles_refuses_to_empty_block(isolated_aws):
    (isolated_aws / "config").write_text("")
    args = argparse.Namespace(execute=True, apply=False, org=None, profile=None, force=False)
    with mock.patch.object(sync_profiles, "list_accounts", return_value=[]), \
         mock.patch.object(sync_profiles, "get_client"), mock.patch.object(sync_profiles, "get_session"):
        assert sync_profiles.run(args, _cfg(sync={"sso_start_url": "https://x/start"})) == 1


def test_atomic_write_follows_symlink(tmp_path):
    real = tmp_path / "real-config"
    real.write_text("old")
    real.chmod(0o600)
    link = tmp_path / "config"
    link.symlink_to(real)
    sync_profiles._atomic_write(link, "new")
    assert link.is_symlink()
    assert real.read_text() == "new"
    assert real.stat().st_mode & 0o777 == 0o600


# --- ns-cert-refresh -------------------------------------------------------

def _self_signed_pem():
    # Any valid CA cert works for parsing; reuse the first one from botocore's bundle.
    text = ns_cert.base_bundle().read_text()
    return ns_cert.PEM_RE.findall(text)[0]


def test_ns_cert_dry_run_and_execute(tmp_path, capsys):
    pem = _self_signed_pem()
    tenant = tmp_path / "nstenantcert.pem"
    tenant.write_text(pem + "\n-----BEGIN CERTIFICATE-----\ngarbage\n-----END CERTIFICATE-----\n")
    out = tmp_path / "bundle.pem"
    cfg = _cfg(netskope={"search_paths": [str(tmp_path)], "include_base_bundle": False})

    args = argparse.Namespace(execute=False, apply=False, output=str(out))
    assert ns_cert.run(args, cfg) == 0
    assert not out.exists()

    args.execute = True
    assert ns_cert.run(args, cfg) == 0
    assert out.read_text() == pem + "\n"
    assert "AWS_CA_BUNDLE" in capsys.readouterr().out


def test_ns_cert_missing_agent(tmp_path):
    cfg = _cfg(netskope={"search_paths": [str(tmp_path / "nope")]})
    with mock.patch.dict(ns_cert.AGENT_DIRS, clear=True):
        assert ns_cert.run(argparse.Namespace(execute=False, apply=False, output=None), cfg) == 1


def test_config_init_writes_example(isolated_aws):
    from aws_toolbox import cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["init", "-q"])
    assert exc.value.code == 0
    assert "[sync]" in (isolated_aws / "toolbox.toml").read_text()


# --- multiple orgs ---------------------------------------------------------

def test_orgs_inherit_roles_but_not_identity():
    cfg = _cfg(sync={"readonly_role": "ViewOnlyAccess", "sso_session": "acme", "profile_prefix": "x-"})
    assert set(config.orgs(cfg)) == {""}  # no [orgs.*]: [sync] is the only org
    cfg["orgs"] = {"globex": {"sso_session": "globex", "management_profile": "globex-mgmt"}}
    globex = config.orgs(cfg)["globex"]
    assert globex["readonly_role"] == "ViewOnlyAccess"
    assert globex["profile_prefix"] == "" and globex["sso_session"] == "globex"


@pytest.mark.parametrize("org, message", [
    ({"sso_session": "globex"}, "management_profile is required"),
    ({"management_profile": "m"}, "exactly one of sso_session or sso_start_url"),
    ({"management_profile": "m", "sso_session": "s", "sso_start_url": "https://x"}, "exactly one of"),
    ({"management_profile": "m", "sso_start_url": "https://x/start"}, "needs sso_region"),
    ({"management_profile": "m", "sso_session": "s", "prefix": "typo"}, "unknown keys prefix"),
])
def test_orgs_reject_incomplete_tables(org, message):
    cfg = _cfg()
    cfg["orgs"] = {"globex": org}
    with pytest.raises(ValueError, match=message):
        config.orgs(cfg)


MULTI_ORG_CONFIG = (
    "[sso-session acme]\nsso_start_url = https://acme.awsapps.com/start#/\nsso_region = us-east-2\n\n"
    "[sso-session globex]\nsso_start_url = https://globex.awsapps.com/start\nsso_region = us-east-1\n\n"
    "[profile acme-mgmt]\nsso_session = acme\nsso_account_id = 111111111111\nsso_role_name = R\n\n"
    "[profile globex-mgmt]\nsso_session = globex\nsso_account_id = 999999999999\nsso_role_name = R\n")


def _multi_org_cfg(globex_mgmt="globex-mgmt"):
    cfg = _cfg()
    cfg["orgs"] = {
        "acme": {"sso_session": "acme", "management_profile": "acme-mgmt", "profile_prefix": "acme-"},
        "globex": {"sso_session": "globex", "management_profile": globex_mgmt, "profile_prefix": "globex-"},
    }
    return cfg


def _fake_orgs(accounts_by_profile):
    def list_accounts(client):
        result = accounts_by_profile[client]
        if isinstance(result, Exception):
            raise result
        return result
    return (mock.patch.object(sync_profiles, "get_session", side_effect=lambda profile, region: profile),
            mock.patch.object(sync_profiles, "get_client", side_effect=lambda service, session: session),
            mock.patch.object(sync_profiles, "list_accounts", side_effect=list_accounts))


def test_sync_writes_one_block_per_org(isolated_aws):
    path = isolated_aws / "config"
    path.write_text(MULTI_ORG_CONFIG)
    patches = _fake_orgs({"acme-mgmt": [{"id": "222222222222", "name": "sbx"}],
                          "globex-mgmt": [{"id": "333333333333", "name": "sbx"}]})
    args = argparse.Namespace(execute=True, apply=False, org=None, profile=None, force=False)
    with patches[0], patches[1], patches[2]:
        assert sync_profiles.run(args, _multi_org_cfg()) == 0
    text = path.read_text()
    assert "# BEGIN aws-toolbox managed profiles: acme" in text
    assert "# BEGIN aws-toolbox managed profiles: globex" in text
    assert "[profile acme-sbx]\nsso_session = acme" in text
    assert "[profile globex-sbx-adm]\nsso_session = globex" in text


def test_sync_one_failing_org_leaves_its_block_and_writes_the_rest(isolated_aws):
    from botocore.exceptions import ClientError
    path = isolated_aws / "config"
    path.write_text(MULTI_ORG_CONFIG)
    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListAccounts")
    patches = _fake_orgs({"acme-mgmt": [{"id": "222222222222", "name": "sbx"}], "globex-mgmt": denied})
    args = argparse.Namespace(execute=True, apply=False, org=None, profile=None, force=False)
    with patches[0], patches[1], patches[2]:
        assert sync_profiles.run(args, _multi_org_cfg()) == 1
    text = path.read_text()
    assert "[profile acme-sbx]" in text and "managed profiles: globex" not in text


def test_sync_org_filter_and_profile_override(isolated_aws):
    (isolated_aws / "config").write_text(MULTI_ORG_CONFIG)
    args = argparse.Namespace(execute=False, apply=False, org=["nope"], profile=None, force=False)
    assert sync_profiles.run(args, _multi_org_cfg()) == 2
    args.org, args.profile = None, "acme-mgmt"
    assert sync_profiles.run(args, _multi_org_cfg()) == 2  # --profile needs one org


def test_sync_rejects_management_profile_from_another_portal(isolated_aws):
    (isolated_aws / "config").write_text(MULTI_ORG_CONFIG)
    args = argparse.Namespace(execute=False, apply=False, org=None, profile=None, force=False)
    assert sync_profiles.run(args, _multi_org_cfg(globex_mgmt="acme-mgmt")) == 2


def test_sts_decode_orders_every_orgs_admin_suffix_last(isolated_aws):
    (isolated_aws / "config").write_text(
        "[profile a-root]\nsso_start_url = https://x/start\nsso_region = us-east-1\nsso_account_id = 1\nsso_role_name = A\n\n"
        "[profile a-ro]\nsso_start_url = https://x/start\nsso_region = us-east-1\nsso_account_id = 1\nsso_role_name = R\n")
    cfg = _cfg()
    cfg["orgs"] = {"o": {"sso_start_url": "https://x/start", "sso_region": "us-east-1", "management_profile": "m",
                   "admin_suffix": "-root"}}
    assert sts_decode.candidate_profiles("1", cfg) == ["a-ro", "a-root"]


def test_sync_rejects_undefined_management_profile(isolated_aws):
    (isolated_aws / "config").write_text(MULTI_ORG_CONFIG)
    args = argparse.Namespace(execute=False, apply=False, org=None, profile=None, force=False)
    assert sync_profiles.run(args, _multi_org_cfg(globex_mgmt="globex-missing")) == 2


def test_orgs_reject_shared_portal_or_management_profile():
    cfg = _multi_org_cfg()
    cfg["orgs"]["globex"]["sso_session"] = "acme"
    with pytest.raises(ValueError, match="share sso_session"):
        config.orgs(cfg)
    cfg = _multi_org_cfg(globex_mgmt="acme-mgmt")
    with pytest.raises(ValueError, match="share management_profile"):
        config.orgs(cfg)


def test_sync_invalid_orgs_table_exits_2(isolated_aws):
    (isolated_aws / "config").write_text(MULTI_ORG_CONFIG)
    cfg = _cfg()
    cfg["orgs"] = {"globex": {"sso_session": "globex"}}
    args = argparse.Namespace(execute=False, apply=False, org=None, profile=None, force=False)
    assert sync_profiles.run(args, cfg) == 2


def test_sync_refuses_to_migrate_over_unnamed_block(isolated_aws):
    from aws_toolbox import awsconfig
    (isolated_aws / "config").write_text(
        awsconfig.replace_managed_block(MULTI_ORG_CONFIG, "[profile acme-sbx]\nsso_session = acme"))
    args = argparse.Namespace(execute=True, apply=False, org=None, profile=None, force=False)
    assert sync_profiles.run(args, _multi_org_cfg()) == 2


@pytest.mark.parametrize("extra, profile", [
    # --profile from the other org's portal
    ("", "acme-mgmt"),
    # role chained from the other org's SSO profile
    ("\n[profile globex-chain]\nrole_arn = arn:aws:iam::1:role/x\nsource_profile = acme-mgmt\n", "globex-chain"),
    # portal can't be determined
    ("\n[profile globex-static]\ncredential_process = /bin/true\n", "globex-static"),
])
def test_sync_rejects_management_profile_outside_org_portal(isolated_aws, extra, profile):
    (isolated_aws / "config").write_text(MULTI_ORG_CONFIG + extra)
    args = argparse.Namespace(execute=False, apply=False, org=["globex"], profile=profile, force=False)
    assert sync_profiles.run(args, _multi_org_cfg()) == 2


def test_sync_accepts_role_chained_from_org_portal(isolated_aws):
    path = isolated_aws / "config"
    path.write_text(MULTI_ORG_CONFIG + "\n[profile globex-chain]\nrole_arn = arn:aws:iam::1:role/x\n"
                    "source_profile = globex-mgmt\n")
    patches = _fake_orgs({"globex-chain": [{"id": "333333333333", "name": "sbx"}]})
    args = argparse.Namespace(execute=False, apply=False, org=["globex"], profile="globex-chain", force=False)
    with patches[0], patches[1], patches[2]:
        assert sync_profiles.run(args, _multi_org_cfg()) == 0


def test_build_block_short_names():
    cfg = _cfg(sync={"sso_start_url": "https://x/start", "profile_prefix": "acme-",
                     "short_names": {"111111111111": "core"}})
    accounts = [{"id": "111111111111", "name": "Core Networking"}, {"id": "222222222222", "name": "Dev"}]
    assert _names(sync_profiles.build_block(accounts, cfg["sync"], cfg["defaults"], reserved=set())) == [
        "acme-core", "acme-core-adm", "acme-dev", "acme-dev-adm"]


def test_log_colors_only_on_a_terminal(monkeypatch):
    import io
    import logging
    from aws_toolbox import cli
    record = logging.LogRecord("aws_toolbox", logging.WARNING, __file__, 1, "careful", None, None)
    assert cli.ColorFormatter("%(message)s").format(record) == "\033[33mcareful\033[0m"
    record.levelno = logging.INFO
    assert cli.ColorFormatter("%(message)s").format(record) == "careful"
    tty = io.StringIO()
    tty.isatty = lambda: True
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert cli._use_color(tty) and not cli._use_color(io.StringIO())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not cli._use_color(tty)


def test_sync_summary_keeps_every_orgs_warnings_on_failure_and_exception(isolated_aws, caplog):
    import logging
    from botocore.exceptions import ClientError
    (isolated_aws / "config").write_text(MULTI_ORG_CONFIG)
    acme = [{"id": "111111111111", "name": "Ops"}]
    args = argparse.Namespace(execute=False, apply=False, org=None, profile=None, force=False)
    caplog.set_level(logging.INFO)
    held_warning = mock.patch.object(sync_profiles, "warn_unknown_short_names",
                                     side_effect=lambda a, o, label: sync_profiles.log.warning("%s: held", label))

    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListAccounts")
    patches = _fake_orgs({"acme-mgmt": acme, "globex-mgmt": denied})
    with patches[0], patches[1], patches[2], held_warning:
        assert sync_profiles.run(args, _multi_org_cfg()) == 1
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs.index("Summary:") < msgs.index("[orgs.acme]: held")
    assert "[orgs.acme]: 1 accounts in the org; 2 profiles for 1 accounts, 1 warning(s):" in msgs
    assert msgs.index("[orgs.globex]: failed; its managed block is unchanged:") < \
        msgs.index("[orgs.globex]: AWS error AccessDenied: nope")

    # a later org raising still releases the earlier org's held warnings
    caplog.clear()
    real_build_block = sync_profiles.build_block
    builds = iter([real_build_block, None])

    def build_then_fail(*a, **kw):
        build = next(builds)
        if build is None:
            raise RuntimeError("boom")
        return build(*a, **kw)

    patches = _fake_orgs({"acme-mgmt": acme, "globex-mgmt": acme})
    with patches[0], patches[1], patches[2], held_warning, \
         mock.patch.object(sync_profiles, "build_block", side_effect=build_then_fail):
        with pytest.raises(RuntimeError):
            sync_profiles.run(args, _multi_org_cfg())
    msgs = [r.getMessage() for r in caplog.records]
    assert "[orgs.acme]: held" in msgs and "[orgs.globex]: held" in msgs
