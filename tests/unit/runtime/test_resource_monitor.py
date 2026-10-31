"""ResourceMonitor reads the real host; it must accept the Path settings hand it."""

from pathlib import Path

from aether.runtime.resource_monitor import ResourceMonitor


def test_snapshot_accepts_a_path_object(tmp_path: Path) -> None:
    # Regression: psutil.disk_usage rejects pathlib.Path on Windows, which made every
    # ORCHESTR release tick fail with "resource snapshot failed".
    snapshot = ResourceMonitor().snapshot(tmp_path)

    assert snapshot.free_disk_bytes > 0
    assert snapshot.available_ram_bytes > 0
    assert snapshot.cpu_percent >= 0
