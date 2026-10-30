"""Read-only measurements of host resources used by the scheduler."""

from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True, slots=True)
class HostResources:
    cpu_percent: float
    available_ram_bytes: int
    free_disk_bytes: int


class ResourceMonitor:
    def snapshot(self, disk_path: Path) -> HostResources:
        return HostResources(
            cpu_percent=psutil.cpu_percent(interval=None),
            available_ram_bytes=psutil.virtual_memory().available,
            free_disk_bytes=psutil.disk_usage(disk_path).free,
        )
