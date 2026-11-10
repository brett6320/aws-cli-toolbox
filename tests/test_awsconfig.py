import hashlib
import json
from datetime import datetime, timezone

import pytest

from aws_toolbox import awsconfig
from aws_toolbox.commands.sso_refresh import plan_logins

CONFIG = """\
[sso-session corp]
sso_start_url = https://corp.awsapps.com/start/
sso_region = us-east-1

[profile main-org]
sso_start_url = https://corp.awsapps.com/start
sso_region = us-east-1
sso_account_id = 111111111111
sso_role_name = AdministratorAccess

[profile other-org]
sso_start_url = https://other.awsapps.com/start
sso_region = us-west-2

[profile dev]
sso_session = corp
sso_account_id = 222222222222
sso_role_name = ReadOnlyAccess

[profile plain]
region = us-east-1
"""


def _parse(text):
    import configparser
    p = configparser.RawConfigParser()
    p.read_string(text)
    return p


def test_sso_profiles_resolves_direct_and_session():
    profiles = {p.name: p for p in awsconfig.sso_profiles(_parse(CONFIG))}
    assert set(profiles) == {"main-org", "other-org", "dev"}
    assert profiles["dev"].start_url == "https://corp.awsapps.com/start"
    assert profiles["dev"].cache_key == "corp"
    assert profiles["main-org"].cache_key == "https://corp.awsapps.com/start"


def test_plan_logins_main_first_and_dedupes_by_cache_key():
    profiles = awsconfig.sso_profiles(_parse(CONFIG))
    plan, main_url = plan_logins(list(reversed(profiles)), "main-org")
    # dev (sso-session corp) shares main-org's URL but has its own token cache.
    assert [p.name for p in plan] == ["main-org", "dev", "other-org"]
    assert main_url == "https://corp.awsapps.com/start"


def test_cache_key_keeps_url_as_written():
    profiles = {p.name: p for p in awsconfig.sso_profiles(_parse(
        "[profile s]\nsso_start_url = https://x/start/\nsso_region = us-east-1\n"))}
    assert profiles["s"].start_url == "https://x/start"
    assert profiles["s"].cache_key == "https://x/start/"


def test_token_expiry_reads_cli_cache(tmp_path):
    profile = awsconfig.SsoProfile("p", "https://x/start", "us-east-1", None)
    digest = hashlib.sha1(b"https://x/start").hexdigest()
    (tmp_path / f"{digest}.json").write_text(json.dumps({"expiresAt": "2030-01-02T03:04:05Z"}))
    assert awsconfig.token_expiry(profile, tmp_path) == datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert awsconfig.token_expiry(awsconfig.SsoProfile("q", "https://y", "", None), tmp_path) is None


def test_replace_managed_block_appends_then_replaces():
    original = "[default]\nregion = us-east-1\n"
    first = awsconfig.replace_managed_block(original, "[profile a]\nx = 1")
    assert first.startswith(original)
    assert awsconfig.BEGIN_MARKER in first
    second = awsconfig.replace_managed_block(first + "[profile after]\ny = 2\n", "[profile b]\nx = 2")
    assert "[profile a]" not in second
    assert "[profile b]" in second
    assert second.startswith(original)
    assert second.endswith("[profile after]\ny = 2\n")


def test_replace_managed_block_rejects_unbalanced_markers():
    with pytest.raises(ValueError):
        awsconfig.replace_managed_block(awsconfig.BEGIN_MARKER + "\n", "x")


def test_replace_managed_block_rejects_duplicate_blocks():
    once = awsconfig.replace_managed_block("", "[profile a]\nx = 1")
    with pytest.raises(ValueError):
        awsconfig.replace_managed_block(once + once, "x")


def test_reserved_profile_names_ignores_block():
    text = awsconfig.replace_managed_block(CONFIG, "[profile generated]\nx = 1")
    names = awsconfig.reserved_profile_names(text)
    assert "generated" not in names
    assert {"main-org", "dev", "plain"} <= names


def test_profile_name_for():
    assert awsconfig.profile_name_for("Prod: Payments (EU)", "acme-") == "acme-prod-payments-eu"
    # a name that already starts with the prefix doesn't get it twice
    assert awsconfig.profile_name_for("ACME Prod", "acme-") == "acme-prod"
    assert awsconfig.profile_name_for("acme-prod", "acme-") == "acme-prod"
    assert awsconfig.profile_name_for("Acmeprod", "acme-") == "acme-acmeprod"  # not the whole prefix
    assert awsconfig.profile_name_for("acme", "acme-") == "acme-acme"


def test_sso_refresh_skips_when_refresh_token_still_works(isolated_aws, monkeypatch):
    import argparse
    from aws_toolbox import config
    from aws_toolbox.commands import sso_refresh
    (isolated_aws / "config").write_text(CONFIG)
    calls = []
    monkeypatch.setattr(sso_refresh.shutil, "which", lambda _: "/usr/bin/aws")
    monkeypatch.setattr(sso_refresh.subprocess, "run", lambda cmd: calls.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(sso_refresh, "credentials_work", lambda p: p.name == "dev")
    args = argparse.Namespace(force=False, main_org=None, main_profile=None, list=False, manual_login=None)
    assert sso_refresh.run(args, config.load()) == 0
    # dev's session still works; main-org opens the browser; other-org names no account
    assert calls == [
        ["/usr/bin/aws", "sso", "login", "--profile", "main-org"],
    ]


def test_org_blocks_are_independent_and_match_whole_lines():
    text = awsconfig.replace_managed_block("", "[profile legacy]\nx = 1")
    text = awsconfig.replace_managed_block(text, "[profile a]\nx = 1", "acme")
    text = awsconfig.replace_managed_block(text, "[profile b]\nx = 1", "acme2")
    text = awsconfig.replace_managed_block(text, "[profile a2]\nx = 2", "acme")
    assert "[profile a]" not in text and "[profile a2]" in text
    assert "[profile legacy]" in text and "[profile b]" in text
    assert awsconfig.reserved_profile_names(text, "acme") == {"legacy", "b"}
    assert awsconfig.has_managed_block(text, "acme2") and not awsconfig.has_managed_block(text, "globex")


def test_sso_refresh_main_org_and_only_profiles_with_an_account(isolated_aws, monkeypatch):
    import argparse
    from aws_toolbox import config
    from aws_toolbox.commands import sso_refresh
    (isolated_aws / "config").write_text(
        "[sso-session acme]\nsso_start_url = https://acme.awsapps.com/start#/\nsso_region = us-east-2\n\n"
        "[sso-session globex]\nsso_start_url = https://globex.awsapps.com/start\nsso_region = us-east-1\n\n"
        "[profile acme-net-ro]\nsso_session = acme\nsso_account_id = 1\nsso_role_name = R\n\n"
        "[profile globex-other]\nsso_session = globex\nsso_region = us-east-1\n")
    cfg = config.load()
    cfg["sso"]["main_org"] = "acme"
    cfg["orgs"] = {"acme": {"sso_session": "acme", "management_profile": "acme-net-ro"},
                   "globex": {"sso_session": "globex", "management_profile": "globex-mgmt"}}
    calls = []
    monkeypatch.setattr(sso_refresh.shutil, "which", lambda _: "/usr/bin/aws")
    monkeypatch.setattr(sso_refresh.subprocess, "run", lambda cmd: calls.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(sso_refresh, "credentials_work", lambda p: False)
    args = argparse.Namespace(force=False, main_org=None, main_profile=None, list=False, manual_login=None)
    assert sso_refresh.run(args, cfg) == 0
    # globex has no profile with sso_account_id, so it isn't logged in at all.
    assert calls == [
        ["/usr/bin/aws", "sso", "login", "--profile", "acme-net-ro"],
    ]
    args.main_org = "nope"
    assert sso_refresh.run(args, cfg) == 2


def test_sso_refresh_manual_login_never_opens_browser_and_goes_last(isolated_aws, monkeypatch):
    import argparse
    from aws_toolbox import config
    from aws_toolbox.commands import sso_refresh
    (isolated_aws / "config").write_text(
        "[sso-session main-org]\nsso_start_url = https://acme.awsapps.com/start\nsso_region = us-east-1\n\n"
        "[sso-session main-org-adm]\nsso_start_url = https://acme.awsapps.com/start\nsso_region = us-east-1\n\n"
        "[sso-session globex]\nsso_start_url = https://globex.awsapps.com/start\nsso_region = us-east-1\n\n"
        "[profile Platform-ADM]\nsso_session = main-org-adm\nsso_account_id = 1\nsso_role_name = Admin\n\n"
        # shares main-org's token with a non-matching profile, so it stays a browser login
        "[profile Shared-ADM]\nsso_session = main-org\nsso_account_id = 1\nsso_role_name = Admin\n\n"
        "[profile Platform-RO]\nsso_session = main-org\nsso_account_id = 1\nsso_role_name = ReadOnly\n\n"
        "[profile globex-ro]\nsso_session = globex\nsso_account_id = 2\nsso_role_name = ReadOnly\n")
    cfg = config.load()
    cfg["sso"]["main_profile"] = "Platform-RO"
    cfg["sso"]["manual_login"] = ["*-ADM"]
    calls = []
    monkeypatch.setattr(sso_refresh.shutil, "which", lambda _: "/usr/bin/aws")
    monkeypatch.setattr(sso_refresh.subprocess, "run", lambda cmd: calls.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(sso_refresh, "credentials_work", lambda p: False)
    args = argparse.Namespace(force=False, main_org=None, main_profile=None, list=False, manual_login=None)
    assert sso_refresh.run(args, cfg) == 0
    assert calls == [
        ["/usr/bin/aws", "sso", "login", "--profile", "Platform-RO"],
        ["/usr/bin/aws", "sso", "login", "--profile", "globex-ro", "--no-browser"],
        ["/usr/bin/aws", "sso", "login", "--profile", "Platform-ADM", "--no-browser"],
    ]
    # the command-line pattern replaces the setting
    calls.clear()
    args.manual_login = ["globex-*"]
    assert sso_refresh.run(args, cfg) == 0
    assert [c[4] for c in calls] == ["Platform-RO", "Platform-ADM", "globex-ro"]
    assert "--no-browser" not in calls[1]


def test_sso_refresh_manual_login_session_pattern_and_bad_type(isolated_aws, monkeypatch, caplog):
    import argparse
    from aws_toolbox import config
    from aws_toolbox.commands import sso_refresh
    (isolated_aws / "config").write_text(
        "[sso-session main-org]\nsso_start_url = https://acme.awsapps.com/start\nsso_region = us-east-1\n\n"
        "[sso-session main-org-adm]\nsso_start_url = https://acme.awsapps.com/start\nsso_region = us-east-1\n\n"
        "[profile admin]\nsso_session = main-org-adm\nsso_account_id = 1\nsso_role_name = Admin\n\n"
        "[profile Shared-ADM]\nsso_session = main-org\nsso_account_id = 1\nsso_role_name = Admin\n\n"
        "[profile ro]\nsso_session = main-org\nsso_account_id = 1\nsso_role_name = ReadOnly\n")
    cfg = config.load()
    cfg["sso"]["main_profile"] = "ro"
    cfg["sso"]["manual_login"] = "*-adm"  # a bare string is one pattern, not one per character
    calls = []
    monkeypatch.setattr(sso_refresh.shutil, "which", lambda _: "/usr/bin/aws")
    monkeypatch.setattr(sso_refresh.subprocess, "run", lambda cmd: calls.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(sso_refresh, "credentials_work", lambda p: False)
    args = argparse.Namespace(force=False, main_org=None, main_profile=None, list=False, manual_login=None)
    assert sso_refresh.run(args, cfg) == 0
    assert calls == [
        ["/usr/bin/aws", "sso", "login", "--profile", "ro"],
        ["/usr/bin/aws", "sso", "login", "--profile", "admin", "--no-browser"],
    ]
    cfg["sso"]["manual_login"] = ["*-ADM"]
    sso_refresh.run(args, cfg)
    assert "Shared-ADM match manual_login but share main-org's token" in caplog.text
    cfg["sso"]["manual_login"] = [1]
    assert sso_refresh.run(args, cfg) == 2


def test_sso_refresh_main_profile_without_account_still_picks_browser_login(isolated_aws, monkeypatch):
    import argparse
    from aws_toolbox import config
    from aws_toolbox.commands import sso_refresh
    (isolated_aws / "config").write_text(
        "[profile main-org]\nsso_start_url = https://acme.awsapps.com/start\nsso_region = us-east-1\n\n"
        "[profile acct]\nsso_start_url = https://acme.awsapps.com/start\nsso_region = us-east-1\n"
        "sso_account_id = 111111111111\nsso_role_name = R\n\n"
        "[profile other]\nsso_start_url = https://other.awsapps.com/start\nsso_region = us-east-1\n"
        "sso_account_id = 222222222222\nsso_role_name = R\n")
    calls = []
    monkeypatch.setattr(sso_refresh.shutil, "which", lambda _: "/usr/bin/aws")
    monkeypatch.setattr(sso_refresh.subprocess, "run", lambda cmd: calls.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(sso_refresh, "credentials_work", lambda p: False)
    args = argparse.Namespace(force=False, main_org=None, main_profile=None, list=False, manual_login=None)
    assert sso_refresh.run(args, config.load()) == 0
    # main-org names no account, so it isn't logged in, but its URL still opens the browser.
    assert calls == [
        ["/usr/bin/aws", "sso", "login", "--profile", "acct"],
        ["/usr/bin/aws", "sso", "login", "--profile", "other", "--no-browser"],
    ]
