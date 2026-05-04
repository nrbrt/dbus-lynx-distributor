""" Unit tests for the SIGTERM/SIGINT handling in __main__.Application. """

from unittest.mock import MagicMock

from dbus_lynx_distributor.__main__ import Application


def test_on_signal_quits_mainloop():
    app = Application()
    app._mainloop = MagicMock()

    result = app._on_signal(15)  # SIGTERM

    app._mainloop.quit.assert_called_once()
    # Returning False removes the GLib signal handler — important so a
    # second SIGTERM during shutdown doesn't fire the handler again.
    assert result is False


def test_on_signal_safe_when_no_mainloop():
    """ If a signal arrives before the mainloop is set up (start-up
    window), _on_signal must not crash. """
    app = Application()
    assert app._mainloop is None

    result = app._on_signal(2)  # SIGINT

    assert result is False  # still removes handler


def test_shutdown_closes_each_service():
    app = Application()
    svc1 = MagicMock()
    svc2 = MagicMock()
    app._services = [svc1, svc2]

    app._shutdown()

    svc1.close.assert_called_once()
    svc2.close.assert_called_once()
    assert app._services == []


def test_shutdown_continues_when_one_service_close_raises():
    """ If one service's close() raises, the others must still get a
    chance to clean up. """
    app = Application()
    svc1 = MagicMock()
    svc1.close.side_effect = RuntimeError("boom")
    svc2 = MagicMock()
    app._services = [svc1, svc2]

    app._shutdown()  # must not raise

    svc1.close.assert_called_once()
    svc2.close.assert_called_once()
    assert app._services == []
