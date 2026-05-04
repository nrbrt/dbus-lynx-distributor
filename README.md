dbus-lynx-distributor
===

A Venus OS 'plugin' to read out Victron's [Lynx Distributor](https://www.victronenergy.de/dc-distribution-systems/lynx-distributor) without a [Lynx Smart BMS](https://www.victronenergy.de/battery-management-systems/lynx-smart-bms). It requires a custom adapter hardware (see below) and emulates a battery (without SoC, ...) on DBUS just providing the distributor information.

*Disclaimer*

This plugin comes without any guarantees or warranties. Use it at your own risk. I only tested it on my hardware setup.

---

## How this fork differs from upstream

This is a substantively reworked fork of [twam/dbus-lynx-distributor](https://github.com/twam/dbus-lynx-distributor). Beyond the original feature set it adds bug fixes, runtime resilience, packaging, CI, and a real test suite. The full list lives in [`CHANGELOG.md`](CHANGELOG.md); the highlights:

### ⚠️ Breaking change (config field names)

The upstream `config.sample.ini` had the `vid` and `pid` field names swapped against their values:

```ini
# upstream — field names are inverted
pid = 0x0403   # actually the FTDI vendor ID
vid = 0xD4F8   # actually the Victron product ID
```

This fork fixes both the field names *and* the corresponding variable assignment in `__main__.py`. The two upstream errors cancelled out, so unmodified upstream installs accidentally worked. **Configurations that were copied from upstream verbatim continue to work without changes** because the values stayed the same — only the names were corrected. Configurations that were intentionally written against the *meaning* of the field names (rare) need the `vid` and `pid` lines swapped.

### Reliability fixes (the reason this fork exists)

- **Transient USB errors no longer kill the service permanently.** Upstream's `_update()` returned `False` from its `except USBError` branch, which causes `GLib.timeout_add` to drop the poll timer for good. A single cable jiggle, EMI burst, or bus glitch would freeze the dbus values until the service was manually restarted. This fork invalidates the affected distributors as Communications Lost, sets a re-init flag, and recovers on the next tick by calling `Ftdi.init_i2c()` again.
- **NACK on a data byte no longer crashes with `TypeError`.** Upstream did `state & 0b00000010` directly on the return of `read_byte_and_send_nak()`. If the slave ACKed the address byte but NACKed the data byte (a real race under EMI), the return was `None` and the bitwise AND raised — and the surrounding `except USBError` did *not* catch it, so the GLib timer died with an uncaught exception. This fork introduces a `NACK` sentinel that the call site handles explicitly as Communications Lost.
- **`/Connected` is now actively maintained.** Upstream hard-coded it to `1` at construction and never updated it, so it stayed `1` even during USB outages. This fork flips it to `0` on invalidation and back to `1` on successful poll.
- **I²C retry count raised from 1 to 3** so transient bus glitches recover before propagating to the caller (~10 ms per retry).
- **Graceful shutdown on SIGTERM/SIGINT** via `GLib.unix_signal_add` so `systemctl restart` no longer leaves a stuck FTDI handle or a half-finished I²C transaction.

### Quality improvements

- **43-test pytest suite** (`tests/`) with mocked pyftdi I/O, including regression tests for both reliability bugs above and a property-style sweep across all 256 status-byte values. Runs in under 0.2 s on a Pi 5; doesn't require Cerbo libraries thanks to `tests/conftest.py` stubs.
- **GitHub Actions CI** (`.github/workflows/test.yml`) running flake8 + pytest on Python 3.11 and 3.12 for every push and PR.
- **Pinned dependencies** (`requirements.txt`: `pyftdi~=0.57.1`, `pyusb~=1.3.1`). The upstream service script ran `pip install pyftdi` with no version constraint, so a breaking upstream release would have crashed the service silently at next reboot.
- **Pure decoder module** (`dbus_lynx_distributor/decoder.py`) — bit-decoding logic extracted from the dbus-publishing layer for testability and clarity. The 4-level-nested if/else from upstream is gone.
- **Type hints** on every public method of `Ftdi`, `DbusLynxDistributorService`, and `Application`.
- **PEP 621 packaging** (`pyproject.toml`) with a `dbus-lynx-distributor` console-script entry point. Local dev install via `pip install -e '.[dev]'`.
- **Dead code removed**: `_thread.daemon = True` (no-op), debug `__del__` print, and the unused `ServicePath` dataclass.

### What did NOT change

- The hardware adapter, wiring, and the I²C status-byte protocol described below — the entire "Hardware" section of this README is unchanged from upstream and reflects current behaviour.
- The Venus OS / Cerbo install procedure (`bash install.sh` from `/data/dbus-lynx-distributor`).
- The dbus path layout and the values published on each path.

---

## Development

The repository is a standard PEP 621 Python project. To set up a dev environment:

```bash
git clone --recurse-submodules https://github.com/nrbrt/dbus-lynx-distributor.git
cd dbus-lynx-distributor
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

`pip install -e '.[dev]'` installs `pyftdi` and `pyusb` from `requirements.txt` plus `pytest`/`flake8` from the `dev` extra. The Cerbo-side libraries (`gi`, `vedbus`, `dbus`, `settingsdevice`) are stubbed in `tests/conftest.py` so the test suite runs on any developer machine.

CI (`.github/workflows/test.yml`) runs the same suite on every push and PR against Python 3.11 and 3.12.

## Hardware

### Background

The Lynx Distributor has a RJ10 connector for daisy-chaining distributors and connecting them to a Lynx Smart BMS. According to the [manual](https://www.victronenergy.com/upload/documents/Lynx_Distributor/24531-Lynx_Distributor_Manual-pdf-en.pdf), section 3.3 the PCB can be also used without data transfer by providing 5 V.

I reversed engineered the data communication on pins 2 and 3 of the connector and came to the following conclusions:

It's an [I²C](https://de.wikipedia.org/wiki/I²C) interface. The cable has the following mapping:

```
Pin 1 - yellow - 5V
Pin 2 - green - SDA
Pin 3 - red - SCL
Pin 4 - black - GND
```

A pull-up resistors (I used 10 kΩ) is required on SDA/SCL to 5V.

The device answers on I²C address `0b000001AA` where `AA` is set by the address jumper. So in default configuration the address is `0x8`.

Any reads on the device will always return the same status byte with the following meaning:

```
0b00000000 - Everything allright (center LED is green)
0b00000010 - No supply on bus bar (center LED is orange)
0b00010000 - Fuse 1 open (center LED red, first fuse LED red)
0b00100000 - Fuse 2 open (center LED red, second fuse LED red)
0b01000000 - Fuse 3 open (center LED red, third fuse LED red)
0b10000000 - Fuse 4 open (center LED red, forth fuse LED red)
```

If multiple fuses are open multiple bits are set (just or the values).

### Adapters

As [Cerbo GX](https://www.victronenergy.de/communication-centres/cerbo-gx) doesn't provide any I²C interfaces an adapter is needed. I choose the [C232HM-EDHSL-0](https://ftdichip.com/products/c232hm-edhsl-0/) from FTDI as it is 'premade' available. I personally used the 3.3V variant ([C232HM-DDHSL-0](https://ftdichip.com/products/c232hm-ddhsl-0/)) which provides less than the according to the manual required 5V, but it works on my device.

Any other FTDI adapter/board using a [FT232H](https://ftdichip.com/products/ft232hq/) chip should work. The bigger variants [FT2232H](https://ftdichip.com/products/ft2232hq/) and [FT4232H](https://ftdichip.com/products/ft4232hq/) won't work without adaptions in the software as they don't feature the open-drain option for the pins (see comment on page 4 of FTDI's [AN 255](https://www.ftdichip.com/Support/Documents/AppNotes/AN_255_USB%20to%20I2C%20Example%20using%20the%20FT232H%20and%20FT201X%20devices.pdf)). Be aware of counterfeit FTDI chips and buy from a trustworthy source!

The following connection's need to be made:

![Schematic](doc/schematic.png)

As Venus OS supports those FTDI's chip by default it would the Linux kernel's USB serial driver for them and scan the serial ports using `serial-starter`. To avoid this I changed the VID/PID of the FT232H chip using FTDI's [FT_PROG](https://ftdichip.com/utilities/).

![Screenshot of FT_PROG](doc/vid_pid.png)

The VID 0x0403 / PID 0xD4F8 I used is out of a PID block I got from FTDI in 2005. Feel free to re-use on your adapter/board as well. 

Besides the VID/PID I changed the Manufacturer to 'twam.info', the Product Description to 'I2C Master' , Port A's Hardware to 245 FIFO and Port A's Driver to D2XX Direct. All those should be optional.

#### FTDI's C232HM-DxDHSL-0

See above.

#### Adafruit FT232H Breakout

Adafruit offers a [FT232H Breakout](https://www.adafruit.com/product/2264), which also works fine. I used the 3.3V supply (and not the 5V) as with the 5V the Lynx Distributors didn't answer I2C requests. The breakout always pull SCL/SDA to only 3.3V which might be not enough for the Lynx Distributor if running at 5V.


## Software

To be able to use I²C with the FT232H chip its MPSSE mode must be used. There are plenty of options to use this mode (FTDI's official drivers, libftdi, pyftdi), but all of them required custom drivers, ... to be compiled/installed on Venus OS which are not premade available.

Therefore I re-implemented the the required drivers parts in Python just using `https://github.com/pyusb/pyusb` which is available on Venus OS via `pip`.

### Installation

Download into `/data/dbus-lynx-distributor` on your Venus device (e.g. Cerbo GX) and run `install.sh`.

If you clone the git repository, don't forget to also clone the submodules. Either by a recursive clone (`git clone --recursive …`) or an explicit submodule update (`git submodule update --init`).

You might also need to install the `python3-modules` package by `mount -o remount,rw / && opkg install python3-modules && mount -o remount,ro /`.

### Configuration

Rename `config.sample.ini` and change to needs.