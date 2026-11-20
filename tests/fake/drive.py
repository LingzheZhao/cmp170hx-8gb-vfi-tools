"""drive.py <runner> <tag> <seq-json>: run the runner against the fake ga100vfi package next to this file.
subprocess.call/run accept only the park command (any command naming park.py) (logged as PARK; FAKE_PARK_RC sets its exit code; parking while
the runner still holds the tty fails the run) and a read-only `lspci -vv -s <port>` (logged as LSPCI); anything else,
setpci included, raises. time.sleep is a no-op."""
import os
import runpy
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ga100vfi import _bench  # noqa: E402


def fake_call(cmd, *args, **kwargs):
    if any(str(x).endswith("park.py") for x in cmd):
        if _bench.open_cards[0]:
            _bench.log("PARK WITH TTY OPEN")
            raise AssertionError("park started while the runner holds the serial port")
        _bench.log("PARK")
        return int(os.environ.get("FAKE_PARK_RC", "0"))
    raise AssertionError("unexpected subprocess %r" % (cmd,))


def fake_run(cmd, *args, **kwargs):
    if list(cmd[:2]) == ["lspci", "-vv"] and cmd[2] == "-s":
        _bench.log("LSPCI " + cmd[3])
        return subprocess.CompletedProcess(cmd, 0, stdout="\tLnkCtl:\tASPM Disabled\n\tLnkSta:\tSpeed 2.5GT/s, Width x8\n"
                                           "\tDevSta:\tCorrErr- NonFatalErr-\n\tUESta:\tDLP- SDES-\n", stderr="")
    rc = fake_call(cmd)
    return subprocess.CompletedProcess(cmd, rc, stdout="park: rail held 787 mV, PEX_RST, config 0x20c210de\n", stderr="")


subprocess.call = fake_call
subprocess.run = fake_run
time.sleep = lambda s: None
runner = sys.argv[1]
sys.argv = [runner] + sys.argv[2:]
runpy.run_path(runner, run_name="__main__")
