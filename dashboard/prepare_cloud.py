"""Convert a Polycam point cloud (.ply) into the compact file the dashboard loads.

    python dashboard/prepare_cloud.py dashboard/resources/5.11.2026.ply [voxel_m] [max_points]

Points are averaged inside voxels of voxel_m metres (default 0.03), which smooths
sensor noise and evens out the density. Voxels with a single point are dropped
as stray noise. Pass 0 to skip smoothing.

Writes data/cloud.bin (float32 xyz, then uint8 rgb) and data/cloud.json (metadata).
Needs numpy. Coordinates are kept exactly as exported, so boxes placed in the
dashboard line up with the original scan.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).parent / "data"

PLY_TYPES = {"double": "<f8", "float": "<f4", "uchar": "u1", "uint8": "u1", "int": "<i4", "uint": "<u4"}


def read_ply(path: Path):
    raw = path.read_bytes()
    marker = b"end_header\n"
    end = raw.index(marker) + len(marker)
    header = raw[:end].decode("ascii", "replace").splitlines()
    if "format binary_little_endian 1.0" not in header:
        raise SystemExit("Only binary_little_endian PLY is supported.")
    count = next(int(l.split()[2]) for l in header if l.startswith("element vertex"))
    fields = [(l.split()[2], PLY_TYPES[l.split()[1]]) for l in header if l.startswith("property")]
    return np.frombuffer(raw, dtype=np.dtype(fields), count=count, offset=end)


def voxel_average(cloud, voxel: float, min_points: int = 2):
    xyz = np.stack([cloud["x"], cloud["y"], cloud["z"]], axis=1)
    rgb = np.stack([cloud["red"], cloud["green"], cloud["blue"]], axis=1).astype("f8")
    cells = np.floor((xyz - xyz.min(axis=0)) / voxel).astype(np.int64)
    _, inverse, counts = np.unique(cells, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.ravel()
    keep = counts >= min_points
    out = np.zeros(len(counts), dtype=[("x", "f8"), ("y", "f8"), ("z", "f8"), ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    for i, name in enumerate("xyz"):
        out[name] = np.bincount(inverse, weights=xyz[:, i]) / counts
    for i, name in enumerate(("red", "green", "blue")):
        out[name] = np.clip(np.bincount(inverse, weights=rgb[:, i]) / counts, 0, 255)
    return out[keep]


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    voxel = float(sys.argv[2]) if len(sys.argv) > 2 else 0.03
    max_points = int(sys.argv[3]) if len(sys.argv) > 3 else 300_000
    cloud = read_ply(Path(sys.argv[1]))
    print(f"Read {len(cloud)} points")
    if voxel > 0:
        cloud = voxel_average(cloud, voxel)
        print(f"After {voxel * 100:.0f} cm voxel averaging: {len(cloud)} points")
    if len(cloud) > max_points:
        keep = np.sort(np.random.default_rng(0).choice(len(cloud), max_points, replace=False))
        cloud = cloud[keep]

    xyz = np.stack([cloud["x"], cloud["y"], cloud["z"]], axis=1).astype("<f4")
    rgb = np.stack([cloud["red"], cloud["green"], cloud["blue"]], axis=1).astype("u1")
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "cloud.bin").write_bytes(xyz.tobytes() + rgb.tobytes())
    meta = {
        "count": int(len(xyz)),
        "min": xyz.min(axis=0).round(3).tolist(),
        "max": xyz.max(axis=0).round(3).tolist(),
        "up_axis": "z",
        "source": Path(sys.argv[1]).name,
    }
    (DATA_DIR / "cloud.json").write_text(json.dumps(meta, indent=2))
    print(f"Wrote {meta['count']} points. Bounds {meta['min']} to {meta['max']}")


if __name__ == "__main__":
    main()
