import logging
from typing import List, Optional, Union

import usb
from pyftdi.ftdi import Ftdi as PyFtdi
from pyftdi.i2c import I2cController, I2cNackError
from pyftdi.misc import add_custom_devices

# Silence pyftdi's chatty INFO/DEBUG logging at module load.
logging.getLogger('pyftdi').setLevel(logging.ERROR)

# Sentinel returned by read_byte_and_send_nak when the slave NACKs the read.
NACK = object()


class Ftdi:

    @classmethod
    def scan(cls, vid: int = 0x0403, pid: int = 0xD4F8) -> List["Ftdi"]:
        """ Scan for USB devices matching vid/pid and return an Ftdi
        wrapper for each one. """
        return [cls(dev) for dev in usb.core.find(find_all=True, idVendor=vid, idProduct=pid)]

    def __init__(self, dev) -> None:
        self._dev = dev
        add_custom_devices(PyFtdi, [f'{dev.idVendor:04x}:{dev.idProduct:04x}'], force_hex=True)
        self.i2c: Optional[I2cController] = None

    @property
    def serial_number(self) -> str:
        return self._dev.serial_number

    @property
    def pid(self) -> str:
        return f'0x{self._dev.idProduct:04x}'

    def init_i2c(self) -> None:
        i2c = I2cController()
        # 3 retries lets transient bus glitches recover before propagating an
        # I2cNackError to the caller; each retry costs <10ms.
        i2c.set_retry_count(3)
        # Address format: ('ftdi://ftdi:pid:serial/1')
        i2c.configure(f'ftdi://ftdi:{self.pid}:{self.serial_number}/1')
        self.i2c = i2c

    def read_byte_and_send_nak(self, address: int) -> Union[int, object]:
        """ Read one byte from the given I2C address.

        Returns the byte value (int 0-255) on success, or the sentinel
        ``NACK`` if the slave didn't acknowledge or returned an empty
        read. The caller must distinguish ``NACK`` from a valid ``0x00``
        byte value.
        """
        try:
            port = self.i2c.get_port(address)
            data_read = port.read(1)
        except I2cNackError:
            return NACK
        if len(data_read) == 0:
            return NACK
        return data_read[0]

    def send_addr_and_check_ack(self, address: int, read: bool = True) -> bool:
        port = self.i2c.get_port(address)
        try:
            port.write([])
            return True
        except I2cNackError:
            return False

    def close(self) -> None:
        """ Release the underlying pyftdi I2C controller. Safe to call
        repeatedly; safe to call when init_i2c() never ran. """
        if self.i2c is not None:
            try:
                self.i2c.terminate()
            finally:
                self.i2c = None
