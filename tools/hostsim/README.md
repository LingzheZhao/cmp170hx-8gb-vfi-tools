# Host simulation of the glitch timeline

The firmware's `glitch_cmd()` and its helpers, cut out of `firmware/pwmvid.c` (`extract.py`) and compiled on the host
against stubs (`stubs.h`, `harness.c`): a 1 µs clock, PMBus writes that take 47 µs, and the release alarm's handler run
from inside every time advance while interrupts are on, as the IRQ would. Every write, pin change and `printf` is
logged with its time, so two firmware versions can be compared shot by shot.

`check3.py` compares push2 and push3 (`firmware/0011`, `docs/PUSH3.md`): 16 push2-legal commands must give identical
output, write timelines and reset-pin timing; 11 overlap shots that push2 refuses must run on push3 with the dip and
restore writes at the intended times and the release exactly at t = 0 by the alarm; the setup-overwrite refusal must
stay.

```sh
cd pico-ga100vfi && git checkout e1ebd97 && git am /path/to/cmp170hx-8gb-vfi-tools/firmware/*.patch
/path/to/cmp170hx-8gb-vfi-tools/tools/hostsim/run.sh . HEAD~1      # HEAD = 0011 (push3), HEAD~1 = 0010 (push2)
```

The last line is `0 failures` (90 checks).
