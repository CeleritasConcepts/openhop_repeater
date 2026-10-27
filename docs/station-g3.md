# BQ Station G3 external LNA control

For a Raspberry Pi in the Station G3's primary SPI slot, the `bq-station-g3`
preset controls the external LNA through BCM GPIO23. Remove the **LNA P jumper**
to allow GPIO control. See the [Station G3 hardware documentation](https://wiki.bqvoy.com/en/devkits/station-g3)
for the board's jumper and PA configuration.

The preset sets `sx1262.rxen_pin: 23` and `sx1262.rxen_active_high: false`.
GPIO23 is LOW during RX/idle and HIGH before TX. Successful, failed, timed-out,
and cancelled sends restore RX. Shutdown deactivates the LNA before releasing
the GPIO line; the electrical state after release depends on the board's bias
circuitry. This controls the external LNA, independently of SX1262 boosted RX gain.

Both `rxen_active_high` and `txen_active_high` default to `true`, preserving
existing active-high boards. The web setup wizard and `setup-radio-config.sh`
copy the preset's polarity into the active configuration and reset it to the
default when selecting a board without polarity overrides.

## Existing installations

Installing this code does not rewrite an existing active configuration. Back up
`/etc/openhop_repeater/config.yaml`, then edit these two values inside its existing
`sx1262` section (do not replace the entire file or create a second section):

```yaml
sx1262:
  rxen_pin: 23
  rxen_active_high: false
```

Keep the other GPIO, radio, identity, and network values as they are. For a
multi-radio configuration, edit the Station G3 entry's own `sx1262` section.
Restart `openhop-repeater` after changing hardware settings. The normal setup
wizard is intended for initial setup; it is not necessary to rerun it for this change.

After deployment, `pinctrl get 23` should report an output LOW during idle/RX.
Use a logic analyzer to verify HIGH before/during TX and LOW after TX, including
a failed transmission. Check that reception resumes. GPIO transitions in the
automated tests use simulated hardware; physical Station G3 testing is still required.

## TX power updates from the web app

The pinned core's `configure_radio()` changes frequency and modulation, but
does not set TX power. The settings manager now also calls `set_tx_power()`;
previously a request could be saved and reported as live-applied while the
radio retained its old power. A failed hardware update reports that the saved
settings require a restart. The settings API also accepts zero/negative power
values consistently with configuration validation. Board-specific output and
PA limits still apply.

The GPIO compatibility layer is in `repeater/sx1262_radio.py` and uses the
existing pinned `openhop_core==1.1.3`. No development-core upgrade or edits to
installed packages are required. Remove this compatibility layer when the
pinned core provides equivalent native polarity and recovery support.
