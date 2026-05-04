import logging
from dataclasses import dataclass
from typing import Callable, Optional, Type, Union

from gi.repository import GLib
from vedbus import VeDbusService
from dbus import SystemBus
from usb.core import USBError

from .ftdi import NACK


# Distributor status values (Victron dbus convention).
DISTRIBUTOR_STATUS_NOT_AVAILABLE = 0
DISTRIBUTOR_STATUS_CONNECTED = 1
DISTRIBUTOR_STATUS_NO_BUS_POWER = 2
DISTRIBUTOR_STATUS_COMMS_LOST = 3

# Fuse status values.
FUSE_STATUS_NOT_AVAILABLE = 0
FUSE_STATUS_NOT_USED = 1
FUSE_STATUS_OK = 2
FUSE_STATUS_BLOWN = 3

# Alarm states (Victron dbus convention skips 1).
ALARM_OK = 0
ALARM_ACTIVE = 2

# Connection states for /Connected dbus path.
CONNECTED_FALSE = 0
CONNECTED_TRUE = 1

# I2C protocol constants — see README "Hardware/Background" for the byte format.
LYNX_I2C_BASE_ADDR = 0x08          # actual address = base + jumper-set offset
NUM_DISTRIBUTORS = 4
FUSES_PER_DISTRIBUTOR = 4
BIT_NO_BUS_POWER = 0b0000_0010
BIT_FUSE0_BLOWN = 0b0001_0000      # fuse N blown bit = BIT_FUSE0_BLOWN << N

POLL_INTERVAL_MS = 2000


@dataclass
class ServicePath:
    dbus_path: str
    dbus_gettextcallback: Optional[Callable[[str, Union[int, str, float]], bool]] = None
    dbus_initialvalue: Union[int, str, float] = None
    dbus_writeable: bool = False
    dbus_valuetype: Type = None
    config_key: Optional[str] = None
    config_invert_key: Optional[str] = None


class DbusLynxDistributorService:
    def __init__(
        self,
        *,
        service_name,
        device_instance,
        ftdi,
        config,
        product_name='Lynx Distributor',
        custom_name='Lynx Distributor',
        connection='USB<->I2C',
    ):

        self._ftdi = ftdi
        self._config = config
        self._reinit_pending = False

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

        self._update()
        GLib.timeout_add(POLL_INTERVAL_MS, self._update)

    def _config_get(self, option, fallback):
        return self._config.get(f'ftdi:{self._ftdi.serial_number}', option, fallback=fallback)

    def _config_getboolean(self, option, fallback):
        return self._config.getboolean(f'ftdi:{self._ftdi.serial_number}', option, fallback=fallback)

    def _fuse_index(self, fuse):
        """ Translate a 0..3 fuse number to the dbus path index, honouring
        the mounted_upside_down config flag. """
        return (FUSES_PER_DISTRIBUTOR - 1) - fuse if self._config_getboolean('mounted_upside_down', False) else fuse

    def _set_distributor_lost(self, distributor):
        """ Mark a single distributor as Communications Lost (or Not
        Available if it isn't installed). Used by both the global
        invalidate-all path and the per-address fallback in _update. """
        installed = self._config_getboolean(f'distributor{distributor}Installed', False)
        self._dbusservice[f'/Distributor/{distributor}/Status'] = DISTRIBUTOR_STATUS_COMMS_LOST if installed else DISTRIBUTOR_STATUS_NOT_AVAILABLE
        self._dbusservice[f'/Distributor/{distributor}/Alarms/ConnectionLost'] = ALARM_ACTIVE if installed else ALARM_OK

    def _invalidate_all(self):
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

    def _update(self):
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
                # so we don't crash on `state & ...` with the NACK sentinel.
                if state is NACK:
                    self._set_distributor_lost(distributor)
                    continue

                no_bus_power = bool(state & BIT_NO_BUS_POWER)

                self._dbusservice[f'/Distributor/{distributor}/Status'] = (
                    DISTRIBUTOR_STATUS_NO_BUS_POWER if no_bus_power else DISTRIBUTOR_STATUS_CONNECTED
                )
                self._dbusservice[f'/Distributor/{distributor}/Alarms/ConnectionLost'] = ALARM_OK

                for fuse in range(FUSES_PER_DISTRIBUTOR):
                    fuse_index = self._fuse_index(fuse)
                    fuse_installed = self._config_getboolean(f'distributor{distributor}Fuse{fuse}Installed', True)
                    fuse_blown = bool(state & (BIT_FUSE0_BLOWN << fuse))

                    if not fuse_installed:
                        self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Status'] = FUSE_STATUS_NOT_USED
                        self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Alarms/Blown'] = ALARM_OK
                    elif no_bus_power:
                        self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Status'] = FUSE_STATUS_NOT_AVAILABLE
                        self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Alarms/Blown'] = ALARM_OK
                    elif fuse_blown:
                        self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Status'] = FUSE_STATUS_BLOWN
                        self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Alarms/Blown'] = ALARM_ACTIVE
                    else:
                        self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Status'] = FUSE_STATUS_OK
                        self._dbusservice[f'/Distributor/{distributor}/Fuse/{fuse_index}/Alarms/Blown'] = ALARM_OK

        except USBError as e:
            logging.error(f"USB communication failed: {e}")
            self._invalidate_all()
            self._reinit_pending = True
            # Keep the GLib timer alive so we can recover on the next tick.
            return True

        # Successful poll — make sure /Connected reflects reality even if
        # we recovered from a previous USBError.
        self._dbusservice['/Connected'] = CONNECTED_TRUE
        return True
