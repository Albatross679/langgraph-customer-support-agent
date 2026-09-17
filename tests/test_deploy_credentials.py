import os
import subprocess
from pathlib import Path

import pytest

COMMON_SH = Path(__file__).parents[1] / "deploy" / "ec2" / "common.sh"


def run_shell(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text(
        "export PATH=\"${CMAKE_PREFIX_PATH}:${PYTHONPATH}\"\n"
        "export AWS_ACCESS_KEY_ID=synthetic-access-key\n"
        "export AWS_SECRET_ACCESS_KEY=synthetic-secret-key\n"
    )
    environment = os.environ.copy()
    environment.update({"HOME": str(home)})
    environment.pop("AWS_ACCESS_KEY_ID", None)
    environment.pop("AWS_SECRET_ACCESS_KEY", None)
    environment.pop("CMAKE_PREFIX_PATH", None)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        ["bash", "-c", script, "bash", str(COMMON_SH)],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


@pytest.mark.parametrize("errexit", [False, True])
@pytest.mark.parametrize("nounset", [False, True])
def test_source_aws_credentials_loads_zshrc_and_restores_shell_options(
    tmp_path: Path, errexit: bool, nounset: bool
) -> None:
    result = run_shell(
        tmp_path,
        f"""
source "$1"
set +e +u
{'set -e' if errexit else 'set +e'}
{'set -u' if nounset else 'set +u'}
source_aws_credentials
[[ $- == *e* ]] && e=on || e=off
[[ $- == *u* ]] && u=on || u=off
printf 'credentials=%s:%s options=%s:%s\\n' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" "$e" "$u"
""",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "credentials=synthetic-access-key:synthetic-secret-key "
        f"options={'on' if errexit else 'off'}:{'on' if nounset else 'off'}\n"
    )


@pytest.mark.parametrize("nounset", [False, True])
def test_source_aws_credentials_reports_missing_credentials_and_restores_nounset(
    tmp_path: Path, nounset: bool
) -> None:
    result = run_shell(
        tmp_path,
        f"""
source "$1"
set +e
{'set -u' if nounset else 'set +u'}
printf 'export PATH="${{CMAKE_PREFIX_PATH}}:${{PYTHONPATH}}"\\n' > "$HOME/.zshrc"
(source_aws_credentials)
status=$?
printf 'status=%s\\n' "$status"
""",
    )

    assert result.returncode == 0
    assert "AWS credentials are missing after loading ~/.zshrc." in result.stderr
    assert "AWS_ACCESS_KEY_ID must be exported in ~/.zshrc" in result.stderr
    assert result.stdout.startswith("status=")
    assert result.stdout != "status=0\n"
