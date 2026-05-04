# Changelog

All notable changes to this fork (`nrbrt/dbus-lynx-distributor`) are documented
here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This file starts at the point of forking from
[`twam/dbus-lynx-distributor`](https://github.com/twam/dbus-lynx-distributor).
Earlier history lives in the upstream repository's commit log.

## [Unreleased]

### Fixed

- **VID/PID variable assignment and config field names were swapped.**
  In `__main__.py` the variable `pid` read the config option `vid` and vice
  versa, and `config.sample.ini` had the same swap on the field names. Both
  errors cancelled out, so existing installs accidentally worked, but anyone
  fixing either side in isolation would break device discovery. Both sides are
  now corrected together — the values themselves are unchanged
  (`vid=0x0403`, `pid=0xD4F8`).

- **`_update()` no longer stops the GLib timer on `USBError`.** Previously a
  single transient USB error (cable jiggle, bus glitch, EMI burst) caused the
  exception handler to return `False`, which dropped the GLib timer permanently
  and froze the dbus values until the service was manually restarted. The
  handler now invalidates installed distributors as Communications Lost, sets
  a re-init flag, and keeps the timer alive so the service can recover by
  re-running `Ftdi.init_i2c()` on the next tick.

- **Race in `_update()` when `read_byte_and_send_nak` returned `None`.** The
  old code did `state & 0b00000010` directly on the return value. If the slave
  ACKed the address byte but NACKed the data byte (a real race under EMI), the
  return was `None` and the bitwise AND raised `TypeError` — which was *not*
  caught by the surrounding `except USBError`, so the GLib timer died with an
  uncaught exception. The function now returns a `NACK` sentinel that is
  explicitly handled at the call site as Communications Lost.

### Changed

- `pyftdi` I2C retry count raised from 1 to 3, so transient bus glitches
  recover before propagating an `I2cNackError` to the caller. Each retry costs
  <10 ms.
- `read_byte_and_send_nak()` now returns a `NACK` sentinel instead of `None`
  on slave-NACK or zero-length read. This lets callers distinguish
  *no-response* from a valid `0x00` byte. The previous contract conflated
  both cases.
- `print()` statements replaced with `logging` calls so error output respects
  the `-v` verbosity flag and ends up in the journal alongside service logs.

### Removed

- Debug `__del__` method that printed `'Good Bye'` on garbage-collection.

### Internal

- Removed duplicate `Ftdi` import in `ftdi.py` (the one on line 2 was masked
  by the local class definition on line 10) and unused `FtdiLogger` / `time`
  imports.
- Moved `logging.getLogger('pyftdi').setLevel(logging.ERROR)` from inside the
  class body to module level.
- Initialised `self.i2c = None` in `Ftdi.__init__` so attribute access is safe
  before `init_i2c()` runs.
- Stripped trailing whitespace.

### Migration notes

Existing `config.ini` files copied from the upstream `config.sample.ini`
continue to work unchanged: the field names there were swapped too, so the
*values* sitting under each name are still correct. Only configurations that
intentionally renamed the fields against their meaning will need to swap
`vid` and `pid`.
