# K2 upstream integration — testing branch

This branch merges official Cartographer3D/cartographer3d-plugin main at
`8c3b0cccbd7699a1002f3b36c6f410ed25461445` into the K2 fork based on
`3b895c7994a71097deefb545a4e473d5c99486c3`.
It includes all 41 upstream-only commits, not just selected mesh fixes.

## Compatibility work

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
4. Check scan/touch calibration, repeated single-run meshes at unchanged
   temperature and scan settings, start-print, and cancellation.
5. Keep firmware updates separate so any behavior change can be attributed
   to the plugin or firmware independently.

Do not promote this branch to main or change the normal installer target until
the printer checks pass. Restore the previous plugin revision and backed-up
configuration if testing exposes a regression.
