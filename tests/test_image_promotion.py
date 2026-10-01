"""Execute the promotion script using local command adapters; no SSH or EC2 writes."""

import os
import subprocess
from pathlib import Path

import pytest

PREFIX = "ghcr.io/albatross679/langgraph-customer-support-agent@sha256:"
OLD = PREFIX + "a" * 64
NEW = PREFIX + "b" * 64


def adapter(tmp_path, name, text):
    path = tmp_path / name
    path.write_text("#!/usr/bin/env python3\n" + text)
    path.chmod(0o755)


@pytest.mark.parametrize("failure", ["", "pull", "init", "health", "rollback-health"])
def test_promotion_and_rollback(tmp_path, failure):
    for name in [".env", "docker-compose.yml", "compose.image.yml"]:
        (tmp_path / name).write_text("test-fixture")
    adapter(
        tmp_path,
        "docker",
        """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps({'args': args, 'image':os.environ.get('APP_IMAGE')})+'\\n')
if args[0]=='inspect': print(os.environ['OLD'])
elif args[-3:]==['ps','-q','api']: print('existing-api')
elif 'pull' in args and os.environ['FAILURE']=='pull': sys.exit(1)
elif 'run' in args and os.environ['FAILURE']=='init': sys.exit(1)
elif 'up' in args: Path(os.environ['CURRENT']).write_text(os.environ['APP_IMAGE'])
""",
    )
    adapter(
        tmp_path,
        "curl",
        """import os, sys
from pathlib import Path
current=Path(os.environ['CURRENT']).read_text()
if os.environ['FAILURE']=='rollback-health' or (os.environ['FAILURE']=='health' and current==os.environ['NEW']): sys.exit(1)
""",
    )
    adapter(tmp_path, "sleep", "pass\n")
    current = tmp_path / "current"
    current.write_text(OLD)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DEPLOY_APP_DIR": str(tmp_path),
        "OLD": OLD,
        "NEW": NEW,
        "FAILURE": failure,
        "CURRENT": str(current),
        "CALLS": str(calls),
    }
    result = subprocess.run(
        ["bash", str(Path("deploy/ec2/promote.sh").resolve()), NEW],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if not failure else 1), result.stderr
    assert current.read_text() == (NEW if not failure else OLD)
    if not failure:
        assert (tmp_path / "previous-image.txt").read_text().strip() == OLD
        assert (tmp_path / "current-image.txt").read_text().strip() == NEW
    else:
        assert not (tmp_path / "current-image.txt").exists()
        assert "restoring prior immutable image" in result.stderr
    if failure == "rollback-health":
        assert "operator intervention required" in result.stderr


def test_unversioned_image_refused_before_docker(tmp_path):
    adapter(tmp_path, "docker", "raise AssertionError('docker must not be invoked')\n")
    result = subprocess.run(
        ["bash", str(Path("deploy/ec2/promote.sh").resolve()), "image:latest"],
        env={
            **os.environ,
            "DEPLOY_APP_DIR": str(tmp_path),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
        capture_output=True,
    )
    assert result.returncode == 2
    assert b"docker must not be invoked" not in result.stderr
