""" Pure decoding logic for the Lynx Distributor I2C status byte.

Kept dependency-free (no vedbus, no gi, no usb) so it can be unit-tested
on a developer machine without the Cerbo runtime.
"""

from dataclasses import dataclass
from typing import Sequence, Tuple


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


@dataclass(frozen=True)
class FuseState:
    status: int   # one of FUSE_STATUS_*
    alarm: int    # one of ALARM_*


@dataclass(frozen=True)
class DistributorState:
    status: int                  # one of DISTRIBUTOR_STATUS_*
    connection_lost_alarm: int   # one of ALARM_*
    fuses: Tuple[FuseState, ...]  # length FUSES_PER_DISTRIBUTOR, indexed by physical fuse 0..3


def decode_distributor_state(state_byte: int, fuse_installed: Sequence[bool]) -> DistributorState:
    """ Decode the raw I2C status byte for a single distributor.

    The byte format (see README, "Hardware/Background"):
        bit 1: 1 = no bus power (centre LED orange)
        bit 4: 1 = fuse 0 blown
        bit 5: 1 = fuse 1 blown
        bit 6: 1 = fuse 2 blown
        bit 7: 1 = fuse 3 blown

    fuse_installed: a length-4 sequence of booleans indexed by *physical*
        fuse number on the device (NOT the dbus path index — flipping for
        ``mounted_upside_down`` happens in the dbus-publishing layer).

    Returns a fully-populated DistributorState. The function never raises
    on a valid integer state_byte — including ``0`` (everything OK) and
    ``0xFF`` (everything wrong).
    """
    if len(fuse_installed) != FUSES_PER_DISTRIBUTOR:
        raise ValueError(f"fuse_installed must have {FUSES_PER_DISTRIBUTOR} entries, got {len(fuse_installed)}")

    no_bus_power = bool(state_byte & BIT_NO_BUS_POWER)

    fuses = []
    for fuse in range(FUSES_PER_DISTRIBUTOR):
        if not fuse_installed[fuse]:
            fuses.append(FuseState(FUSE_STATUS_NOT_USED, ALARM_OK))
        elif no_bus_power:
            # Without bus power we can't tell what the fuses are doing; the
            # device reports them as blown via pull-up bits, but that's not
            # actionable info while the supply is missing.
            fuses.append(FuseState(FUSE_STATUS_NOT_AVAILABLE, ALARM_OK))
        elif state_byte & (BIT_FUSE0_BLOWN << fuse):
            fuses.append(FuseState(FUSE_STATUS_BLOWN, ALARM_ACTIVE))
        else:
            fuses.append(FuseState(FUSE_STATUS_OK, ALARM_OK))

    return DistributorState(
        status=DISTRIBUTOR_STATUS_NO_BUS_POWER if no_bus_power else DISTRIBUTOR_STATUS_CONNECTED,
        connection_lost_alarm=ALARM_OK,
        fuses=tuple(fuses),
    )
