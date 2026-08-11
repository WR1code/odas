"""Convert the integration scene-state JSON to AcoustiX/Sionna Mitsuba XML."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET

import numpy as np


FACES = np.asarray([
    [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
    [0, 1, 5], [0, 5, 4], [3, 7, 6], [3, 6, 2],
    [0, 4, 7], [0, 7, 3], [1, 2, 6], [1, 6, 5],
], dtype=int)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def box_vertices(center, size) -> np.ndarray:
    center = np.asarray(center, dtype=float)
    half = np.asarray(size, dtype=float) / 2.0
    return center + np.asarray([
        [-half[0], -half[1], -half[2]], [half[0], -half[1], -half[2]],
        [half[0], half[1], -half[2]], [-half[0], half[1], -half[2]],
        [-half[0], -half[1], half[2]], [half[0], -half[1], half[2]],
        [half[0], half[1], half[2]], [-half[0], half[1], half[2]],
    ])


def write_ascii_ply(path: Path, vertices: np.ndarray) -> None:
    lines = [
        "ply", "format ascii 1.0", f"element vertex {len(vertices)}",
        "property float x", "property float y", "property float z",
        f"element face {len(FACES)}", "property list uchar int vertex_indices", "end_header",
    ]
    lines.extend("%.9g %.9g %.9g" % tuple(v) for v in vertices)
    lines.extend("3 %d %d %d" % tuple(face) for face in FACES)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def convert_scene(scene_path: Path, materials_path: Path, output_dir: Path) -> Path:
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    materials = json.loads(materials_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    root = ET.Element("scene", version="2.1.0")
    integrator = ET.SubElement(root, "integrator", type="path")
    ET.SubElement(integrator, "integer", name="max_depth", value="12")
    used_materials = sorted({obj["material"] for obj in scene["objects"]})
    for material_name in used_materials:
        if material_name not in materials["materials"]:
            raise KeyError(f"no acoustic material mapping for {material_name!r}")
        material_id = f"mat-{safe_name(material_name)}"
        bsdf = ET.SubElement(root, "bsdf", type="twosided", id=material_id, name=material_id)
        principled = ET.SubElement(bsdf, "bsdf", type="principled", name="bsdf")
        ET.SubElement(principled, "rgb", name="base_color", value="0.8 0.8 0.8")
        ET.SubElement(principled, "float", name="roughness", value="0.25")
    for obj in scene["objects"]:
        name = safe_name(obj["name"])
        ply_name = f"{name}.ply"
        write_ascii_ply(output_dir / ply_name, box_vertices(obj["center_m"], obj["size_m"]))
        shape = ET.SubElement(root, "shape", type="ply", id=name)
        ET.SubElement(shape, "string", name="filename", value=ply_name)
        ET.SubElement(shape, "boolean", name="face_normals", value="true")
        ET.SubElement(shape, "ref", id=f"mat-{safe_name(obj['material'])}", name="bsdf")
    xml_path = output_dir / "scene.xml"
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(xml_path, encoding="utf-8", xml_declaration=True)
    metadata = {
        "source_scene": str(scene_path.resolve()),
        "coordinate_transform": "identity: RHS, metres, +X forward, +Y left, +Z up",
        "objects": scene["objects"],
    }
    (output_dir / "conversion_manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return xml_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scene", type=Path)
    parser.add_argument("materials", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(convert_scene(args.scene, args.materials, args.output))


if __name__ == "__main__":
    main()
