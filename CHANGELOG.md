# Changelog

All notable changes to this fork (`nrbrt/dbus-lynx-distributor`) are documented
here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This file starts at the point of forking from
[`twam/dbus-lynx-distributor`](https://github.com/twam/dbus-lynx-distributor).
Earlier history lives in the upstream repository's commit log.

## [Unreleased]

### Added

- **Optional Bosch BME280 environmental sensor support** sharing the FT232H
  I²C bus with the Lynx Distributors. Detects the sensor at 0x76 or 0x77
  (configurable, auto-probe by default) and publishes a second
  `com.victronenergy.temperature.<serial>_bme280` VeDbusService with
  `/Temperature` (°C), `/Humidity` (%RH), and `/Pressure` (hPa) so it
  appears as a sensor tile in the Venus OS GUI alongside the Lynx
  Distributor service.
  - New module `dbus_lynx_distributor/bme280.py` with pure compensation
    maths (Bosch BST-BME280-DS002 §8.1: integer formula for humidity,
    double for temperature/pressure) plus a thin `Bme280Reader` wrapper
    around a pyftdi I²C port.
  - Helper `parse_address_config()` accepts `auto` / `0x76` / `0x77` /
    `disabled`, validates range, raises on unparseable input.
  - Helper `detect_bme280()` probes candidate addresses and returns an
    initialised reader or `None`.
  - **`bme280TemperatureType` config option** (default `2` = generic). The
    `/TemperatureType` D-Bus path determines whether the Cerbo's DVCC
    treats the reading as a battery temperature (and applies
    temperature-compensated charge-voltage adjustment) or as inert
    monitoring data. Defaults to generic so cabin/bilge air doesn't end
    up steering the charger; the README has a warning explaining why
    `0 = battery` is dangerous unless the BME280 is physically bonded
    to a battery cell.
  - Invalid integers in this option fall back to the default with a
    warning rather than crashing the service.
  - 22 new pytest cases (compensation against Bosch reference vectors,
    config parsing, detection logic via fake I²C controller) — all
    hardware-free.
  - DeviceInstance offset by +100 to avoid collision with the Lynx
    instance number.
  - Sensor errors are logged but never break Lynx polling; on read
    failure the sensor service is marked Disconnected and the next
    Lynx tick continues unaffected.
  - README: new section under "How this fork differs from upstream",
    a wiring table, all three `bme280*` config keys, and a "Visibility &
    integration" subsection covering the Cerbo local GUI, VRM Portal,
    MQTT-on-LAN bridging (the route to Home Assistant/Node-RED and the
    only way the chip's `/Pressure` becomes consumer-side visible),
    Cerbo Alarms, and NMEA2000 PGN bridging notes.
- **`pyproject.toml`** (PEP 621) with full project metadata: name,
  version, description, authors, license, classifiers, deps, optional
  `dev` extras (pytest, flake8), URLs, and a `dbus-lynx-distributor`
  console-script entry point. Pytest configuration moved here from
  the standalone `pytest.ini`. The Cerbo continues to launch via
  `python -m dbus_lynx_distributor` (no install step needed); on a
  developer machine `pip install -e '.[dev]'` gives you the script
  on PATH plus all test deps.
- **Type hints** on every public method of `Ftdi`,
  `DbusLynxDistributorService`, and `Application` (parameters and
  return types). The decoder module already had type hints from the
  start. Lets editors and mypy/pyright catch wiring mistakes that the
  test suite can't.
- README "Development" section explaining the venv + install + test
  loop.
- **GitHub Actions workflow** (`.github/workflows/test.yml`) running
  `flake8 --select=E9,F` plus the full `pytest` suite on every push to
  master and every PR, against Python 3.11 and 3.12. Pulls deps from
  `requirements.txt` + `requirements-dev.txt`. Uses `actions/checkout@v4`
  with `submodules: recursive` so the `ext/velib_python` submodule comes
  along.
- `requirements-dev.txt` for dev/CI deps (pytest, flake8) — kept
  separate from `requirements.txt` so the Cerbo doesn't pull pytest at
  service startup.
- **Pytest test suite** — 65 tests across five files:
  - `tests/test_decoder.py` — I2C status-byte decoding (per-fuse,
    all-blown, no-bus-power, uninstalled-fuse, property-style sweep
    across all 256 byte values × 16 install masks).
  - `tests/test_ftdi.py` — `Ftdi` I/O wrapper contract (NACK sentinel,
    zero-byte-vs-NACK distinction, close idempotency).
  - `tests/test_service.py` — service flow (`_publish_distributor`,
    `_invalidate_all`, full `_update` cycle including the addr-NACK
    and data-NACK paths that used to crash, USBError recovery,
    upside-down fuse-index swap, `close()` semantics).
  - `tests/test_main.py` — SIGTERM/SIGINT handler, `_shutdown()`
    continuing after a service `close()` raises.
  - `tests/test_bme280.py` — BME280 driver: compensation against Bosch
    reference vectors (temperature, pressure, humidity), 0-100% clamp,
    division-by-zero safety, calibration register parsing (incl. 12-bit
    packed `dig_H4`/`dig_H5`), raw measurement parsing, address-config
    parser, and `detect_bme280()` with a fake I²C controller covering
    found-at-default, fall-through-to-alt, disabled, absent, and
    explicit-pinned scenarios.
  - `tests/conftest.py` stubs `gi`, `vedbus`, `dbus`, and
    `settingsdevice` in `sys.modules` when those Cerbo-side libraries
    aren't installed locally, so the whole suite runs on any
    developer machine with `pyftdi` and `pyusb`.
- **`dbus_lynx_distributor/decoder.py`** — dependency-free module
  containing the pure bit-decoding logic and all status/alarm/protocol
  constants. The service module imports from here.
- **`requirements.txt`** with `pyftdi~=0.57.1` and `pyusb~=1.3.1` so a
  breaking upstream release no longer silently crashes the service at
  next reboot. `service/run` only re-runs `pip install` when
  `requirements.txt` actually changed (md5 hashed in
  `.requirements.installed`).
- **Graceful shutdown** via `GLib.unix_signal_add` for SIGTERM/SIGINT.
  `DbusLynxDistributorService.close()` cancels the GLib poll timer and
  calls `Ftdi.close()` which terminates the pyftdi I2C controller.
  `Ftdi.close()` is idempotent and safe when `init_i2c()` never ran.
- Named constants in place of magic numbers: `DISTRIBUTOR_STATUS_*`,
  `FUSE_STATUS_*`, `ALARM_*`, `CONNECTED_*`, `LYNX_I2C_BASE_ADDR`,
  `BIT_NO_BUS_POWER`, `BIT_FUSE0_BLOWN`, `NUM_DISTRIBUTORS`,
  `FUSES_PER_DISTRIBUTOR`, `POLL_INTERVAL_MS`.

### Changed

- **`__version__` bumped from `0.0.1` to `0.2.0`** to reflect the
  substantive fork changes (USB error recovery, NACK sentinel,
  graceful shutdown, named constants, test suite). Surfaces on dbus
  as `/Mgmt/ProcessVersion` so support tools see something useful.
- `/Connected` is now actively maintained: flipped to `0` on
  USBError-induced invalidation, restored to `1` on successful
  re-init or the next successful poll. Previously hard-coded to `1`
  at construction and never updated, so it lied during outages.
- `_update()` now delegates byte-decoding to `decode_distributor_state()`
  and only handles dbus publishing. The 4-level-nested if/else from
  the upstream code is gone.
- `pyftdi` I2C retry count raised from 1 to 3, so transient bus
  glitches recover before propagating an `I2cNackError` to the caller.
  Each retry costs <10 ms.
- `read_byte_and_send_nak()` now returns a `NACK` sentinel instead of
  `None` on slave-NACK or zero-length read. This lets callers
  distinguish *no-response* from a valid `0x00` byte. The previous
  contract conflated both cases.
- `print()` statements replaced with `logging` calls so error output
  respects the `-v` verbosity flag and ends up in the journal
  alongside service logs.

### Removed

- `ServicePath` dataclass in `dbus_lynx_distributor_service.py` —
  defined but never used anywhere in the codebase. Likely an
  abandoned-refactor artifact from upstream.
- Standalone `pytest.ini` removed; configuration consolidated under
  `[tool.pytest.ini_options]` in `pyproject.toml`.
- Dead code: `_thread.daemon = True` from `__main__.py`. `_thread` is
  the low-level threading API and the main thread cannot be a daemon
  thread by definition. The "allow the program to quit" comment was
  misleading; SIGTERM handling now does the actual work.
- Debug `__del__` method that printed `'Good Bye'` on garbage-collection.
- Runtime daemontools state (`service/supervise/{lock,status}`,
  `service/log/supervise/{lock,status}`) removed from git tracking and
  added to `.gitignore` alongside `.pytest_cache/` and
  `.requirements.installed`.

### Fixed

- **VID/PID variable assignment and config field names were swapped.**
  In `__main__.py` the variable `pid` read the config option `vid` and
  vice versa, and `config.sample.ini` had the same swap on the field
  names. Both errors cancelled out, so existing installs accidentally
  worked, but anyone fixing either side in isolation would break
  device discovery. Both sides are now corrected together — the
  values themselves are unchanged (`vid=0x0403`, `pid=0xD4F8`).
- **`_update()` no longer stops the GLib timer on `USBError`.**
  Previously a single transient USB error (cable jiggle, bus glitch,
  EMI burst) caused the exception handler to return `False`, which
  dropped the GLib timer permanently and froze the dbus values until
  the service was manually restarted. The handler now invalidates
  installed distributors as Communications Lost, sets a re-init flag,
  and keeps the timer alive so the service can recover by re-running
  `Ftdi.init_i2c()` on the next tick.
- **Race in `_update()` when `read_byte_and_send_nak` returned `None`.**
  The old code did `state & 0b00000010` directly on the return value.
  If the slave ACKed the address byte but NACKed the data byte (a real
  race under EMI), the return was `None` and the bitwise AND raised
  `TypeError` — which was *not* caught by the surrounding
  `except USBError`, so the GLib timer died with an uncaught
  exception. The function now returns a `NACK` sentinel that is
  explicitly handled at the call site as Communications Lost.

### Internal

- Removed duplicate `Ftdi` import in `ftdi.py` (the one on line 2 was
  masked by the local class definition on line 10) and unused
  `FtdiLogger` / `time` imports.
- Moved `logging.getLogger('pyftdi').setLevel(logging.ERROR)` from
  inside the class body to module level.
- Initialised `self.i2c = None` in `Ftdi.__init__` so attribute access
  is safe before `init_i2c()` runs.
- Stripped trailing whitespace.

### Migration notes

Existing `config.ini` files copied from the upstream `config.sample.ini`
continue to work unchanged: the field names there were swapped too, so
the *values* sitting under each name are still correct. Only
configurations that intentionally renamed the fields against their
meaning will need to swap `vid` and `pid`.
