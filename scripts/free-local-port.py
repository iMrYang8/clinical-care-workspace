#!/usr/bin/env python3
"""Print an available loopback TCP port for an isolated short-lived gate."""

from __future__ import annotations

import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
