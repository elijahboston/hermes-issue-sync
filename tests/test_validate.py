import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_hermes_plugins_validate_passes():
    hermes = shutil.which("hermes")
    if hermes is None:
        pytest.skip("hermes is not on PATH")
    proc = subprocess.run([hermes, "plugins", "validate", str(REPO_ROOT), "--json"],
                          capture_output=True, text=True, timeout=120)
    report = json.loads(proc.stdout)
    failed = [check for check in report["checks"] if not check["ok"]]
    assert report["ok"] and not failed, failed
    assert proc.returncode == 0, proc.stderr
