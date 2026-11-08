import argparse
import configparser
from unittest import mock

import boto3
import pytest
from moto import mock_aws

from aws_toolbox import account_meta, config
from aws_toolbox.commands import sync_profiles

TABLE_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/account-mapping"  # moto's default account


# --- ported helpers ---------------------------------------------------------

def test_normalize_account_id_pads_numbers():
    assert account_meta.normalize_account_id(12345678901) == "012345678901"
    assert account_meta.normalize_account_id("12345678901.0") == "012345678901"
    assert account_meta.normalize_account_id(" 123456789012 ") == "123456789012"


@pytest.mark.parametrize("raw, env", [
    ("production", "Prd"), ("PROD", "Prd"), ("testing", "Tst"), ("billing", "Bil"),
    (" sandbox ", "Sbx"), ("qa", None), (None, None),
])
def test_normalize_env(raw, env):
    assert account_meta.normalize_env(raw) == env


def test_resolve_settings_precedence_and_arn(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"export BM_DDB_TABLE_ARN='{TABLE_ARN}'\nBM_DEFAULT_ROLES=ReadOnly, Administrator\n"
                        "BM_ATTR_OU=domain\nAWS_REGION=us-east-2\n")
    monkeypatch.setenv("BM_ATTR_OU", "businessDomain")  # environment beats .env
    s = account_meta.resolve_settings({"roles": "Admin"}, account_meta.read_dotenv(env_file))
    assert s["roles"] == ["Admin"]  # kwargs beat both
    assert s["attr_ou"] == "businessDomain"
    assert (s["ddb_table"], s["ddb_account_id"], s["ddb_region"]) == ("account-mapping", "123456789012", "us-east-1")
    assert s["region"] == "us-east-2" and s["include_inactive"] is False
    with pytest.raises(ValueError, match="required"):
        account_meta.resolve_settings({})
    with pytest.raises(ValueError, match="Not a DynamoDB table ARN"):
        account_meta.resolve_settings({"ddb_table_arn": "arn:aws:s3:::bucket"})


def test_fetch_metadata_skips_duplicated_accounts():
    ddb = mock.Mock()
    rows = [
        {"accountNumber": {"N": "12345678901"}, "accountShortCode": {"S": "Net"}, "ou": {"S": "Core"}},
        {"accountNumber": {"S": "222222222222"}, "accountShortCode": {"S": "a"}},
        {"accountNumber": {"S": "222222222222"}, "accountShortCode": {"S": "b"}},
        {"accountShortCode": {"S": "no-id"}},
    ]
    ddb.get_paginator.return_value.paginate.return_value = [{"Items": rows}]
    attrs = {"account_id": "accountNumber", "short_name": "accountShortCode", "ou": "ou", "environment": "environment"}
    meta, dupes = account_meta.fetch_metadata(ddb, "t", attrs)
    assert dupes == {"222222222222"}
    assert set(meta) == {"012345678901"}
    kwargs = ddb.get_paginator.return_value.paginate.call_args.kwargs
    assert kwargs["TableName"] == "t" and set(kwargs["ExpressionAttributeNames"].values()) == set(attrs.values())


def test_merge_derives_short_name_domain_env():
    org = {"accounts": {
        "1": {"id": "1", "name": "Prod Payments"},
        "2": {"id": "2", "name": "Old Thing"},
        "3": {"id": "3", "name": "Mgmt"},
    }}
    meta = {"1": {"shortName": " PAY ", "ou": "Payments", "envRaw": "production"},
            "3": {"shortName": "", "ou": "Root", "envRaw": "qa"}}
    by_id = {a["id"]: a for a in account_meta.merge(org, meta)}
    assert (by_id["1"]["shortName"], by_id["1"]["domain"], by_id["1"]["env"]) == ("pay", "Payments", "Prd")
    assert (by_id["2"]["shortName"], by_id["2"]["domain"], by_id["2"]["env"]) == ("Old Thing", "Legacy", None)
    assert (by_id["3"]["shortName"], by_id["3"]["domain"], by_id["3"]["env"]) == ("Mgmt", "Root", None)


USER_SUFFIXES = {"ReadOnly": "-ro", "Account-Admin": "-adm", "Platform-Admin": ""}


def test_role_suffixes_mapping_default_and_errors():
    roles = ["Platform-Admin", "ReadOnly", "Account-Admin", "Billing Viewer"]
    assert sync_profiles.role_suffixes(roles, USER_SUFFIXES) == [
        ("Platform-Admin", ""), ("ReadOnly", "-ro"), ("Account-Admin", "-adm"),
        ("Billing Viewer", "-billing-viewer")]
    with pytest.raises(ValueError, match="both use suffix"):
        sync_profiles.role_suffixes(["A", "B"], {"A": "", "B": ""})
    with pytest.raises(ValueError, match="lowercase"):
        sync_profiles.role_suffixes(["A"], {"A": "ro"})
    with pytest.raises(ValueError, match="lowercase"):
        sync_profiles.role_suffixes(["A"], {"A": "-RO"})


def test_build_role_block_names_and_clashes():
    org = {"exclude_accounts": ["999999999999"], "profile_prefix": "bm-", "source_profile": "bm-src"}
    accounts = [
        {"id": "111111111111", "name": "Net", "shortName": "net", "domain": "Core", "env": "Prd"},
        {"id": "222222222222", "name": "Net 2", "shortName": "net", "domain": "Core", "env": None},
        {"id": "999999999999", "name": "Skip", "shortName": "skip", "domain": "Legacy", "env": None},
    ]
    roles = sync_profiles.role_suffixes(["Platform-Admin", "ReadOnly", "Account-Admin"], USER_SUFFIXES)
    block = sync_profiles.build_role_block(accounts, org, {"region": "us-east-1", "output": "json"},
                                           roles, "aws", reserved={"bm-x"})
    parser = configparser.RawConfigParser()
    parser.read_string(block)
    # a shared short code gives every account in the clash its ID; none keeps the plain name
    assert parser.sections() == [
        "profile bm-net-111111111111", "profile bm-net-111111111111-ro", "profile bm-net-111111111111-adm",
        "profile bm-net-222222222222", "profile bm-net-222222222222-ro", "profile bm-net-222222222222-adm",
    ]
    assert parser["profile bm-net-111111111111"]["role_arn"] == "arn:aws:iam::111111111111:role/Platform-Admin"
    assert parser["profile bm-net-111111111111-adm"]["role_arn"] == "arn:aws:iam::111111111111:role/Account-Admin"
    assert parser["profile bm-net-111111111111-ro"]["source_profile"] == "bm-src"
    assert "# Net (111111111111) | domain: Core | env: Prd" in block


def test_orgs_inherit_roles_and_suffixes_from_sync():
    cfg = config.load()
    cfg["sync"].update(roles=["ReadOnly"], role_suffixes={"ReadOnly": "-ro"})
    cfg["orgs"] = {"a": {"sso_session": "a", "management_profile": "m"},
                   "b": {"sso_session": "b", "management_profile": "n", "role_suffixes": {"ReadOnly": "-view"}}}
    orgs = config.orgs(cfg)
    assert orgs["a"]["roles"] == ["ReadOnly"] and orgs["a"]["role_suffixes"] == {"ReadOnly": "-ro"}
    assert orgs["b"]["role_suffixes"] == {"ReadOnly": "-view"}


# --- sync in assume-role mode ----------------------------------------------

def _args(**kw):
    base = dict(execute=False, apply=False, org=None, profile=None, force=False)
    base.update({k: None for k in sync_profiles.META_FLAGS})
    base.update(kw)
    return argparse.Namespace(**base)


def _role_cfg(env_file, **sync):
    cfg = config.load()
    cfg["sync"].update({"sso_session": "corp", "management_profile": None, "source_profile": "corp-src",
                        "env_file": str(env_file), "profile_prefix": "corp-"}, **sync)
    return cfg


CORP_CONFIG = ("[sso-session corp]\nsso_start_url = https://corp.awsapps.com/start\nsso_region = us-east-1\n\n"
               "[profile corp-src]\nsso_session = corp\nsso_account_id = 123456789012\n"
               "sso_role_name = PlatformAdmin\n")


@mock_aws
def test_sync_assume_role_mode_end_to_end(isolated_aws, tmp_path, capsys):
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    root = org.list_roots()["Roots"][0]["Id"]
    ou = org.create_organizational_unit(ParentId=root, Name="Workloads")["OrganizationalUnit"]["Id"]
    ids = {}
    for name in ("Prod Payments", "Shared Net", "Orphan", "Twice"):
        ids[name] = org.create_account(AccountName=name, Email=f"{name[0]}@example.com")["CreateAccountStatus"]["AccountId"]
    org.move_account(AccountId=ids["Prod Payments"], SourceParentId=root, DestinationParentId=ou)

    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="account-mapping", BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}])
    rows = [
        ("Prod Payments", {"N": str(int(ids["Prod Payments"]))}, "PAY", "Payments", "production"),
        ("Shared Net", {"S": ids["Shared Net"]}, "net", "Root", "qa"),
        ("Twice", {"S": ids["Twice"]}, "tw1", "X", "dev"),
        ("Twice", {"S": ids["Twice"]}, "tw2", "X", "dev"),
    ]
    for i, (_, acct, short, ou_name, env) in enumerate(rows):
        ddb.put_item(TableName="account-mapping", Item={
            "pk": {"S": f"{ou_name}-{env}-{i}"},
            "sk": {"S": f"b-{i}"},
            "accountNumber": acct, "accountShortCode": {"S": short}, "ou": {"S": ou_name},
            "environment": {"S": env}})

    env_file = tmp_path / "bm.env"
    env_file.write_text(f"BM_DDB_TABLE_ARN={TABLE_ARN}\nBM_DEFAULT_ROLES=ReadOnly,Administrator\n")
    path = isolated_aws / "config"
    path.write_text(CORP_CONFIG)

    cfg = _role_cfg(env_file, role_suffixes={"ReadOnly": "-ro", "Administrator": "-adm"})
    assert sync_profiles.run(_args(execute=True), cfg) == 0
    text = path.read_text()
    parser = configparser.RawConfigParser()
    parser.read_string(text)
    pay = parser["profile corp-pay-adm"]
    assert pay["role_arn"] == f"arn:aws:iam::{ids['Prod Payments']}:role/Administrator"
    assert pay["source_profile"] == "corp-src"
    assert "domain: Payments | env: Prd" in text
    assert parser.has_section("profile corp-net-ro")  # unknown env 'qa' -> no env
    assert f"Shared Net ({ids['Shared Net']}) | domain: Root | env: -" in text
    assert parser.has_section("profile corp-orphan-ro")  # no row -> account name, Legacy
    assert "domain: Legacy" in text
    assert not any(ids["Twice"] in v for s in parser.sections() for v in parser[s].values())  # duplicate rows
    assert "sso_account_id" not in text.split("# BEGIN")[1]  # no SSO profiles in the block
    # read-only against AWS: the table is unchanged
    assert ddb.scan(TableName="account-mapping")["Count"] == 4


@mock_aws
def test_sync_unreadable_table_bootstraps_then_keeps_profiles(isolated_aws, tmp_path):
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    acct = org.create_account(AccountName="Ops Tools", Email="o@example.com")["CreateAccountStatus"]["AccountId"]
    env_file = tmp_path / "bm.env"
    # credentials resolve to moto's account, not the table's: the read is refused
    env_file.write_text("BM_DDB_TABLE_ARN=arn:aws:dynamodb:us-east-1:999999999999:table/account-mapping\n"
                        "BM_DDB_PROFILE=corp-src\nBM_DEFAULT_ROLES=ReadOnly\n")
    path = isolated_aws / "config"
    path.write_text(CORP_CONFIG)
    cfg = _role_cfg(env_file, require_short_name=True)  # ignored while the table is unreadable
    with mock.patch.object(account_meta, "get_session", return_value=boto3.Session(region_name="us-east-1")):
        assert sync_profiles.run(_args(execute=True), cfg) == 0  # run 1: names from accounts
        text = path.read_text()
        assert "[profile corp-ops-tools-readonly]" in text
        assert f"role/ReadOnly" in text and acct in text
        # a later run that still can't read the table leaves the profiles alone
        assert sync_profiles.run(_args(execute=True), cfg) == 1
        assert path.read_text() == text


def test_read_table_falls_back_to_generated_profile(isolated_aws):
    from aws_toolbox import awsconfig
    text = awsconfig.replace_managed_block(CORP_CONFIG, (
        "[profile corp-ops-readonly]\nrole_arn = arn:aws:iam::123456789012:role/ReadOnly\nsource_profile = corp-src\n\n"
        "[profile corp-ops]\nrole_arn = arn:aws:iam::123456789012:role/Platform-Admin\nsource_profile = corp-src\n\n"
        "[profile corp-other]\nrole_arn = arn:aws:iam::222222222222:role/Platform-Admin\nsource_profile = corp-src"))
    roles = ["Platform-Admin", "ReadOnly"]
    assert sync_profiles.generated_profile_for(text, "", "123456789012", roles) == "corp-ops"
    assert sync_profiles.generated_profile_for(text, "", "123456789012", ["ReadOnly"]) == "corp-ops-readonly"
    assert sync_profiles.generated_profile_for(text, "", "333333333333", roles) is None

    from botocore.exceptions import ClientError
    denied = ClientError({"Error": {"Code": "ForbiddenException", "Message": "No access"}}, "GetRoleCredentials")
    used = []

    def ddb_session(settings, org_session):
        used.append(settings["ddb_profile"])
        if settings["ddb_profile"] != "corp-ops":
            raise denied
        return mock.Mock()
    s = account_meta.resolve_settings({"ddb_table_arn": TABLE_ARN, "ddb_profile": "ops-adm", "roles": roles})
    with mock.patch.object(account_meta, "get_ddb_session", side_effect=ddb_session), \
         mock.patch.object(sync_profiles, "get_client"), \
         mock.patch.object(account_meta, "fetch_metadata", return_value=({"1": {}}, set())):
        assert sync_profiles.read_table("", s, None, text) == ({"1": {}}, set())
    assert used == ["ops-adm", "corp-ops"]


def test_sync_assume_role_config_errors(isolated_aws, tmp_path):
    (isolated_aws / "config").write_text(CORP_CONFIG)
    missing = tmp_path / "nope.env"
    assert sync_profiles.run(_args(), _role_cfg(missing)) == 2  # env_file not found
    empty = tmp_path / "empty.env"
    empty.write_text("")
    assert sync_profiles.run(_args(), _role_cfg(empty)) == 2  # no table ARN
    good = tmp_path / "bm.env"
    good.write_text(f"BM_DDB_TABLE_ARN={TABLE_ARN}\n")
    assert sync_profiles.run(_args(), _role_cfg(good, source_profile="undefined")) == 2


def test_sync_source_profile_must_be_the_orgs(isolated_aws, tmp_path):
    from test_commands import MULTI_ORG_CONFIG, _multi_org_cfg
    env_file = tmp_path / "bm.env"
    env_file.write_text(f"BM_DDB_TABLE_ARN={TABLE_ARN}\n")
    (isolated_aws / "config").write_text(MULTI_ORG_CONFIG)
    cfg = _multi_org_cfg()
    cfg["orgs"]["globex"].update(source_profile="acme-mgmt", env_file=str(env_file))
    assert sync_profiles.run(_args(org=["globex"]), cfg) == 2
    # table flags apply to one org at a time
    assert sync_profiles.run(_args(ddb_profile="x"), _multi_org_cfg()) == 2


def test_sync_rejects_clashing_role_suffixes(isolated_aws, tmp_path):
    (isolated_aws / "config").write_text(CORP_CONFIG)
    env_file = tmp_path / "bm.env"
    env_file.write_text(f"BM_DDB_TABLE_ARN={TABLE_ARN}\nBM_DEFAULT_ROLES=A,B\n")
    cfg = _role_cfg(env_file, role_suffixes={"A": "-x", "B": "-x"})
    assert sync_profiles.run(_args(), cfg) == 2


def test_build_role_block_neutralizes_control_characters():
    org = {"exclude_accounts": [], "profile_prefix": "", "source_profile": "src"}
    evil = "Core\n[profile x]\ncredential_process = /usr/bin/touch /tmp/pwned\n#"
    accounts = [{"id": "111111111111", "name": "Net\r\n[default]", "shortName": "net", "domain": evil, "env": None}]
    block = sync_profiles.build_role_block(accounts, org, {"region": "us-east-1", "output": "json"},
                                           [("ReadOnly", "-ro")], "aws", reserved=set())
    parser = configparser.RawConfigParser()
    parser.read_string(block)
    assert parser.sections() == ["profile net-ro"]
    assert "credential_process" not in parser["profile net-ro"]
    assert len(block.splitlines()) == 6


def test_sync_assume_role_refuses_when_no_account_matches_table(isolated_aws, tmp_path):
    (isolated_aws / "config").write_text(CORP_CONFIG)
    env_file = tmp_path / "bm.env"
    env_file.write_text(f"BM_DDB_TABLE_ARN={TABLE_ARN}\nBM_DEFAULT_ROLES=ReadOnly\n")
    tree = {"tree": {}, "accounts": {"111111111111": {"id": "111111111111", "name": "Net", "email": "",
                                                    "state": "ACTIVE", "orgPath": []}}}
    session = mock.Mock()
    session.get_partition_for_region.return_value = "aws"
    with mock.patch.object(account_meta, "get_session", return_value=session), \
         mock.patch.object(sync_profiles, "get_client"), \
         mock.patch.object(account_meta, "fetch_org", return_value=tree), \
         mock.patch.object(account_meta, "get_ddb_session"), \
         mock.patch.object(account_meta, "fetch_metadata", return_value=({}, set())):
        assert sync_profiles.run(_args(execute=True), _role_cfg(env_file)) == 1
        assert "managed profiles" not in (isolated_aws / "config").read_text()
        assert sync_profiles.run(_args(execute=True, force=True), _role_cfg(env_file)) == 0
    assert "[profile corp-net-readonly]" in (isolated_aws / "config").read_text()


@pytest.mark.parametrize("roles, expected", [
    (["ReadOnly", " Admin "], ["ReadOnly", "Admin"]),
    ("ReadOnly, Admin", ["ReadOnly", "Admin"]),
])
def test_meta_settings_roles_list_or_string(isolated_aws, tmp_path, roles, expected):
    env_file = tmp_path / "bm.env"
    env_file.write_text(f"BM_DDB_TABLE_ARN={TABLE_ARN}\n")
    org = config.orgs(_role_cfg(env_file, roles=roles))[""]
    assert sync_profiles.meta_settings(org, _args())["roles"] == expected


def test_meta_settings_warns_when_environment_hides_env_file(isolated_aws, tmp_path, monkeypatch, caplog):
    env_file = tmp_path / "bm.env"
    env_file.write_text(f"BM_DDB_TABLE_ARN={TABLE_ARN}\nBM_ATTR_OU=ou\n")
    monkeypatch.setenv("BM_ATTR_OU", "domain")
    org = config.orgs(_role_cfg(env_file))[""]
    assert sync_profiles.meta_settings(org, _args())["attr_ou"] == "domain"
    assert "BM_ATTR_OU from the environment overrides" in caplog.text


def test_sync_only_source_profile_portal_checked(isolated_aws, tmp_path):
    env_file = tmp_path / "bm.env"
    env_file.write_text(f"BM_DDB_TABLE_ARN={TABLE_ARN}\n")
    (isolated_aws / "config").write_text(
        CORP_CONFIG + "\n[profile other]\nsso_start_url = https://other.awsapps.com/start\nsso_region = us-east-1\n")
    assert sync_profiles.run(_args(), _role_cfg(env_file, source_profile="other")) == 2


def test_table_flags_rejected_for_sso_mode_org(isolated_aws):
    (isolated_aws / "config").write_text(CORP_CONFIG)
    cfg = config.load()
    cfg["sync"].update(sso_session="corp", management_profile=None)
    assert sync_profiles.run(_args(ddb_profile="x"), cfg) == 2


@mock_aws
@pytest.mark.parametrize("include", [False, True])
def test_fetch_org_inactive_filter(include):
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    acct = org.create_account(AccountName="Gone", Email="g@example.com")["CreateAccountStatus"]["AccountId"]
    org.close_account(AccountId=acct)
    ids = set(account_meta.fetch_org(org, include)["accounts"])
    assert (acct in ids) is include


@mock_aws
def test_require_short_name_skips_accounts_without_one(isolated_aws, tmp_path):
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    ids = {n: org.create_account(AccountName=n, Email=f"{n}@example.com")["CreateAccountStatus"]["AccountId"]
           for n in ("Sandbox", "Network", "Unnamed", "NoRow")}
    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(TableName="account-mapping", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])
    for i, (name, short) in enumerate((("Sandbox", "sbx"), ("Network", "net"), ("Unnamed", " "))):
        ddb.put_item(TableName="account-mapping", Item={
            "pk": {"S": str(i)}, "accountNumber": {"S": ids[name]}, "accountShortCode": {"S": short}})
    env_file = tmp_path / "bm.env"
    env_file.write_text(f"BM_DDB_TABLE_ARN={TABLE_ARN}\nBM_DEFAULT_ROLES=ReadOnly,Account-Admin\n")
    path = isolated_aws / "config"
    path.write_text(CORP_CONFIG)
    cfg = _role_cfg(env_file, require_short_name=True,
                    role_suffixes={"ReadOnly": "-ro", "Account-Admin": "-adm"})
    assert sync_profiles.run(_args(execute=True), cfg) == 0
    parser = configparser.RawConfigParser()
    parser.read(path)
    generated = sorted(s for s in parser.sections() if s.startswith("profile corp-") and s != "profile corp-src")
    assert generated == ["profile corp-net-adm", "profile corp-net-ro", "profile corp-sbx-adm", "profile corp-sbx-ro"]
    assert parser["profile corp-sbx-adm"]["role_arn"] == f"arn:aws:iam::{ids['Sandbox']}:role/Account-Admin"
