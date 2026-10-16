"""Load ROS map YAMLs as compact, correctly positioned browser images."""
from __future__ import annotations

import base64
import io
import math
from pathlib import Path

import numpy as np
from PIL import Image
import yaml


def load_map(filename: str) -> dict:
    path = Path(filename).expanduser().resolve()
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict) or not config.get("image"):
        raise ValueError("Map YAML needs image, resolution, and origin fields")
    resolution = float(config.get("resolution", 0))
    origin = [float(v) for v in config.get("origin", [0, 0, 0])]
    if len(origin) != 3 or not all(math.isfinite(v) for v in origin) or not math.isfinite(resolution) or resolution <= 0:
        raise ValueError("Map origin must be finite [x, y, yaw] and resolution positive")
    image_path = Path(str(config["image"])).expanduser()
    if not image_path.is_absolute():
        image_path = path.parent / image_path
    with Image.open(image_path) as source:
        gray = np.asarray(source.convert("L"))
    height, width = gray.shape
    values = gray / 255.0
    occupancy = values if int(config.get("negate", 0)) else 1.0 - values
    occupied = occupancy > float(config.get("occupied_thresh", .65))
    free = occupancy < float(config.get("free_thresh", .196))
    # Crop the unknown padding common in SLAM maps without moving the track.
    rows, cols = np.nonzero(occupied)
    left, top, right, bottom = 0, 0, width, height
    if len(rows):
        margin = max(10, int(1.0 / resolution))
        left, right = max(0, int(cols.min()) - margin), min(width, int(cols.max()) + margin + 1)
        top, bottom = max(0, int(rows.min()) - margin), min(height, int(rows.max()) + margin + 1)
    rgb = np.empty((*gray.shape, 3), dtype=np.uint8)
    rgb[:] = [22, 29, 38]
    rgb[free] = [38, 48, 60]
    rgb[occupied] = [115, 132, 149]
    image = Image.fromarray(rgb[top:bottom, left:right])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    dx, dy = left * resolution, (height - bottom) * resolution
    c, s = math.cos(origin[2]), math.sin(origin[2])
    return {
        "image": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
        "origin": [origin[0] + c * dx - s * dy, origin[1] + s * dx + c * dy, origin[2]],
        "resolution": resolution, "width": right - left, "height": bottom - top,
        "source": str(path), "frame": "map",
    }
