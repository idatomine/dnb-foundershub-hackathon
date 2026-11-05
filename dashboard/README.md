# Room materials dashboard

Click an object in the scanned room and attach the physical data the scan can't give you: material, fabric, friction coefficient, density, thickness and absorption α per octave band.

```bash
python dashboard/server.py      # http://localhost:8765
```

Uses only the Python standard library, so there's nothing to install.

## Using your Polycam scan

```bash
pip install numpy                                              # only for this one-off step
python dashboard/prepare_cloud.py dashboard/resources/5.11.2026.ply
```

This writes `data/cloud.bin`, a downsampled copy the browser can load. Then in the dashboard click **+ Place object**, click the object in the 3D view, and adjust the box size, position and label in the side panel. Boxes you place are stored in `data/user_surfaces.json`.

## Files

| File | What it is |
|---|---|
| `data/room_scan.json` | Objects from an automatic source (empty for now). Hand-placed boxes go in `user_surfaces.json`. |
| `data/user_surfaces.json` | Boxes you placed in the 3D view. |
| `data/cloud.bin` | Downsampled point cloud, made by `prepare_cloud.py`. |
| `data/enriched_room.json` | What you enter in the dashboard, keyed by object id. Created on first save. |
| `data/materials.json` | Presets with typical literature values. They are starting guesses, not measurements. |

The scan file is never written to, so you can swap in a new scan and keep your enrichment as long as the ids stay the same.

## Scan format (`room_scan.json`)

Whoever converts the Polycam export should produce this:

```json
{
  "room": { "name": "...", "source": "polycam", "scanned_at": "ISO-8601" },
  "surfaces": [
    {
      "id": "curtain_1",                          // stable, [A-Za-z0-9_-]
      "label": "Curtain",
      "category": "wall | floor | window | door | furniture",
      "cv_guess": { "material": "fabric", "confidence": 0.5 },
      "geometry": {
        "type": "bbox",
        "center_m": [x, y, z],                    // x, y = floor plane; z = height
        "size_m": [w, d, h],
        "rotation_deg": 0,
        "wall": "wall_north"                      // optional: mounted on a wall
      }
    },
    {
      "id": "wall_north", "category": "wall",
      "geometry": { "type": "polygon", "points_m": [[0,0],[5,0]], "height_m": 2.6 }
    }
  ]
}
```

## API

- `GET /api/room`: the scan with the enrichment merged in
- `POST /api/surfaces/<id>`: partial update of one object's enrichment
- `GET /api/export/pyroomacoustics`: α per octave band (125 Hz to 4 kHz) for every object that has values

Each object's `source` field records where its numbers came from: `unmeasured`, `estimated` (literature preset), `manual` or `rig_measured`. Only `rig_measured` is a measurement. Bands above 4 kHz are left out because the rig can't reach them.
