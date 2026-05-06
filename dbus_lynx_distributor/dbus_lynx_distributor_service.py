import logging
from configparser import ConfigParser
from typing import Optional

from gi.repository import GLib
from vedbus import VeDbusService
from dbus import SystemBus
from usb.core import USBError

from .bme280 import Bme280Reader, detect_bme280
from .decoder import (
    ALARM_ACTIVE,
    ALARM_OK,
    CONNECTED_FALSE,
    CONNECTED_TRUE,
    DISTRIBUTOR_STATUS_COMMS_LOST,
    DISTRIBUTOR_STATUS_NOT_AVAILABLE,
    FUSE_STATUS_NOT_AVAILABLE,
    FUSES_PER_DISTRIBUTOR,
    LYNX_I2C_BASE_ADDR,
    NUM_DISTRIBUTORS,
    POLL_INTERVAL_MS,
    DistributorState,
    decode_distributor_state,
)
from .ftdi import NACK, Ftdi


class DbusLynxDistributorService:
    def __init__(
        self,
        *,
        service_name: str,
        device_instance: int,
        ftdi: Ftdi,
        config: ConfigParser,
        product_name: str = 'Lynx Distributor',
        custom_name: str = 'Lynx Distributor',
        connection: str = 'USB<->I2C',
    ) -> None:

        self._ftdi = ftdi
        self._config = config
        self._reinit_pending: bool = False
        self._timer_id: Optional[int] = None

        self._dbusservice = VeDbusService(servicename=service_name, bus=SystemBus(private=True), register=False)

        from . import __version__

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __name__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion', __version__)
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Create the mandatory objects
        self._dbusservice.add_path('/ProductName', product_name)
        self._dbusservice.add_path('/CustomName', self._config_get('name', custom_name))
        self._dbusservice.add_path('/DeviceInstance', device_instance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        self._dbusservice.add_path('/Serial', self._ftdi.serial_number)
        self._dbusservice.add_path('/FirmwareVersion', '')
        self._dbusservice.add_path('/HardwareVersion', '')

        self._dbusservice.add_path('/Connected', CONNECTED_TRUE)

        self._dbusservice.add_path('/NrOfDistributors', NUM_DISTRIBUTORS)

        for distributor in ['A', 'B', 'C', 'D']:
            self._dbusservice.add_path(f'/Distributor/{distributor}/Status', None)
            self._dbusservice.add_path(f'/Distributor/{distributor}/Alarms/ConnectionLost', None)
            for fuse in range(FUSES_PER_DISTRIBUTOR):
                fuse_index = self._fuse_index(fuse)
                self._dbusservice.add_path(f'/Distributor/{distributor}/Fuse/{fuse_index}/Name', self._config_get(f'distributor{distributor}Fuse{fuse}Name', None))  # 16-byte UTF-8 limit
                self._dbusservice.add_path(f'/Distributor/{distributor}/Fuse/{fuse_index}/Status', None)
                self._dbusservice.add_path(f'/Distributor/{distributor}/Fuse/{fuse_index}/Alarms/Blown', None)

        self._dbusservice.register()
        self._ftdi.init_i2c()

        # Optional BME280 environmental sensor on the same I2C bus.
        # Address conflict with Lynx (0x08-0x17) is impossible — BME280 lives at 0x76/0x77.
        self._bme280_reader: Optional[Bme280Reader] = None
        self._bme280_dbus: Optional[VeDbusService] = None
        self._init_bme280(device_instance)

        self._update()
        self._timer_id = GLib.timeout_add(POLL_INTERVAL_MS, self._update)

    def _init_bme280(self, lynx_device_instance: int) -> None:
        """ Probe for a BME280 on the shared I2C bus and, if found, register
        a second VeDbusService under com.victronenergy.temperature.* so it
        appears as a sensor tile in the Venus OS GUI.

        Config (in the same [ftdi:<serial>] section as the Lynx options):
          bme280 = auto         # 'auto' (default), '0x76', '0x77', or 'disabled'
          bme280Name = ...      # optional CustomName, default 'BME280'
        """
        bme280_address = self._config_get('bme280', 'auto')
        try:
            self._bme280_reader = detect_bme280(self._ftdi.i2c, bme280_address)
        except Exception as e:                              # noqa: BLE001
            logging.warning(f"BME280 detection failed: {e}")
            self._bme280_reader = None
            return
        if self._bme280_reader is None:
            return

        from . import __version__

        serial = self._ftdi.serial_number
        service_name = f'com.victronenergy.temperature.{serial}_bme280'
        # Offset by +100 to avoid colliding with the Lynx instance number.
        sensor_instance = lynx_device_instance + 100

        self._bme280_dbus = VeDbusService(servicename=service_name, bus=SystemBus(private=True), register=False)
        self._bme280_dbus.add_path('/Mgmt/ProcessName', __name__)
        self._bme280_dbus.add_path('/Mgmt/ProcessVersion', __version__)
        self._bme280_dbus.add_path('/Mgmt/Connection', f'I2C 0x{self._bme280_reader.address:02x} via FT232H')
        self._bme280_dbus.add_path('/ProductName', 'BME280')
        self._bme280_dbus.add_path('/CustomName', self._config_get('bme280Name', 'BME280'))
        self._bme280_dbus.add_path('/DeviceInstance', sensor_instance)
        self._bme280_dbus.add_path('/ProductId', 0xFFFE)    # placeholder, no Victron PID for generic sensors
        self._bme280_dbus.add_path('/Serial', f'{serial}_bme280')
        self._bme280_dbus.add_path('/FirmwareVersion', '')
        self._bme280_dbus.add_path('/HardwareVersion', '')
        self._bme280_dbus.add_path('/Connected', CONNECTED_TRUE)
        # Standard Victron temperature service paths.
        self._bme280_dbus.add_path('/TemperatureType', 2)   # 2 = generic
        self._bme280_dbus.add_path('/Status', 0)            # 0 = OK
        self._bme280_dbus.add_path('/Temperature', None)
        self._bme280_dbus.add_path('/Humidity', None)
        self._bme280_dbus.add_path('/Pressure', None)
        self._bme280_dbus.register()
        logging.info(f"BME280 registered as {service_name} (address 0x{self._bme280_reader.address:02x})")

    def close(self) -> None:
        """ Release the I2C controller and cancel the poll timer.

        Safe to call multiple times. Called from the main process's
        SIGTERM/SIGINT handler so a systemctl restart doesn't leave a
        stuck FTDI handle or a half-finished I2C transaction.
        """
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        if self._bme280_dbus is not None:
            try:
                self._bme280_dbus['/Connected'] = CONNECTED_FALSE
            except Exception:                              # noqa: BLE001
                pass
            self._bme280_dbus = None
        try:
            self._ftdi.close()
        except Exception as e:  # noqa: BLE001 — last-ditch cleanup
            logging.warning(f"Error closing Ftdi during shutdown: {e}")

    def _poll_bme280(self) -> None:
        """ Read the BME280 (if present) and publish to its D-Bus service.

        Errors are logged but never fatal — the Lynx polling continues even
        if the sensor disappears or returns garbage.
        """
        if self._bme280_reader is None or self._bme280_dbus is None:
            return
        try:
            reading = self._bme280_reader.read()
        except Exception as e:                              # noqa: BLE001
            logging.warning(f"BME280 read failed: {e}")
            self._bme280_dbus['/Connected'] = CONNECTED_FALSE
            self._bme280_dbus['/Temperature'] = None
            self._bme280_dbus['/Humidity'] = None
            self._bme280_dbus['/Pressure'] = None
            return
        self._bme280_dbus['/Connected'] = CONNECTED_TRUE
        self._bme280_dbus['/Temperature'] = round(reading.temperature_c, 2)
        self._bme280_dbus['/Humidity'] = round(reading.humidity_percent, 1)
        self._bme280_dbus['/Pressure'] = round(reading.pressure_hpa, 2)

    def _config_get(self, option: str, fallback):
        return self._config.get(f'ftdi:{self._ftdi.serial_number}', option, fallback=fallback)

    def _config_getboolean(self, option: str, fallback: bool) -> bool:
        return self._config.getboolean(f'ftdi:{self._ftdi.serial_number}', option, fallback=fallback)

    def _fuse_index(self, fuse: int) -> int:
        """ Translate a 0..3 fuse number to the dbus path index, honouring
        the mounted_upside_down config flag. """
        return (FUSES_PER_DISTRIBUTOR - 1) - fuse if self._config_getboolean('mounted_upside_down', False) else fuse

    def _set_distributor_lost(self, distributor: str) -> None:
        """ Mark a single distributor as Communications Lost (or Not
        Available if it isn't installed). Used by both the global
        invalidate-all path and the per-address fallback in _update. """
        installed = self._config_getboolean(f'distributor{distributor}Installed', False)
        self._dbusservice[f'/Distributor/{distributor}/Status'] = DISTRIBUTOR_STATUS_COMMS_LOST if installed else DISTRIBUTOR_STATUS_NOT_AVAILABLE
        self._dbusservice[f'/Distributor/{distributor}/Alarms/ConnectionLost'] = ALARM_ACTIVE if installed else ALARM_OK

    def _publish_distributor(self, distributor: str, decoded: DistributorState) -> None:
        """ Publish a decoded DistributorState on the dbus paths for one
        distributor. Honours the mounted_upside_down flag for fuse path
        indices (the decoder works on physical fuse 0..3 and doesn't know
        about the dbus-path mapping). """
        self._dbusservice[f'/Distributor/{distributor}/Status'] = decoded.status
        self._dbusservice[f'/Distributor/{distributor}/Alarms/ConnectionLost'] = decoded.connection_lost_alarm
        for fuse, fuse_state in enumerate(decoded.fuses):
            fuse_index = self._fuse_index(fuse)
            self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Status'] = fuse_state.status
            self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Alarms/Blown'] = fuse_state.alarm

    def _invalidate_all(self) -> None:
        """ Mark every installed distributor as Communications Lost and the
        service as disconnected.

        Called when USB or bus-level errors prevent us from polling. We
        deliberately keep the GLib timer running so we can recover on the
        next tick.
        """
        self._dbusservice['/Connected'] = CONNECTED_FALSE
        for distributor in ['A', 'B', 'C', 'D']:
            self._set_distributor_lost(distributor)
            for fuse in range(FUSES_PER_DISTRIBUTOR):
                fuse_index = self._fuse_index(fuse)
                self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Status'] = FUSE_STATUS_NOT_AVAILABLE
                self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Alarms/Blown'] = ALARM_OK

    def _update(self) -> bool:
        # GLib timer convention: return True keeps the timer alive, False
        # removes it permanently. We always return True here — even on
        # error — because the recovery path needs the next tick to fire.
        if self._reinit_pending:
            try:
                self._ftdi.init_i2c()
                self._reinit_pending = False
                self._dbusservice['/Connected'] = CONNECTED_TRUE
                logging.info("I2C controller re-initialized after USB error")
            except USBError as e:
                logging.warning(f"I2C re-init still failing: {e}")
                return True

        try:
            for lynx in range(NUM_DISTRIBUTORS):
                distributor = chr(ord('A') + lynx)
                address = LYNX_I2C_BASE_ADDR + lynx

                available = self._ftdi.send_addr_and_check_ack(address)
                if not available:
                    self._set_distributor_lost(distributor)
                    continue

                state = self._ftdi.read_byte_and_send_nak(address)
                # Slave ACKed the addressing but NACKed the data byte
                # (or returned zero bytes). Treat as Communications Lost
                # so we don't pass the NACK sentinel into the decoder.
                if state is NACK:
                    self._set_distributor_lost(distributor)
                    continue

                fuse_installed = tuple(
                    self._config_getboolean(f'distributor{distributor}Fuse{fuse}Installed', True)
                    for fuse in range(FUSES_PER_DISTRIBUTOR)
                )
                decoded = decode_distributor_state(state, fuse_installed)
                self._publish_distributor(distributor, decoded)

        except USBError as e:
            logging.error(f"USB communication failed: {e}")
            self._invalidate_all()
            self._reinit_pending = True
            # Keep the GLib timer alive so we can recover on the next tick.
            return True

        # Successful poll — make sure /Connected reflects reality even if
        # we recovered from a previous USBError.
        self._dbusservice['/Connected'] = CONNECTED_TRUE

        # BME280 sensor on the same I2C bus (no-op if not detected).
        self._poll_bme280()

        return True
