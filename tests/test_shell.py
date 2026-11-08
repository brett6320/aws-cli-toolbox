import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "shell" / "aws-toolbox.functions"


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_assume_role_wrapper_rejects_format(shell, tmp_path):
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    # Fake aws-toolbox that would emit something dangerous if ever called.
    fake = tmp_path / "aws-toolbox"
    fake.write_text("#!/bin/sh\necho 'export PWNED=1'\n")
    fake.chmod(0o755)
    cmd = f'PATH={tmp_path}:$PATH; source {SCRIPT}; aws-assume-role Admin --format fish; echo "rc=$? pwned=${{PWNED:-}}"'
    out = subprocess.run([shell, "-c", cmd], capture_output=True, text=True)
    assert "rc=2 pwned=" in out.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_assume_role_wrapper_does_not_eval_on_failure(shell, tmp_path):
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    fake = tmp_path / "aws-toolbox"
    fake.write_text("#!/bin/sh\necho 'export PWNED=1'\nexit 1\n")
    fake.chmod(0o755)
    cmd = f'PATH={tmp_path}:$PATH; source {SCRIPT}; aws-assume-role Admin; echo "rc=$? pwned=${{PWNED:-}}"'
    out = subprocess.run([shell, "-c", cmd], capture_output=True, text=True)
    assert "rc=1 pwned=" in out.stdout
