""" Unit tests for DbusLynxDistributorService methods that don't require
a live dbus connection.

We bypass __init__ via __new__ so we don't need to construct a full
VeDbusService. Tests attach a dict-like mock (FakeDbusService) to
self._dbusservice so we can assert which paths the service writes to.
"""

from configparser import ConfigParser
from unittest.mock import MagicMock

from dbus_lynx_distributor.dbus_lynx_distributor_service import DbusLynxDistributorService
from dbus_lynx_distributor.decoder import (
    ALARM_ACTIVE,
    ALARM_OK,
    CONNECTED_FALSE,
    CONNECTED_TRUE,
    DISTRIBUTOR_STATUS_COMMS_LOST,
    DISTRIBUTOR_STATUS_CONNECTED,
    DISTRIBUTOR_STATUS_NOT_AVAILABLE,
    FUSE_STATUS_BLOWN,
    FUSE_STATUS_NOT_AVAILABLE,
    FUSE_STATUS_OK,
    decode_distributor_state,
)
from dbus_lynx_distributor.ftdi import NACK


class FakeDbusService(dict):
    """ Stand-in for VeDbusService — supports ``svc[path] = value`` exactly
    as the real one does for our purposes. """


def _make_service(*, mounted_upside_down=False, fuses_installed=None):
    """ Build a DbusLynxDistributorService instance suitable for unit
    tests, without invoking __init__ (which would touch dbus and
    vedbus). Attach a FakeDbusService and a real ConfigParser primed
    with the requested options. """
    if fuses_installed is None:
        fuses_installed = {dist: [True] * 4 for dist in 'ABCD'}

    config = ConfigParser()
    config['general'] = {}
    config['ftdi:FAKE001'] = {}
    if mounted_upside_down:
        config['ftdi:FAKE001']['mounted_upside_down'] = 'True'
    for distributor, flags in fuses_installed.items():
        config['ftdi:FAKE001'][f'distributor{distributor}Installed'] = 'True'
        for fuse, installed in enumerate(flags):
            config['ftdi:FAKE001'][f'distributor{distributor}Fuse{fuse}Installed'] = str(installed)

    svc = DbusLynxDistributorService.__new__(DbusLynxDistributorService)
    svc._ftdi = MagicMock()
    svc._ftdi.serial_number = 'FAKE001'
    svc._config = config
    svc._dbusservice = FakeDbusService()
    svc._reinit_pending = False
    svc._timer_id = None

    # Initial dbus paths the real __init__ would have created.
    for distributor in 'ABCD':
        svc._dbusservice[f'/Distributor/{distributor}/Status'] = None
        svc._dbusservice[f'/Distributor/{distributor}/Alarms/ConnectionLost'] = None
        for fuse in range(4):
            idx = svc._fuse_index(fuse)
            svc._dbusservice[f'/Distributor/{distributor}/Fuse/{idx}/Status'] = None
            svc._dbusservice[f'/Distributor/{distributor}/Fuse/{idx}/Alarms/Blown'] = None
    svc._dbusservice['/Connected'] = CONNECTED_TRUE

    return svc


# ---------- _fuse_index ----------

def test_fuse_index_normal_mounting():
    svc = _make_service(mounted_upside_down=False)
    assert [svc._fuse_index(i) for i in range(4)] == [0, 1, 2, 3]


def test_fuse_index_upside_down():
    svc = _make_service(mounted_upside_down=True)
    assert [svc._fuse_index(i) for i in range(4)] == [3, 2, 1, 0]


# ---------- _set_distributor_lost ----------

def test_set_distributor_lost_when_installed():
    svc = _make_service()
    svc._set_distributor_lost('A')
    assert svc._dbusservice['/Distributor/A/Status'] == DISTRIBUTOR_STATUS_COMMS_LOST
    assert svc._dbusservice['/Distributor/A/Alarms/ConnectionLost'] == ALARM_ACTIVE


def test_set_distributor_lost_when_not_installed():
    svc = _make_service()
    # mark distributor B as NOT installed
    svc._config['ftdi:FAKE001']['distributorBInstalled'] = 'False'
    svc._set_distributor_lost('B')
    assert svc._dbusservice['/Distributor/B/Status'] == DISTRIBUTOR_STATUS_NOT_AVAILABLE
    assert svc._dbusservice['/Distributor/B/Alarms/ConnectionLost'] == ALARM_OK


# ---------- _publish_distributor ----------

def test_publish_distributor_all_ok():
    svc = _make_service()
    decoded = decode_distributor_state(0b0000_0000, (True, True, True, True))
    svc._publish_distributor('A', decoded)

    assert svc._dbusservice['/Distributor/A/Status'] == DISTRIBUTOR_STATUS_CONNECTED
    assert svc._dbusservice['/Distributor/A/Alarms/ConnectionLost'] == ALARM_OK
    for fuse in range(4):
        assert svc._dbusservice[f'/Distributor/A/Fuse/{fuse}/Status'] == FUSE_STATUS_OK
        assert svc._dbusservice[f'/Distributor/A/Fuse/{fuse}/Alarms/Blown'] == ALARM_OK


def test_publish_distributor_fuse_2_blown():
    svc = _make_service()
    decoded = decode_distributor_state(0b0100_0000, (True, True, True, True))
    svc._publish_distributor('A', decoded)

    assert svc._dbusservice['/Distributor/A/Status'] == DISTRIBUTOR_STATUS_CONNECTED
    assert svc._dbusservice['/Distributor/A/Fuse/0/Status'] == FUSE_STATUS_OK
    assert svc._dbusservice['/Distributor/A/Fuse/1/Status'] == FUSE_STATUS_OK
    assert svc._dbusservice['/Distributor/A/Fuse/2/Status'] == FUSE_STATUS_BLOWN
    assert svc._dbusservice['/Distributor/A/Fuse/2/Alarms/Blown'] == ALARM_ACTIVE
    assert svc._dbusservice['/Distributor/A/Fuse/3/Status'] == FUSE_STATUS_OK


def test_publish_distributor_upside_down_swaps_fuse_paths():
    """ With upside-down mounting, physical fuse 0 must end up on dbus
    path /Fuse/3, fuse 1 on /Fuse/2, etc. """
    svc = _make_service(mounted_upside_down=True)
    # Physical fuse 0 blown
    decoded = decode_distributor_state(0b0001_0000, (True, True, True, True))
    svc._publish_distributor('A', decoded)

    assert svc._dbusservice['/Distributor/A/Fuse/3/Status'] == FUSE_STATUS_BLOWN  # was physical 0
    assert svc._dbusservice['/Distributor/A/Fuse/2/Status'] == FUSE_STATUS_OK
    assert svc._dbusservice['/Distributor/A/Fuse/1/Status'] == FUSE_STATUS_OK
    assert svc._dbusservice['/Distributor/A/Fuse/0/Status'] == FUSE_STATUS_OK


# ---------- _invalidate_all ----------

def test_invalidate_all_sets_connected_false():
    svc = _make_service()
    svc._invalidate_all()
    assert svc._dbusservice['/Connected'] == CONNECTED_FALSE


def test_invalidate_all_marks_installed_distributors_lost():
    svc = _make_service()
    svc._invalidate_all()
    for distributor in 'ABCD':
        assert svc._dbusservice[f'/Distributor/{distributor}/Status'] == DISTRIBUTOR_STATUS_COMMS_LOST
        assert svc._dbusservice[f'/Distributor/{distributor}/Alarms/ConnectionLost'] == ALARM_ACTIVE


def test_invalidate_all_clears_fuse_alarms():
    """ Even a previously-blown fuse should have its alarm cleared during
    invalidation — we don't actually know what's going on with the
    fuses if the bus is down. """
    svc = _make_service()
    # Pretend a previous poll saw fuse 1 blown.
    svc._dbusservice['/Distributor/A/Fuse/1/Status'] = FUSE_STATUS_BLOWN
    svc._dbusservice['/Distributor/A/Fuse/1/Alarms/Blown'] = ALARM_ACTIVE

    svc._invalidate_all()

    assert svc._dbusservice['/Distributor/A/Fuse/1/Status'] == FUSE_STATUS_NOT_AVAILABLE
    assert svc._dbusservice['/Distributor/A/Fuse/1/Alarms/Blown'] == ALARM_OK


# ---------- _update full flow ----------

def test_update_publishes_decoded_state_for_all_distributors():
    svc = _make_service()
    # All distributors return state 0x00 (everything OK).
    svc._ftdi.send_addr_and_check_ack.return_value = True
    svc._ftdi.read_byte_and_send_nak.return_value = 0x00

    result = svc._update()

    assert result is True  # GLib timer must remain alive
    for distributor in 'ABCD':
        assert svc._dbusservice[f'/Distributor/{distributor}/Status'] == DISTRIBUTOR_STATUS_CONNECTED
    assert svc._dbusservice['/Connected'] == CONNECTED_TRUE


def test_update_handles_addr_nack_per_distributor():
    """ A NACK on the addressing of one distributor should mark only
    that one as Communications Lost, not affect the others. """
    svc = _make_service()
    # Distributor B (lynx index 1, address 0x09) doesn't ack.

    def addr_check(address):
        return address != 0x09  # everything except B answers

    svc._ftdi.send_addr_and_check_ack.side_effect = addr_check
    svc._ftdi.read_byte_and_send_nak.return_value = 0x00

    svc._update()

    assert svc._dbusservice['/Distributor/A/Status'] == DISTRIBUTOR_STATUS_CONNECTED
    assert svc._dbusservice['/Distributor/B/Status'] == DISTRIBUTOR_STATUS_COMMS_LOST
    assert svc._dbusservice['/Distributor/C/Status'] == DISTRIBUTOR_STATUS_CONNECTED
    assert svc._dbusservice['/Distributor/D/Status'] == DISTRIBUTOR_STATUS_CONNECTED


def test_update_handles_data_nack_without_crash():
    """ The NACK sentinel from read_byte_and_send_nak must NOT propagate
    into ``state & ...`` and crash with TypeError. This was the
    original bug in the upstream code. """
    svc = _make_service()
    svc._ftdi.send_addr_and_check_ack.return_value = True
    svc._ftdi.read_byte_and_send_nak.return_value = NACK

    # Must not raise.
    result = svc._update()

    assert result is True
    for distributor in 'ABCD':
        assert svc._dbusservice[f'/Distributor/{distributor}/Status'] == DISTRIBUTOR_STATUS_COMMS_LOST


def test_update_on_usb_error_keeps_timer_alive_and_invalidates():
    """ The previous code returned False from _update on USBError, which
    killed the GLib timer permanently. The new code must return True
    and invalidate. """
    from usb.core import USBError
    svc = _make_service()
    svc._ftdi.send_addr_and_check_ack.side_effect = USBError("device disappeared")

    result = svc._update()

    assert result is True
    assert svc._reinit_pending is True
    assert svc._dbusservice['/Connected'] == CONNECTED_FALSE
    for distributor in 'ABCD':
        assert svc._dbusservice[f'/Distributor/{distributor}/Status'] == DISTRIBUTOR_STATUS_COMMS_LOST


def test_update_recovers_after_usb_error():
    """ When _reinit_pending is set, the next _update must call init_i2c
    again and resume polling on success. """
    svc = _make_service()
    svc._reinit_pending = True
    svc._ftdi.send_addr_and_check_ack.return_value = True
    svc._ftdi.read_byte_and_send_nak.return_value = 0x00

    svc._update()

    svc._ftdi.init_i2c.assert_called_once()
    assert svc._reinit_pending is False
    assert svc._dbusservice['/Connected'] == CONNECTED_TRUE


def test_update_reinit_still_failing_keeps_pending_flag():
    """ If init_i2c still fails on the recovery tick, the pending flag
    stays set and we try again next tick. """
    from usb.core import USBError
    svc = _make_service()
    svc._reinit_pending = True
    svc._ftdi.init_i2c.side_effect = USBError("still no")

    result = svc._update()

    assert result is True
    assert svc._reinit_pending is True


# ---------- close() ----------

def test_close_cancels_timer_and_closes_ftdi():
    from gi.repository import GLib  # this is the stub from conftest

    svc = _make_service()
    svc._timer_id = 42

    svc.close()

    GLib.source_remove.assert_called_with(42)
    svc._ftdi.close.assert_called_once()
    assert svc._timer_id is None


def test_close_is_safe_when_timer_never_started():
    svc = _make_service()
    assert svc._timer_id is None
    svc.close()  # must not raise
    svc._ftdi.close.assert_called_once()
