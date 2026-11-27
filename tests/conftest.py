from __future__ import annotations

import os

# Prevent tests from spawning real external worker CLIs (e.g. codebuddy) unless explicitly overridden.
os.environ.setdefault("ADK_WORKER_PROVIDER", "mock")
