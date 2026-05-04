#!/usr/bin/env python3

import logging
import signal
from argparse import ArgumentParser
from configparser import ConfigParser
from pathlib import Path
from typing import List, Optional

from gi.repository import GLib
from dbus import SystemBus

from settingsdevice import SettingsDevice
from .ftdi import Ftdi
from .dbus_lynx_distributor_service import DbusLynxDistributorService


class Application:
    def __init__(self) -> None:
        self._services: List[DbusLynxDistributorService] = []
        self._mainloop: Optional[GLib.MainLoop] = None

    def _parse_args(self) -> None:
        from . import __version__

        parser = ArgumentParser()

        parser.add_argument("-c", "--config", help="Specify config file", metavar="FILE", required=True, type=Path)
        parser.add_argument("-v", "--verbose", help="Increases log verbosity for each occurence", dest="verbose_count", action="count", default=0)
        parser.add_argument("--version", action="version", version=__version__)

        self._args = parser.parse_args()

        logging.basicConfig(format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s", level=max(3 - self._args.verbose_count, 0) * 10)

    def _read_config(self) -> None:
        self._config = ConfigParser()
        self._config.read(self._args.config)

    @staticmethod
    def _get_class_and_vrm_instance(*, serial_number: str, service: str) -> List[str]:
        settings = SettingsDevice(
            bus=SystemBus(),
            supportedSettings={'ClassAndVrmInstance': [f'/Settings/Devices/dbus_lynx_distributor_{serial_number}/ClassAndVrmInstance', f'{service}:1', 0, 0],},
            eventCallback=None,
            timeout=10)

        return settings['ClassAndVrmInstance'].split(':', 2)

    def run(self) -> None:
        self._parse_args()
        self._read_config()

        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)

        vid = int(self._config.get(section='general', option='vid', fallback='0x0403'), 0)
        pid = int(self._config.get(section='general', option='pid', fallback='0xD4F8'), 0)

        ftdis = Ftdi.scan(vid=vid, pid=pid)

        if len(ftdis) == 0:
            logging.warning("No devices found.")
            return

        for ftdi in ftdis:
            logging.info(f"Device with serial number {ftdi.serial_number} found.")

            service, device_instance = self._get_class_and_vrm_instance(serial_number=ftdi.serial_number, service='battery')
            service_name = f'com.victronenergy.{service}.dbus_lynx_distributor_{ftdi.serial_number}'

            self._services.append(DbusLynxDistributorService(
                service_name=service_name,
                device_instance=int(device_instance),
                ftdi=ftdi,
                config=self._config,
            ))

        self._mainloop = GLib.MainLoop()
        # Translate SIGTERM/SIGINT into a clean mainloop.quit() so the
        # cleanup path runs (i2c.terminate(), timer cancellation) instead
        # of the process being killed mid-I2C-transaction.
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self._on_signal, signal.SIGTERM)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self._on_signal, signal.SIGINT)

        try:
            self._mainloop.run()
        finally:
            self._shutdown()

    def _on_signal(self, signum: int) -> bool:
        logging.info(f"Received signal {signum}, shutting down")
        if self._mainloop is not None:
            self._mainloop.quit()
        return False  # returning False removes the GLib signal handler

    def _shutdown(self) -> None:
        for svc in self._services:
            try:
                svc.close()
            except Exception as e:  # noqa: BLE001 — best-effort cleanup
                logging.warning(f"Error closing service during shutdown: {e}")
        self._services.clear()


def main() -> None:
    """ Entry point for the ``dbus-lynx-distributor`` console script
    declared in pyproject.toml. """
    Application().run()


if __name__ == "__main__":
    main()
