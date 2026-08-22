"""Shared test setup.

Every app module here is a flat top-level file (no package), and
rest_service.py builds its module-level `service = Service()` (which
constructs an XRayCore, which shells out to `<XRAY_EXECUTABLE_PATH>
version`) the moment it's imported. Without a real Xray binary available
in the test environment, that import would crash before any test runs.

`/usr/bin/true` is a safe stand-in: called with a "version" argument it
still exits 0 with empty output, so get_xray_version()'s regex simply
finds no match and returns None instead of raising. This mirrors the same
trick the Marzban panel's own test suite uses for the same reason.

This file must set the environment variable and put the repo root on
sys.path *before* any test module imports app code — conftest.py is
collected first by pytest, which is what makes that ordering guarantee
hold.
"""
import os
import sys

os.environ.setdefault("XRAY_EXECUTABLE_PATH", "/usr/bin/true")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
