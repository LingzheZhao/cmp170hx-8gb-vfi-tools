"""drive.py <runner> <tag> <seq-json>: run the runner against the fake ga100vfi package next to this file.
subprocess.call/run accept only the park command (logged as PARK; FAKE_PARK_RC sets its exit code); anything else,
setpci included, raises. time.sleep is a no-op."""
import os
import runpy
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ga100vfi import _bench  # noqa: E402


def fake_call(cmd, *args, **kwargs):
    if not any(str(x).endswith("park.py") for x in cmd):
        raise AssertionError("unexpected subprocess %r" % (cmd,))
    _bench.log("PARK")
    return int(os.environ.get("FAKE_PARK_RC", "0"))


def fake_run(cmd, *args, **kwargs):
    rc = fake_call(cmd)
    return subprocess.CompletedProcess(cmd, rc, stdout="park: rail held 787 mV, PEX_RST, config 0x20c210de\n", stderr="")


subprocess.call = fake_call
subprocess.run = fake_run
time.sleep = lambda s: None
runner = sys.argv[1]
sys.argv = [runner] + sys.argv[2:]
runpy.run_path(runner, run_name="__main__")
