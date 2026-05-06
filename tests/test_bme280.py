""" Tests for the BME280 environmental sensor driver.

The Bosch BST-BME280-DS002 datasheet documents calibration constants and
expected outputs (section 4.2.3 / appendix A.1). Those reference values
anchor the compensation tests below — if these stay green, our maths
matches the chip's reference implementation.

No hardware needed: every test feeds raw bytes straight into the
compensation logic.
"""

import struct

import pytest

from dbus_lynx_distributor.bme280 import (
    BME280_CHIP_ID,
    BME280_DEFAULT_ADDRESS,
    Bme280Calibration,
    Bme280Reading,
    compensate_humidity,
    compensate_pressure,
    compensate_temperature,
    parse_calibration,
    parse_raw_measurement,
)


# ---- Bosch datasheet reference calibration (BST-BME280-DS002 §4.2.3) ----

REF_CALIBRATION = Bme280Calibration(
    dig_T1=27504, dig_T2=26435, dig_T3=-1000,
    dig_P1=36477, dig_P2=-10685, dig_P3=3024, dig_P4=2855,
    dig_P5=140, dig_P6=-7, dig_P7=15500, dig_P8=-14600, dig_P9=6000,
    dig_H1=75, dig_H2=380, dig_H3=0,
    dig_H4=305, dig_H5=0, dig_H6=30,
)

# Reference raw measurement (datasheet example).
REF_RAW_TEMP = 519888
REF_RAW_PRESSURE = 415148
REF_RAW_HUMIDITY = 30000


def test_chip_id_constant():
    """BME280 chip ID is 0x60 (per datasheet §5.4.1)."""
    assert BME280_CHIP_ID == 0x60


def test_default_address():
    """Default I2C address is 0x76 (SDO tied to GND)."""
    assert BME280_DEFAULT_ADDRESS == 0x76


def test_temperature_compensation_with_reference():
    """
    Datasheet reference: raw_temp=519888 with reference calibration
    yields ~25.08 °C and t_fine = 128422.
    """
    temp_c, t_fine = compensate_temperature(REF_RAW_TEMP, REF_CALIBRATION)
    assert t_fine == 128422
    assert temp_c == pytest.approx(25.08, abs=0.01)


def test_pressure_compensation_with_reference():
    """
    Datasheet reference: raw_pressure=415148 with reference calibration
    and t_fine=128422 yields ~1006.53 hPa.
    """
    _, t_fine = compensate_temperature(REF_RAW_TEMP, REF_CALIBRATION)
    pressure_hpa = compensate_pressure(REF_RAW_PRESSURE, REF_CALIBRATION, t_fine)
    assert pressure_hpa == pytest.approx(1006.53, abs=0.05)


def test_humidity_compensation_with_reference():
    """
    Bosch's integer compensation formula (BST-BME280-DS002 §8.1) with
    raw_humidity=30000 and the reference calibration yields ~61.62 %RH.
    Verified against a stand-alone C reimplementation of the same formula.
    """
    _, t_fine = compensate_temperature(REF_RAW_TEMP, REF_CALIBRATION)
    rh_percent = compensate_humidity(REF_RAW_HUMIDITY, REF_CALIBRATION, t_fine)
    assert rh_percent == pytest.approx(61.62, abs=0.05)


def test_humidity_clamped_to_0_100():
    """
    Compensation can mathematically produce <0 or >100 — datasheet says
    clamp. Use extreme raw values to force out-of-range and verify clamp.
    """
    _, t_fine = compensate_temperature(REF_RAW_TEMP, REF_CALIBRATION)
    rh_low = compensate_humidity(0, REF_CALIBRATION, t_fine)
    rh_high = compensate_humidity(0xFFFF, REF_CALIBRATION, t_fine)
    assert 0.0 <= rh_low <= 100.0
    assert 0.0 <= rh_high <= 100.0


def test_pressure_zero_div_safe():
    """
    Pressure compensation has a divide-by-zero risk if dig_P1 lookalike
    intermediate hits zero. Edge-case fixture with a calibration that
    drives the divisor to zero must return 0.0 (not crash).
    """
    cal = Bme280Calibration(
        dig_T1=27504, dig_T2=26435, dig_T3=-1000,
        dig_P1=0, dig_P2=0, dig_P3=0, dig_P4=0,
        dig_P5=0, dig_P6=0, dig_P7=0, dig_P8=0, dig_P9=0,
        dig_H1=75, dig_H2=380, dig_H3=0,
        dig_H4=305, dig_H5=0, dig_H6=30,
    )
    _, t_fine = compensate_temperature(REF_RAW_TEMP, cal)
    assert compensate_pressure(REF_RAW_PRESSURE, cal, t_fine) == 0.0


def test_parse_calibration_from_bytes():
    """
    Calibration registers 0x88-0xA1 (26 bytes) + 0xE1-0xE7 (7 bytes)
    are encoded little-endian per Bosch §4.2.2. Round-trip the
    reference calibration through pack→parse and check it survives.
    """
    block_88 = struct.pack(
        "<HhhHhhhhhhhhBB",
        REF_CALIBRATION.dig_T1, REF_CALIBRATION.dig_T2, REF_CALIBRATION.dig_T3,
        REF_CALIBRATION.dig_P1, REF_CALIBRATION.dig_P2, REF_CALIBRATION.dig_P3,
        REF_CALIBRATION.dig_P4, REF_CALIBRATION.dig_P5, REF_CALIBRATION.dig_P6,
        REF_CALIBRATION.dig_P7, REF_CALIBRATION.dig_P8, REF_CALIBRATION.dig_P9,
        0,  # reserved 0xA0
        REF_CALIBRATION.dig_H1,
    )

    # Humidity calibration block 0xE1-0xE7 layout (Bosch §4.2.2 table 16):
    #   0xE1-0xE2: dig_H2 (int16, LE)
    #   0xE3:      dig_H3 (uint8)
    #   0xE4:      dig_H4 [11:4]   (signed, 12-bit, packed across E4/E5)
    #   0xE5[3:0]: dig_H4 [3:0]    (low 4 bits in E5 lower nibble)
    #   0xE5[7:4]: dig_H5 [3:0]    (low 4 bits in E5 upper nibble)
    #   0xE6:      dig_H5 [11:4]
    #   0xE7:      dig_H6 (int8)
    h4 = REF_CALIBRATION.dig_H4 & 0xFFF
    h5 = REF_CALIBRATION.dig_H5 & 0xFFF
    e4 = (h4 >> 4) & 0xFF
    e5 = (h4 & 0x0F) | ((h5 & 0x0F) << 4)
    e6 = (h5 >> 4) & 0xFF
    block_e1 = struct.pack(
        "<hBBBBb",
        REF_CALIBRATION.dig_H2,
        REF_CALIBRATION.dig_H3,
        e4, e5, e6,
        REF_CALIBRATION.dig_H6,
    )

    cal = parse_calibration(block_88, block_e1)
    assert cal == REF_CALIBRATION


def test_parse_raw_measurement():
    """
    Raw measurement registers 0xF7-0xFE: pressure (3 bytes, 20-bit MSB),
    temperature (3 bytes, 20-bit MSB), humidity (2 bytes, 16-bit).
    """
    raw_p = REF_RAW_PRESSURE << 4    # left-justify into 24 bits
    raw_t = REF_RAW_TEMP << 4
    raw_h = REF_RAW_HUMIDITY

    block = bytes([
        (raw_p >> 16) & 0xFF, (raw_p >> 8) & 0xFF, raw_p & 0xFF,
        (raw_t >> 16) & 0xFF, (raw_t >> 8) & 0xFF, raw_t & 0xFF,
        (raw_h >> 8) & 0xFF, raw_h & 0xFF,
    ])

    measurement = parse_raw_measurement(block)
    assert measurement.raw_temperature == REF_RAW_TEMP
    assert measurement.raw_pressure == REF_RAW_PRESSURE
    assert measurement.raw_humidity == REF_RAW_HUMIDITY


def test_bme280_reading_dataclass():
    """Reading dataclass holds the three values, exposed for D-Bus publish."""
    r = Bme280Reading(temperature_c=21.5, humidity_percent=55.0, pressure_hpa=1013.25)
    assert r.temperature_c == 21.5
    assert r.humidity_percent == 55.0
    assert r.pressure_hpa == 1013.25


# ---- Address-config parsing ----

from dbus_lynx_distributor.bme280 import parse_address_config


def test_parse_address_config_auto():
    assert parse_address_config("auto") == (0x76, 0x77)


def test_parse_address_config_default_is_auto():
    """None and empty config map to auto so users without the section work."""
    assert parse_address_config(None) == (0x76, 0x77)


def test_parse_address_config_disabled():
    for v in ("disabled", "off", "no", "false", ""):
        assert parse_address_config(v) is None, f"{v!r} should disable"


def test_parse_address_config_explicit_hex():
    assert parse_address_config("0x76") == (0x76,)
    assert parse_address_config("0x77") == (0x77,)


def test_parse_address_config_explicit_decimal():
    assert parse_address_config("118") == (0x76,)
    assert parse_address_config("119") == (0x77,)


def test_parse_address_config_case_insensitive():
    assert parse_address_config("AUTO") == (0x76, 0x77)
    assert parse_address_config("Disabled") is None


def test_parse_address_config_out_of_range():
    with pytest.raises(ValueError):
        parse_address_config("0x80")          # too high
    with pytest.raises(ValueError):
        parse_address_config("0x00")          # too low


# ---- detect_bme280 ----

from dbus_lynx_distributor.bme280 import BME280_CHIP_ID, detect_bme280


class _FakePort:
    """Minimal pyftdi I2cPort stand-in for detection tests."""
    def __init__(self, address, chip_id_value):
        self.address = address
        self._chip_id_value = chip_id_value
        self._calib_88 = bytes(26)            # zeros — initialize() reads this
        self._calib_e1 = bytes(7)
        self._writes = []

    def read_from(self, reg, length):
        if reg == 0xD0:
            return bytes([self._chip_id_value])
        if reg == 0x88:
            return self._calib_88
        if reg == 0xE1:
            return self._calib_e1
        return bytes(length)

    def write_to(self, reg, data):
        self._writes.append((reg, data))


class _FakeI2cController:
    def __init__(self, ports_by_addr):
        self._ports = ports_by_addr

    def get_port(self, address):
        if address not in self._ports:
            raise ValueError(f"no port at 0x{address:02x}")
        return self._ports[address]


def test_detect_bme280_finds_default_address():
    ctrl = _FakeI2cController({
        0x76: _FakePort(0x76, BME280_CHIP_ID),
        0x77: _FakePort(0x77, 0xFF),                      # not BME280
    })
    reader = detect_bme280(ctrl, "auto")
    assert reader is not None
    assert reader.address == 0x76


def test_detect_bme280_falls_through_to_alt_address():
    ctrl = _FakeI2cController({
        0x76: _FakePort(0x76, 0xFF),                      # not BME280
        0x77: _FakePort(0x77, BME280_CHIP_ID),
    })
    reader = detect_bme280(ctrl, "auto")
    assert reader is not None
    assert reader.address == 0x77


def test_detect_bme280_returns_none_when_disabled():
    ctrl = _FakeI2cController({
        0x76: _FakePort(0x76, BME280_CHIP_ID),
    })
    assert detect_bme280(ctrl, "disabled") is None


def test_detect_bme280_returns_none_when_chip_absent():
    ctrl = _FakeI2cController({
        0x76: _FakePort(0x76, 0xFF),
        0x77: _FakePort(0x77, 0xFF),
    })
    assert detect_bme280(ctrl, "auto") is None


def test_detect_bme280_explicit_address_only_probes_that_one():
    """User pinned 0x77 → don't even look at 0x76 (avoids spurious matches)."""
    ctrl = _FakeI2cController({
        0x76: _FakePort(0x76, BME280_CHIP_ID),            # would match if probed
        0x77: _FakePort(0x77, 0xFF),
    })
    reader = detect_bme280(ctrl, "0x77")
    assert reader is None                                  # 0x77 didn't match, 0x76 not probed
