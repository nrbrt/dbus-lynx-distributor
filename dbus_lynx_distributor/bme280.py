""" Bosch BME280 environmental sensor driver — pure logic + I2C glue.

The compensation maths follows the Bosch BST-BME280-DS002 datasheet
(§8.1, floating-point reference). The compensate_*() functions are
hardware-free and unit-testable; the Bme280Reader class wraps them
behind a pyftdi I2cPort.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass
from typing import Optional, Tuple

log = logging.getLogger(__name__)


# ---- Constants ------------------------------------------------------------

BME280_DEFAULT_ADDRESS = 0x76        # SDO tied to GND (most modules ship this way)
BME280_ALT_ADDRESS = 0x77            # SDO tied to VDDIO

BME280_CHIP_ID = 0x60                # value of register 0xD0

# Register addresses (datasheet §5.3).
REG_ID = 0xD0
REG_RESET = 0xE0
REG_CTRL_HUM = 0xF2
REG_STATUS = 0xF3
REG_CTRL_MEAS = 0xF4
REG_CONFIG = 0xF5
REG_DATA = 0xF7                      # 0xF7-0xFE: 8 bytes pressure/temp/humidity
REG_CALIB_88 = 0x88                  # 0x88-0xA1: 26 bytes (T+P+H1)
REG_CALIB_E1 = 0xE1                  # 0xE1-0xE7: 7 bytes (H2..H6)

# ctrl_meas / ctrl_hum / config bit fields.
OSRS_X1 = 0b001                       # 1x oversampling
MODE_SLEEP = 0b00
MODE_FORCED = 0b01
MODE_NORMAL = 0b11

# Reset magic and timings (datasheet §5.4.2 + §11.1).
RESET_MAGIC = 0xB6
POWER_ON_RESET_DELAY_S = 0.005        # 2 ms typ., be generous
MEASUREMENT_DELAY_S = 0.010           # 1×osrs all → ~9.3 ms typical


# ---- Calibration ----------------------------------------------------------

@dataclass(frozen=True)
class Bme280Calibration:
    """ Per-chip compensation parameters, read once at init from registers. """
    dig_T1: int
    dig_T2: int
    dig_T3: int
    dig_P1: int
    dig_P2: int
    dig_P3: int
    dig_P4: int
    dig_P5: int
    dig_P6: int
    dig_P7: int
    dig_P8: int
    dig_P9: int
    dig_H1: int
    dig_H2: int
    dig_H3: int
    dig_H4: int
    dig_H5: int
    dig_H6: int


@dataclass(frozen=True)
class RawMeasurement:
    """ Raw (uncompensated) sensor values straight from registers 0xF7-0xFE. """
    raw_pressure: int       # 20-bit unsigned
    raw_temperature: int    # 20-bit unsigned
    raw_humidity: int       # 16-bit unsigned


@dataclass(frozen=True)
class Bme280Reading:
    """ Compensated reading, ready for D-Bus publish. """
    temperature_c: float
    humidity_percent: float
    pressure_hpa: float


def parse_calibration(block_88: bytes, block_e1: bytes) -> Bme280Calibration:
    """ Decode the two calibration register blocks per datasheet §4.2.2. """
    if len(block_88) < 26:
        raise ValueError(f"calibration block 0x88 too short: {len(block_88)} bytes")
    if len(block_e1) < 7:
        raise ValueError(f"calibration block 0xE1 too short: {len(block_e1)} bytes")

    # 0x88-0xA1: dig_T1 (uint16), dig_T2/T3 (int16), dig_P1 (uint16), dig_P2..P9 (int16),
    # 0xA0 reserved, 0xA1 dig_H1 (uint8). Layout matches "<HhhHhhhhhhhhBB".
    (
        dig_T1, dig_T2, dig_T3,
        dig_P1, dig_P2, dig_P3, dig_P4, dig_P5,
        dig_P6, dig_P7, dig_P8, dig_P9,
        _reserved_a0, dig_H1,
    ) = struct.unpack("<HhhHhhhhhhhhBB", block_88[:26])

    # 0xE1-0xE7: dig_H2 (int16), dig_H3 (uint8), then 12-bit packed dig_H4 / dig_H5,
    # then dig_H6 (int8).
    dig_H2, dig_H3, e4, e5, e6, dig_H6 = struct.unpack("<hBBBBb", block_e1[:7])

    # H4 is 12-bit signed: high 8 bits in E4, low 4 bits in E5[3:0].
    dig_H4 = (e4 << 4) | (e5 & 0x0F)
    if dig_H4 & 0x800:
        dig_H4 -= 0x1000

    # H5 is 12-bit signed: low 4 bits in E5[7:4], high 8 bits in E6.
    dig_H5 = ((e5 >> 4) & 0x0F) | (e6 << 4)
    if dig_H5 & 0x800:
        dig_H5 -= 0x1000

    return Bme280Calibration(
        dig_T1=dig_T1, dig_T2=dig_T2, dig_T3=dig_T3,
        dig_P1=dig_P1, dig_P2=dig_P2, dig_P3=dig_P3, dig_P4=dig_P4,
        dig_P5=dig_P5, dig_P6=dig_P6, dig_P7=dig_P7, dig_P8=dig_P8, dig_P9=dig_P9,
        dig_H1=dig_H1, dig_H2=dig_H2, dig_H3=dig_H3,
        dig_H4=dig_H4, dig_H5=dig_H5, dig_H6=dig_H6,
    )


def parse_raw_measurement(block: bytes) -> RawMeasurement:
    """ Decode the 8-byte 0xF7-0xFE block: pressure, temperature, humidity.

    Pressure and temperature are 20-bit MSB-aligned in 3 bytes (top 4 bits
    of the third byte are unused); humidity is a plain 16-bit big-endian.
    """
    if len(block) < 8:
        raise ValueError(f"data block too short: {len(block)} bytes")

    raw_pressure = ((block[0] << 16) | (block[1] << 8) | block[2]) >> 4
    raw_temperature = ((block[3] << 16) | (block[4] << 8) | block[5]) >> 4
    raw_humidity = (block[6] << 8) | block[7]

    return RawMeasurement(
        raw_pressure=raw_pressure,
        raw_temperature=raw_temperature,
        raw_humidity=raw_humidity,
    )


# ---- Compensation (Bosch §8.1, double-precision reference) ---------------

def compensate_temperature(adc_T: int, cal: Bme280Calibration) -> Tuple[float, int]:
    """ Returns (temperature_celsius, t_fine). t_fine feeds the other two. """
    var1 = (adc_T / 16384.0 - cal.dig_T1 / 1024.0) * cal.dig_T2
    var2 = ((adc_T / 131072.0 - cal.dig_T1 / 8192.0) ** 2) * cal.dig_T3
    t_fine = int(var1 + var2)
    temp_c = (var1 + var2) / 5120.0
    return temp_c, t_fine


def compensate_pressure(adc_P: int, cal: Bme280Calibration, t_fine: int) -> float:
    """ Returns pressure in hPa. 0.0 on calibration-induced division-by-zero. """
    var1 = (t_fine / 2.0) - 64000.0
    var2 = var1 * var1 * cal.dig_P6 / 32768.0
    var2 = var2 + var1 * cal.dig_P5 * 2.0
    var2 = (var2 / 4.0) + (cal.dig_P4 * 65536.0)
    var1 = (cal.dig_P3 * var1 * var1 / 524288.0 + cal.dig_P2 * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * cal.dig_P1
    if var1 == 0.0:
        return 0.0
    p = 1048576.0 - adc_P
    p = (p - (var2 / 4096.0)) * 6250.0 / var1
    var1 = cal.dig_P9 * p * p / 2147483648.0
    var2 = p * cal.dig_P8 / 32768.0
    p = p + (var1 + var2 + cal.dig_P7) / 16.0
    return p / 100.0   # Pa → hPa


def compensate_humidity(adc_H: int, cal: Bme280Calibration, t_fine: int) -> float:
    """ Returns relative humidity in %RH, clamped to 0..100.

    Uses Bosch's integer (Q22.10) compensation formula — the double-precision
    reference in the same datasheet has a known divergence here, and every
    embedded driver uses the integer one. Returned scalar /1024 → %RH.
    """
    v = t_fine - 76800
    v = ((((adc_H << 14) - (cal.dig_H4 << 20) - (cal.dig_H5 * v)) + 16384) >> 15) * (
        (((((v * cal.dig_H6) >> 10) * (((v * cal.dig_H3) >> 11) + 32768)) >> 10)
            + 2097152) * cal.dig_H2 + 8192
    ) >> 14
    v = v - (((((v >> 15) * (v >> 15)) >> 7) * cal.dig_H1) >> 4)
    if v < 0:
        v = 0
    elif v > 419430400:                # clamp at 100 %RH (per Bosch)
        v = 419430400
    return (v >> 12) / 1024.0


# ---- Hardware reader ------------------------------------------------------

class Bme280Reader:
    """ I2C wrapper around a BME280 on a pyftdi I2cController.

    Open the controller once for the whole process (the Lynx service does
    this); pass a port via i2c.get_port(address) here.
    """

    def __init__(self, i2c_port, address: int = BME280_DEFAULT_ADDRESS) -> None:
        self._port = i2c_port
        self.address = address
        self._calibration: Optional[Bme280Calibration] = None

    def probe(self) -> bool:
        """ True iff the chip at this I2C address replies with BME280's chip ID. """
        try:
            chip_id = self._port.read_from(REG_ID, 1)[0]
        except Exception as e:
            log.debug("BME280 probe at 0x%02x failed: %s", self.address, e)
            return False
        return chip_id == BME280_CHIP_ID

    def initialize(self) -> None:
        """ Read calibration and configure the chip for forced single-shot mode. """
        block_88 = bytes(self._port.read_from(REG_CALIB_88, 26))
        block_e1 = bytes(self._port.read_from(REG_CALIB_E1, 7))
        self._calibration = parse_calibration(block_88, block_e1)

        # ctrl_hum must be written BEFORE ctrl_meas (datasheet §5.4.3).
        self._port.write_to(REG_CTRL_HUM, bytes([OSRS_X1]))
        # ctrl_meas: osrs_t=1, osrs_p=1, mode=sleep (forced is set per measurement).
        self._port.write_to(REG_CTRL_MEAS, bytes([(OSRS_X1 << 5) | (OSRS_X1 << 2) | MODE_SLEEP]))
        # config: tsb=0.5ms, filter=off, no SPI 3-wire.
        self._port.write_to(REG_CONFIG, bytes([0]))

    def read(self) -> Bme280Reading:
        """ Trigger a forced measurement and return the compensated reading. """
        if self._calibration is None:
            raise RuntimeError("Bme280Reader.initialize() must be called first")

        # Forced mode: kick off a single conversion, wait, then read.
        self._port.write_to(REG_CTRL_MEAS, bytes([(OSRS_X1 << 5) | (OSRS_X1 << 2) | MODE_FORCED]))
        time.sleep(MEASUREMENT_DELAY_S)

        block = bytes(self._port.read_from(REG_DATA, 8))
        raw = parse_raw_measurement(block)

        temp_c, t_fine = compensate_temperature(raw.raw_temperature, self._calibration)
        pressure_hpa = compensate_pressure(raw.raw_pressure, self._calibration, t_fine)
        humidity_percent = compensate_humidity(raw.raw_humidity, self._calibration, t_fine)

        return Bme280Reading(
            temperature_c=temp_c,
            humidity_percent=humidity_percent,
            pressure_hpa=pressure_hpa,
        )
