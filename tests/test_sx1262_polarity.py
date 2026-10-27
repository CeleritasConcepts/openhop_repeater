"""Exercise the real pinned core's init/send/cleanup with simulated GPIO/SPI."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import cherrypy
import pytest
import yaml
from openhop_core.hardware import sx1262_wrapper as core

from repeater.config_manager import ConfigManager
from repeater.sx1262_radio import SX1262Radio
from repeater.web.api_endpoints import APIEndpoints


class FakeGPIO:
    def __init__(self):
        self.levels = {}
        self.events = []
        self.fail_pin = None

    def setup_interrupt_pin(self, *args, **kwargs):
        return object()

    def setup_output_pin(self, pin_number, initial_value=False):
        self.events.append(("setup", pin_number, initial_value))
        self.levels[pin_number] = initial_value
        return True

    def _write(self, pin, value):
        if pin == self.fail_pin:
            return False
        self.events.append(("write", pin, value))
        self.levels[pin] = value
        return True

    def set_pin_high(self, pin):
        return self._write(pin, True)

    def set_pin_low(self, pin):
        return self._write(pin, False)

    def blink_led(self, pin):
        pass

    def cleanup_pin(self, pin):
        self.events.append(("release", pin, self.levels.pop(pin, None)))

    def cleanup_all(self):
        for pin in list(self.levels):
            self.cleanup_pin(pin)


@pytest.fixture
def radio_factory(monkeypatch):
    chip_type = core.SX126x
    radios = []
    monkeypatch.setattr(core.time, "sleep", lambda _: None)
    monkeypatch.setattr(SX1262Radio, "_bind_instance_spi_transport", lambda _: None)

    def create(**kwargs):
        gpio = kwargs.pop("gpio_manager", FakeGPIO())
        chip = MagicMock(spec=chip_type)
        for name in dir(chip_type):
            if name.isupper():
                setattr(chip, name, getattr(chip_type, name))
        chip.busyCheck.return_value = False
        chip.readRegister.return_value = [0]
        chip.getIrqStatus.return_value = chip.IRQ_TX_DONE
        chip.transmitTime.return_value = 0
        monkeypatch.setattr(core, "SX126x", lambda: chip)
        radio = SX1262Radio(gpio_manager=gpio, radio_timing_delay=0, **kwargs)
        radio.RADIO_TIMING_DELAY = 0
        # Do not create the IRQ polling task in async tests.
        radio._rx_irq_task = MagicMock()
        radio._rx_irq_task.done.return_value = False
        radios.append(radio)
        assert radio.begin()
        return radio, gpio, chip

    yield create
    for radio in radios:
        radio.cleanup()


@pytest.mark.parametrize("tx_high", [True, False])
@pytest.mark.parametrize("rx_high", [True, False])
def test_enable_polarities_and_transition_order(radio_factory, tx_high, rx_high):
    radio, gpio, _ = radio_factory(
        txen_pin=13, rxen_pin=23, txen_active_high=tx_high, rxen_active_high=rx_high
    )
    assert ("setup", 13, not tx_high) in gpio.events
    assert ("setup", 23, not rx_high) in gpio.events
    assert gpio.levels == {13: not tx_high, 23: rx_high}
    radio._control_tx_rx_pins(True)
    assert gpio.events[-2:] == [("write", 23, not rx_high), ("write", 13, tx_high)]
    radio._control_tx_rx_pins(False)
    assert gpio.events[-2:] == [("write", 13, not tx_high), ("write", 23, rx_high)]


def test_existing_defaults_and_disabled_pins(radio_factory):
    radio, gpio, _ = radio_factory(txen_pin=13, rxen_pin=12)
    assert gpio.levels == {13: False, 12: True}
    radio._control_tx_rx_pins(True)
    assert gpio.levels == {13: True, 12: False}
    disabled, gpio, _ = radio_factory(txen_pin=-1, rxen_pin=-1, rxen_active_high=False)
    disabled._control_tx_rx_pins(True)
    disabled._control_tx_rx_pins(False)
    assert gpio.events == []


@pytest.mark.parametrize("outcome", ["success", "not_ready", "start_failed", "timeout", "cancel"])
@pytest.mark.asyncio
async def test_station_g3_restores_lna_after_send(radio_factory, outcome):
    radio, gpio, chip = radio_factory(txen_pin=-1, rxen_pin=23, rxen_active_high=False)
    assert gpio.levels[23] is False

    async def prepare():
        radio._control_tx_rx_pins(True)
        return outcome != "not_ready", []

    async def execute(_timeout):
        assert gpio.levels[23] is True  # LNA bypassed before starting TX.
        if outcome == "cancel":
            raise asyncio.CancelledError
        return outcome != "start_failed"

    def standby(_mode):
        gpio.events.append(("standby",))

    chip.setStandby.side_effect = standby

    radio._prepare_radio_for_tx = prepare
    radio._execute_transmission = execute
    radio._wait_for_transmission_complete = AsyncMock(return_value=outcome != "timeout")
    if outcome == "success":
        assert "airtime_ms" in await radio.send(b"test")
    else:
        error = asyncio.CancelledError if outcome == "cancel" else RuntimeError
        with pytest.raises(error):
            await radio.send(b"test")
    assert gpio.levels[23] is False
    assert radio._tx_buffer_busy is False
    if outcome != "success":
        last_tx = max(i for i, event in enumerate(gpio.events) if event == ("write", 23, True))
        recovery = gpio.events[last_tx + 1 :]
        assert recovery.index(("standby",)) < recovery.index(("write", 23, False))
    chip.request.assert_called_with(chip.RX_CONTINUOUS)


@pytest.mark.parametrize("owns_gpio", [True, False])
def test_cleanup_bypasses_lna_before_release(radio_factory, owns_gpio):
    radio, gpio, _ = radio_factory(txen_pin=13, rxen_pin=23, rxen_active_high=False)
    radio._owns_gpio_manager = owns_gpio
    radio.cleanup()
    assert ("release", 23, True) in gpio.events
    assert ("release", 13, False) in gpio.events
    assert radio._initialized is False


def test_sleep_bypasses_lna(radio_factory):
    radio, gpio, chip = radio_factory(txen_pin=-1, rxen_pin=23, rxen_active_high=False)
    radio.sleep()
    chip.sleep.assert_called_once()
    assert gpio.levels[23] is True


def test_failed_init_releases_enable_pins(radio_factory, monkeypatch):
    monkeypatch.setattr(SX1262Radio, "_basic_radio_setup", lambda *args, **kwargs: False)
    gpio = FakeGPIO()
    with pytest.raises(RuntimeError, match="initialization failed"):
        radio_factory(gpio_manager=gpio, txen_pin=-1, rxen_pin=23, rxen_active_high=False)
    assert ("release", 23, True) in gpio.events


def test_failed_lna_bypass_does_not_enable_tx(radio_factory):
    radio, gpio, _ = radio_factory(txen_pin=13, rxen_pin=23, rxen_active_high=False)
    gpio.fail_pin = 23
    with pytest.raises(RuntimeError, match="GPIO23"):
        radio._control_tx_rx_pins(True)
    assert gpio.levels[13] is False
    gpio.fail_pin = None


def test_shared_manager_keeps_other_radio_and_led_polarity(radio_factory):
    gpio = FakeGPIO()
    first, _, _ = radio_factory(
        gpio_manager=gpio, txen_pin=-1, rxen_pin=23, rxen_active_high=False, txled_pin=25
    )
    second, _, _ = radio_factory(gpio_manager=gpio, txen_pin=13, rxen_pin=12)
    assert gpio.levels[25] is False
    first._control_tx_rx_pins(True)
    assert gpio.levels[12] is True
    first.cleanup()
    assert gpio.levels[12] is True
    second._control_tx_rx_pins(True)
    assert gpio.levels[13] is True


@pytest.mark.parametrize("value", ["false", 0, None])
def test_polarity_requires_yaml_boolean(value):
    with pytest.raises(ValueError, match="must be a boolean"):
        SX1262Radio(rxen_active_high=value)


@pytest.mark.parametrize("power", [19, 0, -9])
@pytest.mark.parametrize("apply_ok", [True, False])
def test_web_tx_power_reaches_core_driver_and_persists(
    radio_factory, tmp_path, monkeypatch, power, apply_ok
):
    radio, gpio, chip = radio_factory(txen_pin=-1, rxen_pin=23, rxen_active_high=False)
    config = {
        "radio_type": "sx1262",
        "radio": {
            "frequency": radio.frequency,
            "bandwidth": radio.bandwidth,
            "spreading_factor": radio.spreading_factor,
            "coding_rate": radio.coding_rate,
            "tx_power": radio.tx_power,
        },
    }
    path = tmp_path / "config.yaml"
    handler = SimpleNamespace(radio_config=dict(config["radio"]))
    daemon = SimpleNamespace(config=config, radio=radio, repeater_handler=handler)
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    api.config_manager = ConfigManager(str(path), config, daemon)
    monkeypatch.setattr(
        cherrypy, "request", SimpleNamespace(method="POST", json={"tx_power": power})
    )
    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}, status=200))
    chip.setTxPower.reset_mock()
    if not apply_ok:
        chip.setTxPower.side_effect = OSError("SPI write failed")

    result = api.update_radio_config()

    assert result["success"] is True
    assert result["data"]["persisted"] is True
    assert result["data"]["live_update"] is apply_ok
    assert result["data"]["restart_required"] is not apply_ok
    assert yaml.safe_load(path.read_text())["radio"]["tx_power"] == power
    chip.setTxPower.assert_called_once_with(power, chip.TX_POWER_SX1262)
    if apply_ok:
        assert radio.tx_power == handler.radio_config["tx_power"] == power
    assert gpio.levels[23] is False
