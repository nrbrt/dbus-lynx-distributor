""" Unit tests for decoder.decode_distributor_state.

The decoding rules under test (see README "Hardware/Background"):

    state byte = 0b00000000  -> all OK
    state byte = 0b00000010  -> no bus power
    state byte = 0b00010000  -> fuse 0 blown
    state byte = 0b00100000  -> fuse 1 blown
    state byte = 0b01000000  -> fuse 2 blown
    state byte = 0b10000000  -> fuse 3 blown

Multiple bits may be set simultaneously (the device just ORs them).
"""

import pytest

from dbus_lynx_distributor.decoder import (
    ALARM_ACTIVE,
    ALARM_OK,
    DISTRIBUTOR_STATUS_CONNECTED,
    DISTRIBUTOR_STATUS_NO_BUS_POWER,
    FUSES_PER_DISTRIBUTOR,
    FUSE_STATUS_BLOWN,
    FUSE_STATUS_NOT_AVAILABLE,
    FUSE_STATUS_NOT_USED,
    FUSE_STATUS_OK,
    decode_distributor_state,
)


ALL_INSTALLED = (True, True, True, True)
NONE_INSTALLED = (False, False, False, False)


def test_all_ok():
    """ State 0x00 with all fuses installed -> connected, all fuses OK. """
    decoded = decode_distributor_state(0b0000_0000, ALL_INSTALLED)

    assert decoded.status == DISTRIBUTOR_STATUS_CONNECTED
    assert decoded.connection_lost_alarm == ALARM_OK
    assert all(f.status == FUSE_STATUS_OK for f in decoded.fuses)
    assert all(f.alarm == ALARM_OK for f in decoded.fuses)


def test_no_bus_power_overrides_fuse_status():
    """ With bus power lost we report fuses as NOT_AVAILABLE regardless of
    the fuse-blown bits in the byte. The device pulls those bits high
    when the supply is missing, but it isn't actionable info. """
    decoded = decode_distributor_state(0b1111_0010, ALL_INSTALLED)

    assert decoded.status == DISTRIBUTOR_STATUS_NO_BUS_POWER
    assert all(f.status == FUSE_STATUS_NOT_AVAILABLE for f in decoded.fuses)
    assert all(f.alarm == ALARM_OK for f in decoded.fuses)


@pytest.mark.parametrize("fuse_idx,bit", [
    (0, 0b0001_0000),
    (1, 0b0010_0000),
    (2, 0b0100_0000),
    (3, 0b1000_0000),
])
def test_single_blown_fuse(fuse_idx, bit):
    """ Each fuse-blown bit lights up exactly the corresponding fuse. """
    decoded = decode_distributor_state(bit, ALL_INSTALLED)

    assert decoded.status == DISTRIBUTOR_STATUS_CONNECTED

    for i, fuse_state in enumerate(decoded.fuses):
        if i == fuse_idx:
            assert fuse_state.status == FUSE_STATUS_BLOWN
            assert fuse_state.alarm == ALARM_ACTIVE
        else:
            assert fuse_state.status == FUSE_STATUS_OK
            assert fuse_state.alarm == ALARM_OK


def test_all_blown_simultaneously():
    """ All four fuse bits set at once -> all four reported as blown. """
    decoded = decode_distributor_state(0b1111_0000, ALL_INSTALLED)

    assert decoded.status == DISTRIBUTOR_STATUS_CONNECTED
    assert all(f.status == FUSE_STATUS_BLOWN for f in decoded.fuses)
    assert all(f.alarm == ALARM_ACTIVE for f in decoded.fuses)


def test_uninstalled_fuse_reports_not_used_even_if_bit_set():
    """ A fuse marked as not-installed in config must report NOT_USED even
    if its blown-bit happens to be set in the byte (e.g. floating I2C
    line, hardware quirk). """
    fuse_installed = (True, False, True, True)
    state_byte = 0b0010_0000  # claims fuse 1 blown
    decoded = decode_distributor_state(state_byte, fuse_installed)

    assert decoded.fuses[1].status == FUSE_STATUS_NOT_USED
    assert decoded.fuses[1].alarm == ALARM_OK
    # The other fuses should be unaffected.
    assert decoded.fuses[0].status == FUSE_STATUS_OK
    assert decoded.fuses[2].status == FUSE_STATUS_OK
    assert decoded.fuses[3].status == FUSE_STATUS_OK


def test_no_fuses_installed_no_bus_power():
    """ Distributor connected but no bus power and zero fuses configured
    as installed: all NOT_USED, distributor status NO_BUS_POWER. """
    decoded = decode_distributor_state(0b0000_0010, NONE_INSTALLED)

    assert decoded.status == DISTRIBUTOR_STATUS_NO_BUS_POWER
    assert all(f.status == FUSE_STATUS_NOT_USED for f in decoded.fuses)


def test_decoder_never_raises_on_any_byte_value():
    """ Property-style: every possible byte value plus every install-flag
    combination produces a valid DistributorState. No crashes, no
    invalid status enum values. """
    valid_distributor_status = {DISTRIBUTOR_STATUS_CONNECTED, DISTRIBUTOR_STATUS_NO_BUS_POWER}
    valid_fuse_status = {FUSE_STATUS_NOT_AVAILABLE, FUSE_STATUS_NOT_USED, FUSE_STATUS_OK, FUSE_STATUS_BLOWN}

    for state in range(256):
        for mask in range(16):
            installed = tuple(bool(mask & (1 << i)) for i in range(4))
            decoded = decode_distributor_state(state, installed)

            assert decoded.status in valid_distributor_status
            assert decoded.connection_lost_alarm == ALARM_OK
            assert len(decoded.fuses) == FUSES_PER_DISTRIBUTOR
            for f in decoded.fuses:
                assert f.status in valid_fuse_status
                assert f.alarm in (ALARM_OK, ALARM_ACTIVE)


def test_wrong_fuse_installed_length_raises():
    with pytest.raises(ValueError, match="must have 4 entries"):
        decode_distributor_state(0, (True, True, True))
    with pytest.raises(ValueError, match="must have 4 entries"):
        decode_distributor_state(0, (True, True, True, True, True))


def test_alarm_only_on_blown_fuse():
    """ ALARM_ACTIVE is only ever raised on a blown fuse — never on
    NOT_USED, NOT_AVAILABLE, or OK. This is a regression guard: the
    fuse-blown alarm path exists *only* in the blown branch. """
    for state in range(256):
        decoded = decode_distributor_state(state, ALL_INSTALLED)
        for f in decoded.fuses:
            if f.status == FUSE_STATUS_BLOWN:
                assert f.alarm == ALARM_ACTIVE
            else:
                assert f.alarm == ALARM_OK
