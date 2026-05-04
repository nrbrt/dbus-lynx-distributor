""" Pre-populate sys.modules with stubs for gi/vedbus/dbus/settingsdevice
so the service and __main__ modules can be imported on a developer
machine without the Cerbo runtime.

The actual logic of those libraries is irrelevant to our tests — we
only need the imports to succeed and a couple of attributes to behave
plausibly. Tests then attach their own MagicMocks where needed.

If the real gi is installed (e.g. on the Cerbo) we leave it alone.
"""

import importlib
import sys
import types
from unittest.mock import MagicMock


def _stub_module(name, **attrs):
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# gi + gi.repository.GLib
try:
    importlib.import_module("gi.repository")
except ImportError:
    glib_stub = MagicMock()
    glib_stub.PRIORITY_HIGH = 100
    glib_stub.timeout_add.return_value = 1   # fake timer source id
    glib_stub.unix_signal_add.return_value = 1
    _stub_module("gi")
    _stub_module("gi.repository", GLib=glib_stub)

# dbus + dbus.mainloop.glib
try:
    importlib.import_module("dbus")
except ImportError:
    _stub_module("dbus", SystemBus=MagicMock())
    _stub_module("dbus.mainloop")
    _stub_module("dbus.mainloop.glib", DBusGMainLoop=MagicMock())

# vedbus
try:
    importlib.import_module("vedbus")
except ImportError:
    _stub_module("vedbus", VeDbusService=MagicMock())

# settingsdevice
try:
    importlib.import_module("settingsdevice")
except ImportError:
    _stub_module("settingsdevice", SettingsDevice=MagicMock())
