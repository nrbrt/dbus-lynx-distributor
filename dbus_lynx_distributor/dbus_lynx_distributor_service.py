import logging

from gi.repository import GLib
from vedbus import VeDbusService
from dbus import SystemBus
from usb.core import USBError

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
    decode_distributor_state,
)
from .ftdi import NACK


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
        self._timer_id = None

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
        self._timer_id = GLib.timeout_add(POLL_INTERVAL_MS, self._update)

    def close(self):
        """ Release the I2C controller and cancel the poll timer.

        Safe to call multiple times. Called from the main process's
        SIGTERM/SIGINT handler so a systemctl restart doesn't leave a
        stuck FTDI handle or a half-finished I2C transaction.
        """
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        try:
            self._ftdi.close()
        except Exception as e:  # noqa: BLE001 — last-ditch cleanup
            logging.warning(f"Error closing Ftdi during shutdown: {e}")

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

    def _publish_distributor(self, distributor, decoded):
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
        return True
