import os
import subprocess
from pathlib import Path


def test_mixed_prior_api_worker_images_are_refused_without_promotion(tmp_path):
    for name in [".env", "docker-compose.yml", "compose.image.yml"]:
        (tmp_path / name).write_text("synthetic fixture")
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import sys
args=sys.argv[1:]
if args[0]=='inspect':
    print('ghcr.io/albatross679/langgraph-customer-support-agent@sha256:'+('a' if args[1]=='api-id' else 'b')*64)
elif args[-3:]==['ps','-q','api']: print('api-id')
elif args[-3:]==['ps','-q','worker']: print('worker-id')
else: raise AssertionError('Promotion must not run with a mixed baseline')
""")
    docker.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(Path("deploy/ec2/promote.sh").resolve()),
            "ghcr.io/albatross679/langgraph-customer-support-agent@sha256:" + "c" * 64,
        ],
        env={
            **os.environ,
            "DEPLOY_APP_DIR": str(tmp_path),
            "PATH": str(tmp_path) + ":" + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "same verified immutable rollback image" in result.stderr
    assert "Promotion must not run" not in result.stderr
