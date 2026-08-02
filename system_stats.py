"""System telemetry for the panel: CPU, memory, uptime and network rates.

Everything comes from the standard library and /proc — no psutil,
consistent with the rest of this minimal-footprint project. Reads that
fail (exotic kernel, missing /proc entries) degrade to None fields rather
than raising, so the endpoint never turns a metrics hiccup into an error.
"""
import os
import socket
import time

from logger import logger


def _read_meminfo() -> dict:
    """Parse /proc/meminfo ("Key:  N kB") into bytes."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, value = line.partition(":")
            parts = value.split()
            if not parts:
                continue
            amount = int(parts[0])
            if len(parts) > 1 and parts[1].lower() == "kb":
                amount *= 1024
            info[key.strip()] = amount
    return info


def _read_uptime() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def _read_net_dev() -> dict:
    """{interface: (rx_bytes, tx_bytes)} from /proc/net/dev, skipping lo."""
    interfaces = {}
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:  # first two lines are headers
            name, _, data = line.partition(":")
            name = name.strip()
            if not data or name == "lo":
                continue
            fields = data.split()
            interfaces[name] = (int(fields[0]), int(fields[8]))
    return interfaces


def get_system_stats(net_sample_interval: float = 0.5) -> dict:
    stats = {
        "hostname": socket.gethostname(),
        "cpu_count": os.cpu_count(),
        "loadavg": None,
        "mem_total": None,
        "mem_free": None,
        "mem_available": None,
        "mem_used": None,
        "uptime_seconds": None,
        "network": None,
    }

    try:
        stats["loadavg"] = list(os.getloadavg())
    except OSError:
        pass

    try:
        meminfo = _read_meminfo()
        total = meminfo.get("MemTotal")
        available = meminfo.get("MemAvailable")
        stats["mem_total"] = total
        stats["mem_free"] = meminfo.get("MemFree")
        stats["mem_available"] = available
        if total is not None and available is not None:
            stats["mem_used"] = total - available
    except (OSError, ValueError) as exc:
        logger.debug(f"meminfo read failed: {exc}")

    try:
        stats["uptime_seconds"] = _read_uptime()
    except (OSError, ValueError) as exc:
        logger.debug(f"uptime read failed: {exc}")

    try:
        # Two samples a moment apart give current byte rates without
        # needing a background sampler thread.
        first = _read_net_dev()
        started_at = time.monotonic()
        time.sleep(net_sample_interval)
        second = _read_net_dev()
        elapsed = time.monotonic() - started_at

        network = {}
        for name, (rx, tx) in second.items():
            prev_rx, prev_tx = first.get(name, (rx, tx))
            network[name] = {
                "rx_bytes": rx,
                "tx_bytes": tx,
                "rx_bytes_per_sec": max(rx - prev_rx, 0) / elapsed,
                "tx_bytes_per_sec": max(tx - prev_tx, 0) / elapsed,
            }
        stats["network"] = network
    except (OSError, ValueError, IndexError) as exc:
        logger.debug(f"net dev read failed: {exc}")

    return stats
