from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


FBX_HEADER = """; FBX 7.4.0 project file
; ----------------------------------------------------

FBXHeaderExtension:  {
    FBXHeaderVersion: 1003
    FBXVersion: 7400
    Creator: "RenderdocDiffPortable"
}
GlobalSettings:  {
    Version: 1000
    Properties70:  {
        P: "UpAxis", "int", "Integer", "",1
        P: "UpAxisSign", "int", "Integer", "",1
        P: "FrontAxis", "int", "Integer", "",2
        P: "FrontAxisSign", "int", "Integer", "",1
        P: "CoordAxis", "int", "Integer", "",0
        P: "CoordAxisSign", "int", "Integer", "",1
        P: "UnitScaleFactor", "double", "Number", "",1
        P: "OriginalUnitScaleFactor", "double", "Number", "",1
    }
}
Documents:  {
    Count: 1
    Document: 1669162400, "Scene", "Scene" {
        RootNode: 0
    }
}
References:  {
}
Definitions:  {
    Version: 100
    Count: 4
    ObjectType: "GlobalSettings" {
        Count: 1
    }
    ObjectType: "Model" {
        Count: 1
    }
    ObjectType: "Geometry" {
        Count: 1
    }
    ObjectType: "Material" {
        Count: 1
    }
}
"""

MATERIAL_HASH = "1737697776"
MATERIAL_ELEMENT = """    Material: 1737697776, "Material::Default_Material", "" {
        Version: 102
        ShadingModel: "lambert"
        MultiLayer: 0
        Properties70:  {
            P: "AmbientColor", "Color", "", "A",0,0,0
            P: "DiffuseColor", "Color", "", "A",1,1,1
            P: "Opacity", "double", "Number", "",1
        }
    }"""

MATERIAL_LAYER = """        LayerElementMaterial: 0 {
            Version: 101
            Name: "Material"
            MappingInformationType: "AllSame"
            ReferenceInformationType: "IndexToDirect"
            Materials: *1 {
                a: 0
            }
        }"""

DEFAULT_HINTS = {
    "position": ["in_position0.x", "position0.x", "position.x", "position"],
    "normal": ["in_normal0.x", "normal0.x", "normal.x", "normal"],
    "uv0": ["in_texcoord0.x", "texcoord0.x", "uv0.x", "uv0"],
    "uv1": ["in_texcoord1.x", "texcoord1.x", "uv1.x", "uv1"],
    "uv2": ["in_texcoord2.x", "texcoord2.x", "uv2.x", "uv2"],
    "uv3": ["in_texcoord3.x", "texcoord3.x", "uv3.x", "uv3"],
    "color": ["in_color0.x", "color0.x", "color.x", "color", "diffuse.x"],
    "tangent": ["in_tangent0.x", "tangent0.x", "tangent.x", "tangent"],
}


@dataclass
class ColumnMapping:
    position: str
    normal: str = ""
    uv0: str = ""
    uv1: str = ""
    uv2: str = ""
    uv3: str = ""
    color: str = ""
    tangent: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "position": self.position,
            "normal": self.normal,
            "uv0": self.uv0,
            "uv1": self.uv1,
            "uv2": self.uv2,
            "uv3": self.uv3,
            "color": self.color,
            "tangent": self.tangent,
        }


@dataclass
class MeshVertex:
    pos: Tuple[float, float, float]
    pos_w: float
    normal: Tuple[float, float, float]
    uv0: Tuple[float, float]
    uv1: Tuple[float, float]
    uv2: Tuple[float, float]
    uv3: Tuple[float, float]
    color: Tuple[float, float, float, float]
    tangent: Tuple[float, float, float, float]


@dataclass
class ConvertedMesh:
    name: str
    vertices: List[MeshVertex]
    polygon_vertex_ids: List[int]


class CsvModelConverter:
    def read_headers(self, csv_path: str | Path) -> List[str]:
        rows = self._read_rows(csv_path)
        if not rows:
            raise ValueError("CSV is empty")
        return rows[0]

    def suggest_mapping(self, csv_path: str | Path) -> ColumnMapping:
        rows = self._read_rows(csv_path)
        if not rows:
            raise ValueError("CSV is empty")
        headers = rows[0]
        mapping = self.auto_detect_mapping(headers)
        groups = self._group_attribute_headers(headers)
        stats = self._analyze_attribute_groups(rows[1:513], groups)
        used_headers = {value for value in mapping.to_dict().values() if value}

        if not mapping.position:
            candidate = self._pick_best_candidate(
                stats,
                used_headers,
                lambda item: item["comp_count"] >= 3
                and not item["normalized_01"]
                and not item["signed_unit_range"]
                and not item["integerish"],
                lambda item: (item["max_abs"], item["span_sum"], -item["header_index"]),
            )
            if candidate:
                mapping.position = candidate["header"]
                used_headers.add(mapping.position)

        if not mapping.tangent:
            candidate = self._pick_best_candidate(
                stats,
                used_headers,
                lambda item: item["comp_count"] >= 4
                and item["signed_unit_range"]
                and not item["integerish"],
                lambda item: (item["w_sign_ratio"], -item["unit_length_error"], item["span_sum"]),
            )
            if candidate:
                mapping.tangent = candidate["header"]
                used_headers.add(mapping.tangent)

        if not mapping.normal:
            candidate = self._pick_best_candidate(
                stats,
                used_headers,
                lambda item: item["comp_count"] >= 3
                and item["signed_unit_range"]
                and not item["integerish"],
                lambda item: (-item["unit_length_error"], item["span_sum"], -item["header_index"]),
            )
            if candidate:
                mapping.normal = candidate["header"]
                used_headers.add(mapping.normal)

        if not mapping.color:
            candidate = self._pick_best_candidate(
                stats,
                used_headers,
                lambda item: item["comp_count"] >= 3
                and item["normalized_01"]
                and item["span_sum"] > 0.05
                and not item["integerish"],
                lambda item: (item["span_sum"], -item["unit_length_error"], -item["header_index"]),
            )
            if candidate:
                mapping.color = candidate["header"]
                used_headers.add(mapping.color)

        uv_candidates = self._pick_sorted_candidates(
            stats,
            used_headers,
            lambda item: item["comp_count"] == 2
            and not item["integerish"]
            and item["span_sum"] > 0.0
            and item["max_abs"] <= 32.0,
            lambda item: (item["comp_count"] == 2, item["span_sum"], -item["header_index"]),
        )
        uv_fields = ["uv0", "uv1", "uv2", "uv3"]
        for field_name, candidate in zip(
            [field for field in uv_fields if not getattr(mapping, field)],
            uv_candidates,
        ):
            setattr(mapping, field_name, candidate["header"])
            used_headers.add(candidate["header"])

        return mapping

    def auto_detect_mapping(self, headers: Sequence[str]) -> ColumnMapping:
        return ColumnMapping(
            position=self._find_header(headers, DEFAULT_HINTS["position"]) or "",
            normal=self._find_header(headers, DEFAULT_HINTS["normal"]) or "",
            uv0=self._find_header(headers, DEFAULT_HINTS["uv0"]) or "",
            uv1=self._find_header(headers, DEFAULT_HINTS["uv1"]) or "",
            uv2=self._find_header(headers, DEFAULT_HINTS["uv2"]) or "",
            uv3=self._find_header(headers, DEFAULT_HINTS["uv3"]) or "",
            color=self._find_header(headers, DEFAULT_HINTS["color"]) or "",
            tangent=self._find_header(headers, DEFAULT_HINTS["tangent"]) or "",
        )

    def convert(self, csv_path: str | Path, output_path: str | Path, mapping: ColumnMapping, fmt: str) -> ConvertedMesh:
        mesh = self.build_mesh(csv_path, mapping)
        fmt_normalized = fmt.lower()
        if fmt_normalized == "obj":
            self.write_obj(mesh, output_path)
        elif fmt_normalized == "fbx":
            self.write_fbx(mesh, output_path)
        else:
            raise ValueError(f"unsupported format: {fmt}")
        return mesh

    def build_mesh(self, csv_path: str | Path, mapping: ColumnMapping) -> ConvertedMesh:
        rows = self._read_rows(csv_path)
        if len(rows) < 4:
            raise ValueError("CSV does not contain enough vertex rows")

        headers = rows[0]
        pos_idx = self._require_column(headers, mapping.position, "position")
        normal_idx = self._optional_column(headers, mapping.normal)
        uv0_idx = self._optional_column(headers, mapping.uv0)
        uv1_idx = self._optional_column(headers, mapping.uv1)
        uv2_idx = self._optional_column(headers, mapping.uv2)
        uv3_idx = self._optional_column(headers, mapping.uv3)
        color_idx = self._optional_column(headers, mapping.color)
        tangent_idx = self._optional_column(headers, mapping.tangent)
        vertex_id_idx = 1 if len(headers) > 1 else 0

        vertex_map: Dict[int, int] = {}
        vertices: List[MeshVertex] = []
        polygon_vertex_ids: List[int] = []
        winding = (0, 2, 1)

        for triangle_start in range(1, len(rows) - 2, 3):
            tri_rows = rows[triangle_start : triangle_start + 3]
            if len(tri_rows) < 3:
                break
            for row_idx in winding:
                row = tri_rows[row_idx]
                vertex_id = self._parse_int(row[vertex_id_idx], triangle_start + row_idx)
                polygon_vertex_ids.append(vertex_id)
                if vertex_id in vertex_map:
                    continue
                vertex_map[vertex_id] = len(vertices)
                vertices.append(
                    MeshVertex(
                        pos=(
                            -self._read_component(row, pos_idx, 0),
                            self._read_component(row, pos_idx, 1),
                            self._read_component(row, pos_idx, 2),
                        ),
                        pos_w=self._read_component(row, pos_idx, 3, default=1.0),
                        normal=(
                            -self._read_component(row, normal_idx, 0),
                            self._read_component(row, normal_idx, 1),
                            self._read_component(row, normal_idx, 2),
                        )
                        if normal_idx is not None
                        else (0.0, 0.0, 0.0),
                        uv0=self._read_vec2(row, uv0_idx),
                        uv1=self._read_vec2(row, uv1_idx),
                        uv2=self._read_vec2(row, uv2_idx),
                        uv3=self._read_vec2(row, uv3_idx),
                        color=self._read_vec4(row, color_idx, default=(1.0, 1.0, 1.0, 1.0)),
                        tangent=(
                            -self._read_component(row, tangent_idx, 0),
                            self._read_component(row, tangent_idx, 1),
                            self._read_component(row, tangent_idx, 2),
                            self._read_component(row, tangent_idx, 3, default=1.0),
                        )
                        if tangent_idx is not None
                        else (0.0, 0.0, 0.0, 1.0),
                    )
                )

        if not vertices or not polygon_vertex_ids:
            raise ValueError("no mesh data was parsed from CSV")

        return ConvertedMesh(
            name=Path(csv_path).stem,
            vertices=vertices,
            polygon_vertex_ids=polygon_vertex_ids,
        )

    def build_mesh_from_obj(self, obj_path: str | Path) -> ConvertedMesh:
        positions: List[Tuple[float, float, float]] = []
        uvs: List[Tuple[float, float]] = []
        normals: List[Tuple[float, float, float]] = []
        vertices: List[MeshVertex] = []
        polygon_vertex_ids: List[int] = []
        vertex_lookup: Dict[Tuple[int, int, int], int] = {}
        mesh_name = Path(obj_path).stem

        for raw_line in Path(obj_path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("o "):
                mesh_name = line[2:].strip() or mesh_name
            elif line.startswith("v "):
                parts = line.split()
                positions.append(
                    (
                        self._parse_float(parts[1], 0.0),
                        self._parse_float(parts[2], 0.0),
                        self._parse_float(parts[3], 0.0),
                    )
                )
            elif line.startswith("vt "):
                parts = line.split()
                uvs.append(
                    (
                        self._parse_float(parts[1], 0.0),
                        self._parse_float(parts[2], 0.0),
                    )
                )
            elif line.startswith("vn "):
                parts = line.split()
                normals.append(
                    (
                        self._parse_float(parts[1], 0.0),
                        self._parse_float(parts[2], 0.0),
                        self._parse_float(parts[3], 0.0),
                    )
                )
            elif line.startswith("f "):
                refs = line[2:].split()
                face_indices = [self._parse_obj_face_ref(ref) for ref in refs]
                if len(face_indices) < 3:
                    continue
                for tri in range(1, len(face_indices) - 1):
                    for corner in (0, tri, tri + 1):
                        pos_idx, uv_idx, normal_idx = face_indices[corner]
                        key = (pos_idx, uv_idx, normal_idx)
                        vertex_index = vertex_lookup.get(key)
                        if vertex_index is None:
                            position = positions[pos_idx] if 0 <= pos_idx < len(positions) else (0.0, 0.0, 0.0)
                            uv = uvs[uv_idx] if 0 <= uv_idx < len(uvs) else (0.0, 0.0)
                            normal = normals[normal_idx] if 0 <= normal_idx < len(normals) else (0.0, 0.0, 0.0)
                            vertex_index = len(vertices)
                            vertex_lookup[key] = vertex_index
                            vertices.append(
                                MeshVertex(
                                    pos=position,
                                    pos_w=1.0,
                                    normal=normal,
                                    uv0=uv,
                                    uv1=(0.0, 0.0),
                                    uv2=(0.0, 0.0),
                                    uv3=(0.0, 0.0),
                                    color=(1.0, 1.0, 1.0, 1.0),
                                    tangent=(0.0, 0.0, 0.0, 1.0),
                                )
                            )
                        polygon_vertex_ids.append(vertex_index)

        if not vertices or not polygon_vertex_ids:
            raise ValueError("OBJ does not contain any mesh faces")

        return ConvertedMesh(name=mesh_name, vertices=vertices, polygon_vertex_ids=polygon_vertex_ids)

    def write_csv(self, mesh: ConvertedMesh, output_path: str | Path) -> None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        headers = [
            "row_index",
            "vertex_id",
            "in_POSITION0.x",
            "in_POSITION0.y",
            "in_POSITION0.z",
            "in_POSITION0.w",
            "in_NORMAL0.x",
            "in_NORMAL0.y",
            "in_NORMAL0.z",
            "in_TEXCOORD0.x",
            "in_TEXCOORD0.y",
            "TEXCOORD1.x",
            "TEXCOORD1.y",
            "TEXCOORD2.x",
            "TEXCOORD2.y",
            "TEXCOORD3.x",
            "TEXCOORD3.y",
            "COLOR.x",
            "COLOR.y",
            "COLOR.z",
            "COLOR.w",
            "TANGENT.x",
            "TANGENT.y",
            "TANGENT.z",
            "TANGENT.w",
        ]
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for row_index, vertex_id in enumerate(mesh.polygon_vertex_ids):
                vertex = mesh.vertices[vertex_id]
                writer.writerow(
                    [
                        row_index,
                        vertex_id,
                        *vertex.pos,
                        vertex.pos_w,
                        *vertex.normal,
                        *vertex.uv0,
                        *vertex.uv1,
                        *vertex.uv2,
                        *vertex.uv3,
                        *vertex.color,
                        *vertex.tangent,
                    ]
                )

    def write_obj(self, mesh: ConvertedMesh, output_path: str | Path) -> None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = [f"o {mesh.name}"]
        for vertex in mesh.vertices:
            lines.append(f"v {self._fmt(vertex.pos[0])} {self._fmt(vertex.pos[1])} {self._fmt(vertex.pos[2])}")
        has_uv = any(vertex.uv0 != (0.0, 0.0) for vertex in mesh.vertices)
        if has_uv:
            for vertex in mesh.vertices:
                lines.append(f"vt {self._fmt(vertex.uv0[0])} {self._fmt(vertex.uv0[1])}")
        has_normal = any(vertex.normal != (0.0, 0.0, 0.0) for vertex in mesh.vertices)
        if has_normal:
            for vertex in mesh.vertices:
                lines.append(f"vn {self._fmt(vertex.normal[0])} {self._fmt(vertex.normal[1])} {self._fmt(vertex.normal[2])}")

        index_lookup = self._build_vertex_lookup(mesh)
        for i in range(0, len(mesh.polygon_vertex_ids), 3):
            a = index_lookup[mesh.polygon_vertex_ids[i]] + 1
            b = index_lookup[mesh.polygon_vertex_ids[i + 1]] + 1
            c = index_lookup[mesh.polygon_vertex_ids[i + 2]] + 1
            if has_uv and has_normal:
                lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")
            elif has_uv:
                lines.append(f"f {a}/{a} {b}/{b} {c}/{c}")
            elif has_normal:
                lines.append(f"f {a}//{a} {b}//{b} {c}//{c}")
            else:
                lines.append(f"f {a} {b} {c}")
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_fbx(self, mesh: ConvertedMesh, output_path: str | Path) -> None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        model_hash = str(abs(hash(mesh.name)))
        scene_hash = str(abs(hash(f"{mesh.name}_scene")))
        vertex_lookup = self._build_vertex_lookup(mesh)
        polygon_vertex_indices = [vertex_lookup[vertex_id] for vertex_id in mesh.polygon_vertex_ids]

        lines: List[str] = [FBX_HEADER, '; Object properties', ';------------------------------------------------------------------', "Objects:  {"]
        lines.extend(self._build_geometry_lines(mesh, scene_hash, polygon_vertex_indices))
        lines.extend(self._build_model_lines(model_hash, mesh.name))
        lines.append(MATERIAL_ELEMENT)
        lines.append("}")
        lines.extend(
            [
                "",
                '; Object connections',
                ';------------------------------------------------------------------',
                "Connections:  {",
                f'    C: "OO",{model_hash},0',
                f'    C: "OO",{MATERIAL_HASH},{model_hash}',
                f'    C: "OO",{scene_hash},{model_hash}',
                "}",
            ]
        )
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _build_geometry_lines(self, mesh: ConvertedMesh, scene_hash: str, polygon_indices: List[int]) -> List[str]:
        unmerged_count = len(polygon_indices)
        vertex_count = len(mesh.vertices)
        lines = [f'    Geometry: {scene_hash}, "Geometry::Scene", "Mesh" {{']
        lines.extend(self._build_vertices_section(mesh, polygon_indices))

        has_normal = any(vertex.normal != (0.0, 0.0, 0.0) for vertex in mesh.vertices)
        has_tangent = any(vertex.tangent[:3] != (0.0, 0.0, 0.0) for vertex in mesh.vertices)
        has_color = any(vertex.color != (1.0, 1.0, 1.0, 1.0) for vertex in mesh.vertices)
        uv_sets = self._collect_uv_sets(mesh.vertices)

        if has_normal:
            normal_values: List[float] = []
            normal_w = ["1"] * unmerged_count
            for poly_idx in polygon_indices:
                normal_values.extend(mesh.vertices[poly_idx].normal)
            lines.extend(
                [
                    "        LayerElementNormal: 0 {",
                    '            Name: "Normals"',
                    '            MappingInformationType: "ByPolygonVertex"',
                    '            ReferenceInformationType: "Direct"',
                    f"            Normals: *{len(normal_values)} {{",
                    f"                a: {self._join_numbers(normal_values)}",
                    "            }",
                    f"            NormalsW: *{unmerged_count} {{",
                    f'                a: {",".join(normal_w)}',
                    "            }",
                    "        }",
                ]
            )

        if has_tangent:
            tangent_values: List[float] = []
            tangent_w: List[str] = []
            for poly_idx in polygon_indices:
                tangent = mesh.vertices[poly_idx].tangent
                tangent_values.extend(tangent[:3])
                tangent_w.append(self._fmt(tangent[3]))
            lines.extend(
                [
                    "        LayerElementTangent: 0 {",
                    '            Name: "Tangents"',
                    '            MappingInformationType: "ByPolygonVertex"',
                    '            ReferenceInformationType: "Direct"',
                    f"            Tangents: *{len(tangent_values)} {{",
                    f"                a: {self._join_numbers(tangent_values)}",
                    "            }",
                    f"            TangentsW: *{unmerged_count} {{",
                    f'                a: {",".join(tangent_w)}',
                    "            }",
                    "        }",
                ]
            )

        if has_color:
            color_values: List[float] = []
            for poly_idx in polygon_indices:
                color_values.extend(mesh.vertices[poly_idx].color)
            lines.extend(
                [
                    "        LayerElementColor: 0 {",
                    '            Name: "VertexColors"',
                    '            MappingInformationType: "ByPolygonVertex"',
                    '            ReferenceInformationType: "IndexToDirect"',
                    f"            Colors: *{len(color_values)} {{",
                    f"                a: {self._join_numbers(color_values)}",
                    "            }",
                    f"            ColorIndex: *{unmerged_count} {{",
                    f'                a: {",".join(str(poly_idx) for poly_idx in polygon_indices)}',
                    "            }",
                    "        }",
                ]
            )

        for uv_set_index, uv_values in uv_sets:
            lines.extend(
                [
                    f"        LayerElementUV: {uv_set_index} {{",
                    '            MappingInformationType: "ByPolygonVertex"',
                    '            ReferenceInformationType: "IndexToDirect"',
                    f'            Name: "UVSet{uv_set_index}"',
                    f"            UV: *{len(uv_values)} {{",
                    f"                a: {self._join_numbers(uv_values)}",
                    "            }",
                    f"            UVIndex: *{unmerged_count} {{",
                    f'                a: {",".join(str(poly_idx) for poly_idx in polygon_indices)}',
                    "            }",
                    "        }",
                ]
            )

        lines.append(MATERIAL_LAYER)
        lines.extend(self._build_layer_blocks(has_normal, has_tangent, has_color, [idx for idx, _ in uv_sets]))
        lines.append("    }")
        return lines

    def _build_vertices_section(self, mesh: ConvertedMesh, polygon_indices: List[int]) -> List[str]:
        vertex_values: List[float] = []
        for vertex in mesh.vertices:
            vertex_values.extend(vertex.pos)

        polygon_values: List[str] = []
        for tri_idx in range(0, len(polygon_indices), 3):
            polygon_values.append(str(polygon_indices[tri_idx]))
            polygon_values.append(str(polygon_indices[tri_idx + 1]))
            polygon_values.append(str(-polygon_indices[tri_idx + 2] - 1))

        return [
            f"        Vertices: *{len(vertex_values)} {{",
            f"            a: {self._join_numbers(vertex_values)}",
            "        }",
            f"        PolygonVertexIndex: *{len(polygon_values)} {{",
            f'            a: {",".join(polygon_values)}',
            "        }",
            "        GeometryVersion: 124",
        ]

    def _build_model_lines(self, model_hash: str, model_name: str) -> List[str]:
        return [
            f'    Model: {model_hash}, "Model::{model_name}", "Mesh" {{',
            "        Version: 232",
            "        Properties70:  {",
            '            P: "InheritType", "enum", "", "",1',
            '            P: "ScalingMax", "Vector3D", "Vector", "",0,0,0',
            '            P: "DefaultAttributeIndex", "int", "Integer", "",0',
            "        }",
            '        Shading: W',
            '        Culling: "CullingOff"',
            "    }",
        ]

    def _build_layer_blocks(self, has_normal: bool, has_tangent: bool, has_color: bool, uv_indices: List[int]) -> List[str]:
        primary_entries: List[str] = []
        if has_normal:
            primary_entries.extend(
                [
                    "            LayerElement:  {",
                    '                Type: "LayerElementNormal"',
                    "                TypedIndex: 0",
                    "            }",
                ]
            )
        if has_tangent:
            primary_entries.extend(
                [
                    "            LayerElement:  {",
                    '                Type: "LayerElementTangent"',
                    "                TypedIndex: 0",
                    "            }",
                ]
            )
        if uv_indices:
            primary_entries.extend(
                [
                    "            LayerElement:  {",
                    '                Type: "LayerElementUV"',
                    "                TypedIndex: 0",
                    "            }",
                ]
            )
        if has_color:
            primary_entries.extend(
                [
                    "            LayerElement:  {",
                    '                Type: "LayerElementColor"',
                    "                TypedIndex: 0",
                    "            }",
                ]
            )
        primary_entries.extend(
            [
                "            LayerElement:  {",
                '                Type: "LayerElementMaterial"',
                "                TypedIndex: 0",
                "            }",
            ]
        )
        lines = ["        Layer: 0 {", "            Version: 100", *primary_entries, "        }"]
        for uv_index in uv_indices[1:]:
            lines.extend(
                [
                    f"        Layer: {uv_index} {{",
                    "            Version: 100",
                    "            LayerElement:  {",
                    '                Type: "LayerElementUV"',
                    f"                TypedIndex: {uv_index}",
                    "            }",
                    "        }",
                ]
            )
        return lines

    def _collect_uv_sets(self, vertices: Sequence[MeshVertex]) -> List[Tuple[int, List[float]]]:
        uv_sets: List[Tuple[int, List[float]]] = []
        for uv_index in range(4):
            values: List[float] = []
            has_any = False
            for vertex in vertices:
                uv = (vertex.uv0, vertex.uv1, vertex.uv2, vertex.uv3)[uv_index]
                values.extend(uv)
                if uv != (0.0, 0.0):
                    has_any = True
            if has_any:
                uv_sets.append((uv_index, values))
        return uv_sets

    def _build_vertex_lookup(self, mesh: ConvertedMesh) -> Dict[int, int]:
        lookup: Dict[int, int] = {}
        for index, vertex_id in enumerate(dict.fromkeys(mesh.polygon_vertex_ids)):
            lookup[vertex_id] = index
        return lookup

    @staticmethod
    def _read_rows(csv_path: str | Path) -> List[List[str]]:
        with Path(csv_path).open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            return [row for row in csv.reader(handle) if row]

    @staticmethod
    def _find_header(headers: Sequence[str], hints: Sequence[str] | str) -> Optional[str]:
        if isinstance(hints, str):
            hints = [hints]
        normalized_hints = [CsvModelConverter._normalize_header(hint) for hint in hints if hint]
        if not normalized_hints:
            return None
        for header in headers:
            normalized_header = CsvModelConverter._normalize_header(header)
            for hint in normalized_hints:
                if hint and hint in normalized_header:
                    return header
        return None

    @staticmethod
    def _normalize_header(value: str) -> str:
        return "".join(ch for ch in (value or "").strip().lower() if not ch.isspace())

    @staticmethod
    def _group_attribute_headers(headers: Sequence[str]) -> List[Dict[str, object]]:
        groups: List[Dict[str, object]] = []
        idx = 0
        while idx < len(headers):
            header = headers[idx]
            if idx < 2 or "." not in header:
                idx += 1
                continue
            base = header.rsplit(".", 1)[0]
            start_idx = idx
            while idx < len(headers) and headers[idx].startswith(base + "."):
                idx += 1
            groups.append(
                {
                    "name": base,
                    "header": headers[start_idx],
                    "start_idx": start_idx,
                    "comp_count": idx - start_idx,
                    "header_index": len(groups),
                }
            )
        return groups

    @staticmethod
    def _analyze_attribute_groups(rows: Sequence[Sequence[str]], groups: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
        stats: List[Dict[str, object]] = []
        for group in groups:
            start_idx = int(group["start_idx"])
            comp_count = int(group["comp_count"])
            values: List[List[float]] = []
            for row in rows:
                if len(row) < start_idx + comp_count:
                    continue
                values.append([CsvModelConverter._parse_float(row[start_idx + offset], 0.0) for offset in range(comp_count)])
            if not values:
                continue

            mins = [min(item[offset] for item in values) for offset in range(comp_count)]
            maxs = [max(item[offset] for item in values) for offset in range(comp_count)]
            spans = [maxs[offset] - mins[offset] for offset in range(comp_count)]
            flat = [component for item in values for component in item]
            xyz_lengths = [math.sqrt(sum(component * component for component in item[: min(3, len(item))])) for item in values]
            integerish = all(abs(value - round(value)) <= 1e-4 for value in flat)
            w_values = [item[3] for item in values if len(item) >= 4]
            w_sign_ratio = 0.0
            if w_values:
                sign_like = sum(1 for item in w_values if abs(abs(item) - 1.0) <= 0.05)
                w_sign_ratio = float(sign_like) / float(len(w_values))

            stats.append(
                {
                    **group,
                    "mins": mins,
                    "maxs": maxs,
                    "spans": spans,
                    "span_sum": sum(abs(item) for item in spans),
                    "max_abs": max(max(abs(item) for item in mins), max(abs(item) for item in maxs)),
                    "normalized_01": min(mins) >= -1e-4 and max(maxs) <= 1.001,
                    "signed_unit_range": min(mins) >= -1.001 and max(maxs) <= 1.001,
                    "integerish": integerish,
                    "unit_length_error": abs((sum(xyz_lengths) / max(len(xyz_lengths), 1)) - 1.0),
                    "w_sign_ratio": w_sign_ratio,
                }
            )
        return stats

    @staticmethod
    def _pick_best_candidate(
        stats: Sequence[Dict[str, object]],
        used_headers: set[str],
        predicate,
        sort_key,
    ) -> Optional[Dict[str, object]]:
        candidates = [item for item in stats if str(item["header"]) not in used_headers and predicate(item)]
        if not candidates:
            return None
        return sorted(candidates, key=sort_key, reverse=True)[0]

    @staticmethod
    def _pick_sorted_candidates(
        stats: Sequence[Dict[str, object]],
        used_headers: set[str],
        predicate,
        sort_key,
    ) -> List[Dict[str, object]]:
        candidates = [item for item in stats if str(item["header"]) not in used_headers and predicate(item)]
        return sorted(candidates, key=sort_key, reverse=True)

    @staticmethod
    def _require_column(headers: Sequence[str], hint: str, label: str) -> int:
        if not hint:
            raise ValueError(f"missing required mapping: {label}")
        idx = CsvModelConverter._optional_column(headers, hint)
        if idx is None:
            raise ValueError(f"mapped column not found for {label}: {hint}")
        return idx

    @staticmethod
    def _optional_column(headers: Sequence[str], hint: str) -> Optional[int]:
        if not hint:
            return None
        for idx, header in enumerate(headers):
            if hint in header:
                return idx
        return None

    @staticmethod
    def _read_component(row: Sequence[str], start_idx: Optional[int], offset: int, default: float = 0.0) -> float:
        if start_idx is None:
            return default
        idx = start_idx + offset
        if idx >= len(row):
            return default
        return CsvModelConverter._parse_float(row[idx], default)

    @staticmethod
    def _read_vec2(row: Sequence[str], start_idx: Optional[int]) -> Tuple[float, float]:
        return (
            CsvModelConverter._read_component(row, start_idx, 0),
            CsvModelConverter._read_component(row, start_idx, 1),
        )

    @staticmethod
    def _read_vec4(row: Sequence[str], start_idx: Optional[int], default: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
        if start_idx is None:
            return default
        return (
            CsvModelConverter._read_component(row, start_idx, 0, default[0]),
            CsvModelConverter._read_component(row, start_idx, 1, default[1]),
            CsvModelConverter._read_component(row, start_idx, 2, default[2]),
            CsvModelConverter._read_component(row, start_idx, 3, default[3]),
        )

    @staticmethod
    def _parse_float(value: str, default: float = 0.0) -> float:
        text = (value or "").strip()
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            return default

    @staticmethod
    def _parse_int(value: str, default: int) -> int:
        text = (value or "").strip()
        if not text:
            return default
        try:
            return int(float(text))
        except ValueError:
            return default

    @staticmethod
    def _join_numbers(values: Iterable[float]) -> str:
        return ",".join(CsvModelConverter._fmt(value) for value in values)

    @staticmethod
    def _fmt(value: float) -> str:
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"

    @staticmethod
    def _parse_obj_face_ref(ref: str) -> Tuple[int, int, int]:
        parts = ref.split("/")
        pos_idx = CsvModelConverter._parse_obj_index(parts[0]) if len(parts) > 0 else -1
        uv_idx = CsvModelConverter._parse_obj_index(parts[1]) if len(parts) > 1 and parts[1] else -1
        normal_idx = CsvModelConverter._parse_obj_index(parts[2]) if len(parts) > 2 and parts[2] else -1
        return pos_idx, uv_idx, normal_idx

    @staticmethod
    def _parse_obj_index(value: str) -> int:
        try:
            index = int(value)
        except ValueError:
            return -1
        return index - 1 if index > 0 else -1
