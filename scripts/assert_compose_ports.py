#!/usr/bin/env python3
"""Fail closed when development demo auth is bound beyond loopback."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def proxy_bindings(config: dict[str, Any]) -> dict[int, str]:
    ports = config.get("services", {}).get("proxy", {}).get("ports", [])
    bindings: dict[int, str] = {}
    for port in ports:
        target = int(port["target"])
        if target in {80, 443}:
            bindings[target] = str(port.get("host_ip") or "")
    if set(bindings) != {80, 443}:
        raise SystemExit("proxy must publish exactly the HTTP and HTTPS entrypoints")
    return bindings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("development", "production"))
    args = parser.parse_args()
    bindings = proxy_bindings(json.load(sys.stdin))

    if args.mode == "development":
        unexpected = {
            port: host for port, host in bindings.items() if host != "127.0.0.1"
        }
        if unexpected:
            raise SystemExit(
                f"development demo ports must bind 127.0.0.1, got {unexpected}"
            )
    else:
        unexpected = {
            port: host
            for port, host in bindings.items()
            if host not in {"", "0.0.0.0"}
        }
        if unexpected:
            raise SystemExit(
                f"production ports must bind the public interface, got {unexpected}"
            )

    print(f"{args.mode} proxy bindings verified: {bindings}")


if __name__ == "__main__":
    main()
