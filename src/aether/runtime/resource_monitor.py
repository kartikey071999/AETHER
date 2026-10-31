"""Read-only measurements of host resources used by the scheduler."""

from dataclasses import dataclass, field
from pathlib import Path

import psutil


def _logical_cpus() -> int:
    return psutil.cpu_count(logical=True) or 1


@dataclass(frozen=True, slots=True)
class HostResources:
    """`cpu_percent` is whole-host utilisation (0-100); `cpu_count` converts it to cores."""

    cpu_percent: float
    available_ram_bytes: int
    free_disk_bytes: int
    cpu_count: int = field(default_factory=_logical_cpus)

    @property
    def available_cpu_percent(self) -> float:
        """Idle capacity in single-core percent (one fully idle core is 100)."""
        cores = max(self.cpu_count, 1)
        return max(0.0, cores * 100.0 - self.cpu_percent * cores)


class ResourceMonitor:
    def snapshot(self, disk_path: Path) -> HostResources:
        return HostResources(
            cpu_percent=psutil.cpu_percent(interval=None),
            cpu_count=_logical_cpus(),
            available_ram_bytes=psutil.virtual_memory().available,
            # psutil on Windows only accepts str paths.
            free_disk_bytes=psutil.disk_usage(str(disk_path)).free,
        )
