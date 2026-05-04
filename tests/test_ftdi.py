""" Unit tests for the Ftdi I/O wrapper.

We mock the underlying pyftdi i2c controller so these tests run on any
machine with pyftdi installed — no FT232H hardware needed. The point is
to verify that the wrapper's contract (return values, sentinel handling,
close idempotency) is correct.
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyftdi")
from pyftdi.i2c import I2cNackError  # noqa: E402

from dbus_lynx_distributor.ftdi import NACK, Ftdi  # noqa: E402


def _make_ftdi_with_mock_i2c():
    """ Construct an Ftdi instance bypassing __init__ (which would touch
    pyusb and add_custom_devices). Inject a Mock i2c controller so we can
    drive its return values from each test. """
    f = Ftdi.__new__(Ftdi)
    f._dev = MagicMock()
    f.i2c = MagicMock()
    return f


# ---------- read_byte_and_send_nak ----------

def test_read_byte_returns_value_on_successful_read():
    f = _make_ftdi_with_mock_i2c()
    port = MagicMock()
    port.read.return_value = b'\x42'
    f.i2c.get_port.return_value = port

    assert f.read_byte_and_send_nak(0x08) == 0x42


def test_read_byte_returns_nack_sentinel_on_i2c_nack():
    f = _make_ftdi_with_mock_i2c()
    port = MagicMock()
    port.read.side_effect = I2cNackError("slave did not respond")
    f.i2c.get_port.return_value = port

    assert f.read_byte_and_send_nak(0x08) is NACK


def test_read_byte_returns_nack_sentinel_on_zero_length_read():
    """ A zero-length read should produce the NACK sentinel, not crash
    on an IndexError when the caller tries to use the value. """
    f = _make_ftdi_with_mock_i2c()
    port = MagicMock()
    port.read.return_value = b''
    f.i2c.get_port.return_value = port

    assert f.read_byte_and_send_nak(0x08) is NACK


def test_read_byte_zero_value_is_not_confused_with_nack():
    """ A valid read of 0x00 must return the int 0, NOT the NACK sentinel.
    This is the contract the new sentinel exists to make explicit. """
    f = _make_ftdi_with_mock_i2c()
    port = MagicMock()
    port.read.return_value = b'\x00'
    f.i2c.get_port.return_value = port

    result = f.read_byte_and_send_nak(0x08)
    assert result == 0
    assert result is not NACK


# ---------- send_addr_and_check_ack ----------

def test_send_addr_returns_true_on_successful_write():
    f = _make_ftdi_with_mock_i2c()
    port = MagicMock()
    port.write.return_value = None
    f.i2c.get_port.return_value = port

    assert f.send_addr_and_check_ack(0x08) is True


def test_send_addr_returns_false_on_i2c_nack():
    f = _make_ftdi_with_mock_i2c()
    port = MagicMock()
    port.write.side_effect = I2cNackError("slave did not respond")
    f.i2c.get_port.return_value = port

    assert f.send_addr_and_check_ack(0x08) is False


# ---------- close() idempotency ----------

def test_close_calls_terminate_on_active_controller():
    f = _make_ftdi_with_mock_i2c()
    mock_i2c = f.i2c
    f.close()

    mock_i2c.terminate.assert_called_once()
    assert f.i2c is None


def test_close_is_idempotent_when_called_twice():
    f = _make_ftdi_with_mock_i2c()
    mock_i2c = f.i2c
    f.close()
    f.close()

    assert f.i2c is None
    # terminate() called exactly once across both close() invocations.
    mock_i2c.terminate.assert_called_once()


def test_close_safe_when_i2c_never_initialised():
    """ close() must not blow up if init_i2c() was never called. """
    f = Ftdi.__new__(Ftdi)
    f._dev = MagicMock()
    f.i2c = None

    f.close()  # should not raise

    assert f.i2c is None
