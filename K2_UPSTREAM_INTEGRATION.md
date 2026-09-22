# K2 upstream integration — testing branch

This branch merges official Cartographer3D/cartographer3d-plugin main at
`8c3b0cccbd7699a1002f3b36c6f410ed25461445` into the K2 fork based on
`3b895c7994a71097deefb545a4e473d5c99486c3`.
It includes all 41 upstream-only commits, not just selected mesh fixes.

## Compatibility work

- After reconnect model validation/loading completes with a known MCU version,
  the K2 adapter removes only the exact stale disconnected-startup runtime
  warning and refreshes Klipper's warning status for Fluidd. Failed or skipped
  validation does not clear it; model compatibility and other warnings remain.
  Regression validation: 484 tests with Python 3.12/SciPy, 478 with Python 3.8
  without SciPy (six SciPy-required tests deselected). Hardware UI verification
  of automatic warning removal is still required.

- K2 transport is isolated under `adapters/k2`. Runtime detection recognizes
  the patched K2 MCU reconnect API; Kalico detection still takes precedence.
- Preserves K2's 8x Cartographer trsync timeouts, restoring the host globals even
  when starting the dispatch raises an exception.
- Reconnect refreshes command queues and dispatch FFI bindings before model
  callbacks. A failed finalization leaves probing disabled.
- Disconnected Z homing is rejected before arming dispatch. Endstop queries
  remain fail-closed, and homing cleanup still disarms after disconnection.
- Informational `QUERY_PROBE` preserves the original K2 disconnected "open"
  response without sampling. This is not an indication that homing is safe:
  the separate homing endstop still reports triggered. Every new sample session
  rejects a disconnected MCU, even while an aborted session awaits cleanup.
- Retains legacy probe entry points and vectorized temperature/distance/grid
  processing, adapted to upstream's expanded mesh-edge sample tolerance.
- Retains touch threshold/speed overrides and retract-distance phantom rejection.
  Rejected triggers now consume the overall touch budget, preventing an
  unbounded retry loop. Upstream sampling profiles and movement-floor checks
  remain in effect.
- The checked-in version fallback supports source/symlink installs without pip.
  Its `k2.upstream.8c3b0cc` suffix identifies this testing snapshot; the
  `1.5.0` base is the existing fork's compatibility version, not an assertion
  that this is an official upstream release.

## Automated validation

### Active-operation disconnect handling

The September 22, 2026 `SAFE_MOVE_Z` wrench test produced a valid scan trigger
and stopped both K2 Z steppers, but exposed a V4 6.0.0 cleanup-order problem.
The firmware stopped servicing trigger-sync traffic after
`cartographer_stop_home`; the host then timed out while trying to finalize the
already-stopped multi-MCU move and correctly shut Klipper down. On K2 only,
trigger dispatch is now finalized while Cartographer is still responsive and
the firmware homing state is disarmed afterward. A real disconnect during
dispatch cleanup still follows the shutdown-and-rehome path.

The V3 bounded Z150-to-Z50 touch diagnostic stopped promptly by operator
observation on September 20, 2026. Its log showed homing had reached dispatch
cleanup before the diagnostic's deliberate shutdown; querying the disconnected
probe then prevented the old loop from cleaning up later MCU participants.
This was not a measurement of stop latency or nozzle-contact safety.

K2 dispatch cleanup now attempts every participant, skips serial queries to an
already-disconnected MCU, and finalizes its local stepper state. Cleanup errors
and communication timeouts shut the host down and propagate as failures:
recovery requires the K2 protected restart procedure and rehoming, not another
touch attempt with potentially stale coordinates. The host MCU patch must also
be refreshed to skip shutdown commands to the disconnected non-critical MCU.
This does not change trigger-sync timeout values or normal mid-print reconnect.

Scan meshing now checks the session's latched abort before and after enqueueing
each path point and after each run's motion drain. It stops issuing further
points once the disconnect is observed; queued moves can still finish. This
is not an emergency stop, does not impose a hardware stop-time bound, and does
not revive a failed session when the probe reconnects. Failed scans do not
proceed to mesh processing/application.

These fixes require protected host reload and a repeat of the bounded hardware
test before hardware sign-off. Do not test against the nozzle/bed.

Automatic removal of the disconnected startup warning uses the runtime-warning
API supplied by the K2 `save-config-restart` `configfile.py` patch (or the
equivalent Jacob host patch). This cosmetic cleanup is best-effort: hosts
without that API retain the warning, and cleanup exceptions are logged without
failing reconnect. Actual model-validation failures still propagate to the
reconnect safety handler and can trigger shutdown.

Using upstream's unchanged `uv.lock`:

- Python 3.12 with SciPy: **479 tests passed**.
- Python 3.8 without SciPy: **473 passed**; six tests that explicitly require
  installed SciPy were deselected. Missing-SciPy error handling remains tested.
- Ruff lint and formatting passed.
- Basedpyright: no errors (warnings remain).

The additional tests cover K2 detection, timeout restoration, reconnect ordering
and failure blocking, disconnected homing, cleanup, edge sample retention,
empty sample batches, touch overrides, bounded phantom rejection, disconnected
informational queries, session-entry rejection, and session recovery after reconnect.

For a standard checkout, use `uv sync --locked --all-extras --all-groups`,
`uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, and
`uv run basedpyright`.

## Printer validation still required

This is not yet a hardware-validated replacement for the installed fork.
No printer configuration or firmware was changed by this integration.

1. Back up the working plugin revision and printer configuration.
2. Test startup and idle disconnect/reconnect with the existing firmware first.
   Confirm logs select the K2 environment and models load.
3. Verify guarded homing and normal homing under supervision. Never use a hand
   or another body part to test collision protection.
4. Send a print with deliberately unhomed axes. Confirm no guarded bed-upward
   move uses an untrusted Z coordinate, normal homing runs first, and a
   disconnected Cartographer blocks the final Z-homing stage.
5. Retest a normal `SAFE_MOVE_Z`, then one controlled unexpected scan trigger.
   The trigger must latch the stop, abort preparation without resuming when the
   object is removed, and leave Klipper running. A genuine active disconnect
   must still require protected restart and rehoming.
6. Check scan/touch calibration, repeated single-run meshes at unchanged
   temperature and scan settings, start-print, and cancellation.
7. Keep firmware updates separate so any behavior change can be attributed
   to the plugin or firmware independently.

Do not promote this branch to main or change the normal installer target until
the printer checks pass. Restore the previous plugin revision and backed-up
configuration if testing exposes a regression.
