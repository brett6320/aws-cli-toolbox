import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
OLD = """aws-clear-identity () {
  unset AWS_PROFILE
}

aws-assume-role() {
  echo old
}
_aws_sso_session_ok() { :; }
"""
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    # Isolate from the user's signing setup and XDG git config.
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "tag.gpgsign", "GIT_CONFIG_VALUE_1": "false",
}


@pytest.fixture
def env(tmp_path):
    """Throwaway git repo (copy of this one, committed on main) + install dirs."""
    repo = tmp_path / "repo"
    # Only files git tracks or would track: never ignored local files (e.g. a
    # symlinked personal toolbox.toml) or the venv.
    files = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           check=True, capture_output=True, text=True).stdout.split("\0")
    for rel in filter(None, files):
        src = REPO / rel
        if src.is_file():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, repo / rel)
    e = {**os.environ, **GIT_ENV, "XDG_CONFIG_HOME": str(tmp_path / "xdg"), "TMPDIR": str(tmp_path),
         "FUNCTIONS_DIR": str(tmp_path / "fd"), "XDG_STATE_HOME": str(tmp_path / "state"),
         "AWS_TOOLBOX_SKIP_UV": "1", "AWS_TOOLBOX_CONFIG": str(tmp_path / "toolbox.toml")}
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], env=e, check=True, capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    (tmp_path / "fd").mkdir()
    return tmp_path, repo, e, git


def _install(env, *args):
    tmp, repo, e, _ = env
    return subprocess.run(["/bin/bash", str(repo / "install.sh"), *args], capture_output=True, text=True, env=e)


def test_dry_run_changes_nothing(env):
    tmp = env[0]
    (tmp / "fd" / "aws-shell.functions").write_text(OLD)
    out = _install(env)
    assert out.returncode == 0, out.stderr
    assert "[DRY-RUN] retiring" in out.stdout
    assert sorted(p.name for p in (tmp / "fd").iterdir()) == ["aws-shell.functions"]
    assert not (tmp / "state").exists()
    assert not (tmp / "toolbox.toml").exists()


def test_execute_retires_with_backup_and_writes_config(env):
    tmp, repo = env[0], env[1]
    fd = tmp / "fd"
    (fd / "aws-shell.functions").write_text(OLD)
    (fd / "other.functions").write_text("vid() { :; }\n")
    out = _install(env, "--execute")
    assert out.returncode == 0, out.stderr
    assert not (fd / "aws-shell.functions").exists()
    assert (fd / "other.functions").exists()
    backups = list((fd / ".backup").glob("aws-shell.functions.*.bak"))
    assert len(backups) == 1 and backups[0].read_text() == OLD
    assert "Rollback" in out.stdout and str(backups[0]) in out.stdout
    assert (fd / "aws-toolbox.functions").read_text() == (repo / "shell" / "aws-toolbox.functions").read_text()
    assert "[sync]" in (tmp / "toolbox.toml").read_text()
    hooks = [p.name for p in (repo / ".git" / "hooks").iterdir() if not p.name.endswith(".sample")]
    assert hooks == []  # no automatic sync


def test_installs_ref_not_working_tree_and_status_tracks_main(env):
    tmp, repo, _, git = env
    target = tmp / "fd" / "aws-toolbox.functions"
    assert _install(env, "--execute").returncode == 0
    installed = target.read_text()
    assert _install(env, "--status").returncode == 0

    # Work on a branch plus uncommitted edits: install from main is unaffected.
    git("checkout", "-q", "-b", "feature")
    (repo / "shell" / "aws-toolbox.functions").write_text("# feature work\n")
    git("commit", "-qam", "wip")
    (repo / "shell" / "aws-toolbox.functions").write_text("# uncommitted\n")
    out = _install(env, "--execute")
    assert out.returncode == 0, out.stderr
    assert target.read_text() == installed
    assert "already up to date" in out.stdout

    # main moves: --status reports it, but nothing changes until --execute.
    git("checkout", "-q", "-f", "main")
    (repo / "shell" / "aws-toolbox.functions").write_text(installed + "# v2\n")
    git("commit", "-qam", "v2")
    status = _install(env, "--status")
    assert status.returncode == 3 and "sync available" in status.stdout and "v2" in status.stdout
    assert target.read_text() == installed

    out = _install(env, "--execute")
    assert out.returncode == 0, out.stderr
    assert target.read_text().endswith("# v2\n")
    assert [p.read_text() for p in (tmp / "fd" / ".backup").glob("aws-toolbox.functions.*.bak")] == [installed]
    assert _install(env, "--status").returncode == 0


def test_status_before_install(env):
    out = _install(env, "--status")
    assert out.returncode == 1 and "not installed" in out.stdout


def test_missing_ref_fails(env):
    out = _install(env, "--ref", "nope")
    assert out.returncode == 1 and "no ref 'nope'" in out.stderr


def test_mixed_file_blocks_install(env):
    fd = env[0] / "fd"
    (fd / "mixed.functions").write_text(OLD + "my-thing() { :; }\n")
    out = _install(env, "--execute")
    assert out.returncode == 1
    assert "keep:       my-thing" in out.stderr
    assert (fd / "mixed.functions").read_text() == OLD + "my-thing() { :; }\n"
    assert not (fd / "aws-toolbox.functions").exists()


def _install_pkg(env, *args, **extra):
    """Dry-run that reaches the console-scripts step (not skipped)."""
    tmp, repo, e, _ = env
    e = {k: v for k, v in e.items() if k not in ("AWS_TOOLBOX_SKIP_UV", "PIP_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX_URL")}
    e.update(HOME=str(tmp / "home"), XDG_DATA_HOME=str(tmp / "data"), **extra)
    return subprocess.run(["/bin/bash", str(repo / "install.sh"), *args], capture_output=True, text=True, env=e)


def test_index_url_is_used_and_its_credentials_hidden(env):
    out = _install_pkg(env, "--installer", "uv", "--index-url", "https://user:s3cret@mirror.example/simple")
    assert out.returncode == 0, out.stderr
    assert "package index: https://***@mirror.example/simple" in out.stdout
    assert "s3cret" not in out.stdout + out.stderr


def test_pip_index_url_env_is_passed_to_uv(env):
    out = _install_pkg(env, "--installer", "uv", PIP_INDEX_URL="https://mirror.example/simple")
    assert out.returncode == 0, out.stderr
    assert "package index: https://mirror.example/simple" in out.stdout


def test_pip_installer_uses_a_private_venv(env):
    tmp = env[0]
    out = _install_pkg(env, "--installer", "pip")
    assert out.returncode == 0, out.stderr
    assert f"{tmp}/data/aws-cli-toolbox/venvs/" in out.stdout
    assert f"{tmp}/home/.local/bin/aws-sync-profiles" in out.stdout
    assert not (tmp / "data").exists() and not (tmp / "home" / ".local").exists()


def test_bad_installer_is_rejected(env):
    assert _install(env, "--installer", "brew").returncode == 2


def test_uv_index_setting_is_shared_with_pip(env):
    out = _install_pkg(env, "--installer", "pip", UV_DEFAULT_INDEX="https://mirror.example/simple")
    assert out.returncode == 0, out.stderr
    assert "package index: https://mirror.example/simple" in out.stdout
