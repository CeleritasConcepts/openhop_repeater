"""Enable-pin polarity support for the pinned openhop_core 1.1.3 driver.

Keep the core's radio/SPI implementation and translate only TXEN/RXEN GPIO
levels, including their initial values. This can be removed when the pinned
core supports these options natively.
"""

import asyncio
import logging

from openhop_core.hardware.sx1262_wrapper import SX1262Radio as CoreSX1262Radio

logger = logging.getLogger(__name__)


class _EnablePinGPIO:
    """Per-radio GPIO view; never change a shared manager or module global."""

    def __init__(self, manager, polarities):
        self._manager = manager
        self._polarities = polarities
        self._configured = set()

    def __getattr__(self, name):
        return getattr(self._manager, name)

    def _physical_level(self, pin, asserted):
        return asserted if self._polarities.get(pin, True) else not asserted

    def setup_output_pin(self, pin_number, initial_value=False):
        result = self._manager.setup_output_pin(
            pin_number, initial_value=self._physical_level(pin_number, initial_value)
        )
        if pin_number in self._polarities:
            if not result:
                raise RuntimeError(f"Could not configure enable pin GPIO{pin_number}")
            self._configured.add(pin_number)
        return result

    def _write(self, pin, asserted):
        write = (
            self._manager.set_pin_high
            if self._physical_level(pin, asserted)
            else self._manager.set_pin_low
        )
        result = write(pin)
        if pin in self._polarities and not result:
            raise RuntimeError(f"Could not drive enable pin GPIO{pin}")
        return result

    def set_pin_high(self, pin_number):
        return self._write(pin_number, True)

    def set_pin_low(self, pin_number):
        return self._write(pin_number, False)

    def disable(self):
        for pin in sorted(self._configured):
            try:
                self.set_pin_low(pin)
            except Exception:
                logger.exception("Could not deactivate enable pin GPIO%s", pin)

    def cleanup_pin(self, pin_number):
        try:
            if pin_number in self._configured:
                self.set_pin_low(pin_number)
        finally:
            self._configured.discard(pin_number)
            self._manager.cleanup_pin(pin_number)

    def cleanup_all(self):
        self.disable()
        try:
            self._manager.cleanup_all()
        finally:
            self._configured.clear()


class SX1262Radio(CoreSX1262Radio):
    """Core SX1262 driver with configurable TXEN/RXEN polarity."""

    def __init__(self, *args, txen_active_high=True, rxen_active_high=True, **kwargs):
        for name, value in (
            ("txen_active_high", txen_active_high),
            ("rxen_active_high", rxen_active_high),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"sx1262.{name} must be a boolean")
        super().__init__(*args, **kwargs)
        self.txen_active_high = txen_active_high
        self.rxen_active_high = rxen_active_high
        self._gpio_manager = _EnablePinGPIO(
            self._gpio_manager,
            {
                pin: polarity
                for pin, polarity in (
                    (self.txen_pin, txen_active_high),
                    (self.rxen_pin, rxen_active_high),
                )
                if pin != -1
            },
        )

    def begin(self):
        try:
            if not super().begin():
                raise RuntimeError("SX1262 initialization failed")
            return True
        except Exception:
            self.cleanup()
            raise

    def _control_tx_rx_pins(self, tx_mode):
        # Disable the old path before enabling the new one. The GPIO view
        # translates these logical levels to each board's electrical polarity.
        inactive, active = (
            (self.rxen_pin, self.txen_pin) if tx_mode else (self.txen_pin, self.rxen_pin)
        )
        if inactive != -1:
            self._gpio_manager.set_pin_low(inactive)
        if active != -1:
            self._gpio_manager.set_pin_high(active)

    def sleep(self):
        super().sleep()
        self._gpio_manager.disable()

    async def _restore_rx_mode(self):
        # A timed-out/cancelled TX may still be on air. Stop it before the core
        # reconnects the LNA, and clear the busy flag even after a failed send.
        self._tx_buffer_busy = False
        if self.lora is not None:
            try:
                self.lora.setStandby(self.lora.STANDBY_RC)
                await asyncio.sleep(self.RADIO_TIMING_DELAY)
            except Exception:
                logger.exception("Could not stop TX; leaving the LNA bypassed")
                return
        await super()._restore_rx_mode()
