#!/usr/bin/env python3
"""
ToonCar R3D Code-Guided Unpacker v102

This revision uses loader behavior verified directly in ToonCar.exe.

Confirmed runtime layout at the beginning of track R3D files:
  texture bank
  uint32 material_count
  material_count * 0x60-byte material records
  uint32 animation_count
  animation_count * 0x1A8-byte animation records
  mesh:
      0x40-byte header
      vertex_count * 0x30
      face_count   * 0x24

The original executable also contains nested object loaders that conditionally
load additional 0x30-byte object records, spatial structures, and more meshes.
Their full semantic graph is not decoded yet, so v102 combines:
  1) exact sequential parsing for confirmed top-level sections
  2) exhaustive signature-based extraction for all additional known mesh and
     texture-bank structures
  3) optional raw preservation for reverse-engineering/debug exports
"""

from __future__ import annotations

import argparse
import base64
import zlib
import tempfile
import binascii
import hashlib
import json
import math
import os
import re
import struct
import zlib
import shutil
import subprocess
from pathlib import Path

TEXTURE_HEADER_SIZE = 148
MATERIAL_RECORD_SIZE = 0x60
ANIMATION_RECORD_SIZE = 0x1A8
MESH_HEADER_SIZE = 0x40
VERTEX_STRIDE = 0x30
FACE_STRIDE = 0x24

IMAGE_EXTS = ("bmp", "tga", "dds", "png", "jpg", "jpeg")
FILE_EXTS = (
    "bmp","tga","dds","png","jpg","jpeg",
    "wav","ogg","mp3","mid","midi","flac",
    "r3d","r3a","dat","rec","txt","cfg","ini","car","chc","cnt"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def read_cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin1", errors="replace")


def safe_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return s.strip("._") or "asset"


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)
    )


def write_rgba_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    stride = width * 4
    rows = [b"\x00" + rgba[y*stride:(y+1)*stride] for y in range(height)]
    raw = b"".join(rows)

    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png += png_chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
    )
    png += png_chunk(b"IDAT", zlib.compress(raw, 9))
    png += png_chunk(b"IEND", b"")
    path.write_bytes(png)



def read_exported_rgba_png(path: Path):
    """
    Read PNGs produced by write_rgba_png() without requiring Pillow.
    The unpacker writes 8-bit RGBA, non-interlaced PNG rows with filter 0.
    """
    raw = Path(path).read_bytes()
    signature = b"\x89PNG\r\n\x1a\n"
    if not raw.startswith(signature):
        raise ValueError(f"Nieprawidłowy PNG: {path}")

    pos = len(signature)
    width = height = None
    bit_depth = color_type = None
    interlace = None
    idat = bytearray()

    while pos + 12 <= len(raw):
        length = struct.unpack_from(">I", raw, pos)[0]
        kind = raw[pos + 4:pos + 8]
        payload = raw[pos + 8:pos + 8 + length]
        pos += 12 + length

        if kind == b"IHDR":
            (
                width,
                height,
                bit_depth,
                color_type,
                _compression,
                _filter_method,
                interlace,
            ) = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break

    if (
        width is None
        or height is None
        or bit_depth != 8
        or color_type != 6
        or interlace != 0
    ):
        raise ValueError(
            f"Nieobsługiwany format PNG eksportera: {path}"
        )

    decoded = zlib.decompress(bytes(idat))
    row_bytes = width * 4
    expected = height * (row_bytes + 1)
    if len(decoded) != expected:
        raise ValueError(
            f"Nieprawidłowy rozmiar danych PNG: {path}"
        )

    rgba = bytearray(width * height * 4)
    src = 0
    dst = 0

    for _y in range(height):
        filter_type = decoded[src]
        src += 1
        if filter_type != 0:
            raise ValueError(
                f"Nieobsługiwany filtr PNG {filter_type}: {path}"
            )

        rgba[dst:dst + row_bytes] = decoded[
            src:src + row_bytes
        ]
        src += row_bytes
        dst += row_bytes

    return int(width), int(height), bytes(rgba)


def resize_rgba_nearest(
    rgba: bytes,
    src_width: int,
    src_height: int,
    dst_width: int,
    dst_height: int,
):
    if (
        src_width == dst_width
        and src_height == dst_height
    ):
        return rgba

    out = bytearray(
        dst_width * dst_height * 4
    )

    for y in range(dst_height):
        sy = min(
            src_height - 1,
            int(y * src_height / dst_height),
        )
        for x in range(dst_width):
            sx = min(
                src_width - 1,
                int(x * src_width / dst_width),
            )

            src_off = (
                sy * src_width + sx
            ) * 4
            dst_off = (
                y * dst_width + x
            ) * 4

            out[dst_off:dst_off + 4] = rgba[
                src_off:src_off + 4
            ]

    return bytes(out)


def build_tooncar_texture_tick_timeline(
    frame_count: int,
    frame_step_per_tick: float,
):
    """
    Reproduce one complete 0x4027F0 texture-animation cycle.

    Returns one source-frame index per 55 Hz game tick.
    """
    frame_count = int(frame_count)
    frame_step_per_tick = float(
        frame_step_per_tick
    )

    if frame_count <= 0:
        return []

    if frame_step_per_tick <= 0.0:
        return list(range(frame_count))

    accumulator = 0.0
    counter = 0
    current_source_index = 0
    selected_frames = 0

    max_ticks = max(
        1000,
        int(
            frame_count
            / max(frame_step_per_tick, 1e-6)
            * 4
        )
        + 100,
    )

    timeline = []

    for _tick in range(max_ticks):
        if accumulator <= 0.0:
            source_index = (
                counter % frame_count
            )

            if selected_frames >= frame_count:
                break

            current_source_index = (
                source_index
            )
            counter += 1
            selected_frames += 1
            accumulator += 1.0

        timeline.append(
            int(current_source_index)
        )
        accumulator -= frame_step_per_tick

    return timeline


def _atlas_copy_rgba(
    atlas: bytearray,
    atlas_width: int,
    cell_width: int,
    cell_height: int,
    column: int,
    row: int,
    frame_rgba: bytes,
):
    x0 = column * cell_width
    y0 = row * cell_height

    row_bytes = cell_width * 4
    atlas_row_bytes = atlas_width * 4

    for y in range(cell_height):
        src_off = y * row_bytes
        dst_off = (
            (y0 + y) * atlas_row_bytes
            + x0 * 4
        )
        atlas[dst_off:dst_off + row_bytes] = (
            frame_rgba[
                src_off:src_off + row_bytes
            ]
        )


def prepare_gltf_runtime_assets(
    unpacked_dir: Path,
    manifest: dict,
    log=print,
):
    """
    Create external runtime assets only for the glTF / Three.js preset.

    Layout:
      gltf/
        runtime.json
        texture_animations.json
        texture_animations/
          anim_XX_mat_YYY.png
          anim_XX_mat_YYY.json
        skybox/
          UP.png DN.png FR.png BK.png LF.png RT.png
          skybox.json
    """
    unpacked_dir = Path(
        unpacked_dir
    ).resolve()

    gltf_dir = unpacked_dir / "gltf"
    anim_dir = (
        gltf_dir / "texture_animations"
    )
    skybox_dir = gltf_dir / "skybox"

    gltf_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    if anim_dir.exists():
        shutil.rmtree(anim_dir)
    anim_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    if skybox_dir.exists():
        shutil.rmtree(skybox_dir)
    skybox_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    texture_banks = manifest.get(
        "texture_banks",
        [],
    )

    primary_bank = next(
        (
            bank
            for bank in texture_banks
            if int(bank.get("offset", -1)) == 0
        ),
        texture_banks[0]
        if texture_banks
        else None,
    )

    animation_index_entries = []

    if primary_bank:
        hash_to_entry = {
            tooncar_name_hash(entry["name"]): entry
            for entry in primary_bank.get(
                "entries",
                [],
            )
        }

        source_texture_dir = (
            unpacked_dir
            / "textures"
            / primary_bank["directory"]
        )

        for anim in manifest.get(
            "texture_animations",
            [],
        ):
            material_index = anim.get(
                "material_index"
            )
            frame_ids = [
                int(x)
                for x in anim.get(
                    "frame_resource_ids",
                    [],
                )
            ]

            if (
                material_index is None
                or not frame_ids
            ):
                continue

            frame_entries = []
            valid = True

            for resource_id in frame_ids:
                entry = hash_to_entry.get(
                    resource_id
                )
                if not entry:
                    valid = False
                    break
                frame_entries.append(entry)

            if not valid or not frame_entries:
                continue

            loaded_frames = []

            for entry in frame_entries:
                png_path = (
                    source_texture_dir
                    / entry["png"]
                )

                width, height, rgba = (
                    read_exported_rgba_png(
                        png_path
                    )
                )

                loaded_frames.append({
                    "entry": entry,
                    "width": width,
                    "height": height,
                    "rgba": rgba,
                })

            cell_width = max(
                frame["width"]
                for frame in loaded_frames
            )
            cell_height = max(
                frame["height"]
                for frame in loaded_frames
            )

            frame_count = len(
                loaded_frames
            )

            columns = max(
                1,
                int(math.ceil(
                    math.sqrt(frame_count)
                )),
            )
            rows = int(
                math.ceil(
                    frame_count / columns
                )
            )

            atlas_width = (
                columns * cell_width
            )
            atlas_height = (
                rows * cell_height
            )

            atlas = bytearray(
                atlas_width
                * atlas_height
                * 4
            )

            frame_metadata = []

            for frame_index, frame in enumerate(
                loaded_frames
            ):
                column = (
                    frame_index % columns
                )
                row_top = (
                    frame_index // columns
                )

                normalized_rgba = (
                    resize_rgba_nearest(
                        frame["rgba"],
                        frame["width"],
                        frame["height"],
                        cell_width,
                        cell_height,
                    )
                )

                _atlas_copy_rgba(
                    atlas,
                    atlas_width,
                    cell_width,
                    cell_height,
                    column,
                    row_top,
                    normalized_rgba,
                )

                # Atlas pixels are described with top-left rows, while
                # Three.js UV offset uses bottom-left origin.
                uv_scale = [
                    1.0 / columns,
                    1.0 / rows,
                ]
                uv_offset = [
                    column / columns,
                    1.0
                    - ((row_top + 1) / rows),
                ]

                frame_metadata.append({
                    "index": frame_index,
                    "sourceName": frame[
                        "entry"
                    ]["name"],
                    "sourcePng": frame[
                        "entry"
                    ]["png"],
                    "sourceResourceId": (
                        frame_ids[
                            frame_index
                        ]
                    ),
                    "column": column,
                    "rowTop": row_top,
                    "uvOffset": uv_offset,
                    "uvScale": uv_scale,
                })

            base_name = (
                f"anim_{int(anim['index']):02d}"
                f"_mat_{int(material_index):03d}"
            )

            atlas_name = (
                base_name + ".png"
            )
            json_name = (
                base_name + ".json"
            )

            atlas_path = (
                anim_dir / atlas_name
            )
            json_path = (
                anim_dir / json_name
            )

            write_rgba_png(
                atlas_path,
                atlas_width,
                atlas_height,
                bytes(atlas),
            )

            frame_step = float(
                anim.get(
                    "frame_step_per_tick",
                    0.0,
                )
                or 0.0
            )

            tick_frames = (
                build_tooncar_texture_tick_timeline(
                    frame_count,
                    frame_step,
                )
            )

            frame_durations_ticks = [
                tick_frames.count(i)
                for i in range(
                    frame_count
                )
            ]

            anim_metadata = {
                "schema": (
                    "tooncar-texture-animation-v1"
                ),
                "animationIndex": int(
                    anim["index"]
                ),
                "materialIndex": int(
                    material_index
                ),
                "materialResourceId": int(
                    anim.get(
                        "material_resource_id",
                        0,
                    )
                ),
                "loop": True,
                "tickRateHz": 55,
                "frameStepPerTick": (
                    frame_step
                ),
                "cycleTicks": len(
                    tick_frames
                ),
                "cycleSeconds": (
                    len(tick_frames) / 55.0
                    if tick_frames
                    else 0.0
                ),
                "tickFrames": (
                    tick_frames
                ),
                "frameDurationsTicks": (
                    frame_durations_ticks
                ),
                "atlas": {
                    "file": atlas_name,
                    "width": atlas_width,
                    "height": atlas_height,
                    "columns": columns,
                    "rows": rows,
                    "cellWidth": cell_width,
                    "cellHeight": cell_height,
                    "uvOrigin": (
                        "bottom-left"
                    ),
                },
                "frames": frame_metadata,
                "threeJsPlayback": {
                    "method": (
                        "texture.offset + "
                        "texture.repeat"
                    ),
                    "repeat": [
                        1.0 / columns,
                        1.0 / rows,
                    ],
                    "frameSelection": (
                        "floor(elapsedSeconds * "
                        "tickRateHz) % cycleTicks "
                        "-> tickFrames[tick]"
                    ),
                    "flipY": False,
                },
            }

            json_path.write_text(
                json.dumps(
                    anim_metadata,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            animation_index_entries.append({
                "animationIndex": int(
                    anim["index"]
                ),
                "materialIndex": int(
                    material_index
                ),
                "atlas": (
                    "texture_animations/"
                    + atlas_name
                ),
                "metadata": (
                    "texture_animations/"
                    + json_name
                ),
                "frameCount": (
                    frame_count
                ),
                "cycleTicks": len(
                    tick_frames
                ),
                "cycleSeconds": (
                    len(tick_frames) / 55.0
                    if tick_frames
                    else 0.0
                ),
            })

    texture_index = {
        "schema": (
            "tooncar-texture-animations-index-v1"
        ),
        "source": manifest.get(
            "source",
            {},
        ),
        "tickRateHz": 55,
        "animations": (
            animation_index_entries
        ),
    }

    (
        gltf_dir
        / "texture_animations.json"
    ).write_text(
        json.dumps(
            texture_index,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # ---------------------------------------------------------------
    # Duplicate the original six ToonCar skybox faces.
    # ---------------------------------------------------------------
    skybox_manifest = manifest.get(
        "skybox"
    )
    skybox_runtime = None

    if skybox_manifest:
        bank_dir = skybox_manifest.get(
            "bank_directory"
        )
        faces = skybox_manifest.get(
            "faces",
            {},
        )

        copied_faces = {}

        for face in (
            "UP",
            "DN",
            "FR",
            "BK",
            "LF",
            "RT",
        ):
            info = faces.get(face)
            if not info or not bank_dir:
                continue

            src = (
                unpacked_dir
                / "textures"
                / bank_dir
                / info["png"]
            )

            dst_name = (
                face + ".png"
            )
            dst = (
                skybox_dir / dst_name
            )

            if src.is_file():
                shutil.copy2(
                    src,
                    dst,
                )
                copied_faces[face] = {
                    "file": dst_name,
                    "sourceName": info.get(
                        "source_name"
                    ),
                    "sourcePng": info.get(
                        "png"
                    ),
                }

        if len(copied_faces) == 6:
            skybox_runtime = {
                "schema": (
                    "tooncar-skybox-v1"
                ),
                "type": "cubemap-6-faces",
                "faces": copied_faces,
                "tooncarMeshFaceOrder": [
                    "RT",
                    "LF",
                    "FR",
                    "BK",
                    "UP",
                    "DN",
                ],
                "sideUvFlip": {
                    "RT": True,
                    "LF": True,
                    "FR": True,
                    "BK": True,
                    "UP": False,
                    "DN": False,
                },
                "note": (
                    "These are the original ToonCar skybox faces. "
                    "The current Blender skybox uses flipped U+V "
                    "on the four side faces."
                ),
            }

            (
                skybox_dir
                / "skybox.json"
            ).write_text(
                json.dumps(
                    skybox_runtime,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    runtime_manifest = {
        "schema": (
            "tooncar-threejs-runtime-v1"
        ),
        "source": manifest.get(
            "source",
            {},
        ),
        "textureAnimations": (
            "texture_animations.json"
        ),
        "skybox": (
            "skybox/skybox.json"
            if skybox_runtime
            else None
        ),
        "expectedModel": (
            Path(
                manifest.get(
                    "source",
                    {},
                ).get(
                    "filename",
                    "ToonCar.r3d",
                )
            ).stem
            + ".glb"
        ),
        "notes": {
            "transformAnimations": (
                "Play GLTF animation clips with "
                "THREE.AnimationMixer and LoopRepeat."
            ),
            "textureAnimations": (
                "Use atlas JSON tickFrames at 55 Hz."
            ),
        },
    }

    (
        gltf_dir / "runtime.json"
    ).write_text(
        json.dumps(
            runtime_manifest,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log(
        "glTF runtime: "
        f"{len(animation_index_entries)} atlas(y/ów) "
        f"animowanych tekstur."
    )

    if skybox_runtime:
        log(
            "glTF runtime: skopiowano 6 ścian skyboxa."
        )

    return {
        "directory": str(
            gltf_dir
        ),
        "texture_animations": (
            animation_index_entries
        ),
        "skybox": (
            skybox_runtime
        ),
    }


def bgra_to_rgba(raw: bytes, keep_alpha: bool) -> bytes:
    out = bytearray(len(raw))
    for i in range(0, len(raw), 4):
        b, g, r, a = raw[i:i+4]
        out[i:i+4] = bytes((r, g, b, a if keep_alpha else 255))
    return bytes(out)


# ---------------------------------------------------------------------------
# TEXTURE BANK
# Verified from ToonCar.exe function 0x4541A0:
#   fread(count, 4)
#   repeat count:
#       fread(name, 0x80)
#       texture.Load(file)
#
# Current files show texture.Load payload as the already reverse-engineered
# 20-byte descriptor followed by width*height*4 BGRA bytes.
# ---------------------------------------------------------------------------

def try_texture_bank(data: bytes, offset: int):
    if offset < 0 or offset + 4 > len(data):
        return None

    count = read_u32(data, offset)
    if not (1 <= count <= 512):
        return None

    cur = offset + 4
    entries = []

    for index in range(count):
        if cur + TEXTURE_HEADER_SIZE > len(data):
            return None

        name = read_cstr(data[cur:cur+128])
        if not re.search(r"\.(?:" + "|".join(IMAGE_EXTS) + r")$", name, re.I):
            return None

        width, height, flags, unknown, descriptor_size = struct.unpack_from(
            "<5I", data, cur + 128
        )

        if not (1 <= width <= 8192 and 1 <= height <= 8192):
            return None
        if descriptor_size not in (0, 0x14):
            return None

        pixel_offset = cur + TEXTURE_HEADER_SIZE
        pixel_size = width * height * 4
        end = pixel_offset + pixel_size

        if end > len(data):
            return None

        entries.append({
            "index": index,
            "name": name,
            "header_offset": cur,
            "width": width,
            "height": height,
            "flags": flags,
            "unknown": unknown,
            "descriptor_size": descriptor_size,
            "pixel_offset": pixel_offset,
            "pixel_size": pixel_size,
            "end_offset": end,
        })
        cur = end

    return {
        "offset": offset,
        "end_offset": cur,
        "size": cur - offset,
        "count": count,
        "entries": entries,
    }


def find_all_texture_banks(data: bytes):
    candidates = {0}

    # Instead of stepping by 4 bytes (which missed Luna's unaligned sky bank),
    # derive likely bank starts from every embedded image filename.
    ext_re = rb"\.(?:" + b"|".join(x.encode() for x in IMAGE_EXTS) + rb")\x00"
    for m in re.finditer(ext_re, data, flags=re.I):
        # Regex starts at the extension (".bmp"), not at the filename.
        # Recover the filename start by looking back to the previous NUL.
        search_from = max(0, m.start() - 128)
        prev_nul = data.rfind(b"\x00", search_from, m.start())
        name_start = prev_nul + 1 if prev_nul >= search_from else search_from
        candidates.add(name_start - 4)

    found = []
    for off in sorted(candidates):
        bank = try_texture_bank(data, off)
        if bank:
            if not any(
                bank["offset"] == b["offset"] and bank["end_offset"] == b["end_offset"]
                for b in found
            ):
                found.append(bank)

    # Remove banks contained inside a larger valid bank.
    final = []
    for bank in sorted(found, key=lambda b: (b["offset"], -b["size"])):
        if any(
            bank["offset"] >= x["offset"] and bank["end_offset"] <= x["end_offset"]
            for x in final
        ):
            continue
        final.append(bank)

    return final



def analyze_texture_alpha(raw_bgra: bytes, keep_alpha: bool):
    """
    Classify embedded texture alpha for Blender.

    ToonCar stores alpha directly in the fourth BGRA byte when the texture
    flag has bit 0x80000000 set.

    Modes:
      opaque  - alpha flag not present
      clip    - alpha is effectively binary / edge-antialiased mask
      blend   - substantial grayscale alpha, so true translucency is intended

    This is intentionally based on the actual pixel channel rather than
    filename heuristics.
    """
    if not keep_alpha or len(raw_bgra) < 4:
        return {
            "mode": "opaque",
            "alpha_min": 255,
            "alpha_max": 255,
            "alpha_unique": 1,
            "transparent_fraction": 0.0,
            "opaque_fraction": 1.0,
            "intermediate_fraction": 0.0,
        }

    alpha = raw_bgra[3::4]
    count = len(alpha)
    if count == 0:
        return {
            "mode": "opaque",
            "alpha_min": 255,
            "alpha_max": 255,
            "alpha_unique": 1,
            "transparent_fraction": 0.0,
            "opaque_fraction": 1.0,
            "intermediate_fraction": 0.0,
        }

    unique_values = set(alpha)
    transparent = sum(1 for a in alpha if a <= 15)
    opaque = sum(1 for a in alpha if a >= 240)
    intermediate = count - transparent - opaque

    transparent_fraction = transparent / count
    opaque_fraction = opaque / count
    intermediate_fraction = intermediate / count

    # Anti-aliased cutout edges normally use only a small percentage of
    # intermediate values. If grayscale alpha is a meaningful part of the
    # image, treat it as true blended translucency.
    if intermediate_fraction >= 0.10:
        mode = "blend"
    else:
        mode = "clip"

    return {
        "mode": mode,
        "alpha_min": min(alpha),
        "alpha_max": max(alpha),
        "alpha_unique": len(unique_values),
        "transparent_fraction": transparent_fraction,
        "opaque_fraction": opaque_fraction,
        "intermediate_fraction": intermediate_fraction,
    }


def export_texture_banks(
    data: bytes,
    banks,
    root: Path,
    export_raw_data=False,
):
    root.mkdir(parents=True, exist_ok=True)
    output = []

    for bank_idx, bank in enumerate(banks):
        d = root / f"bank_{bank_idx:02d}_0x{bank['offset']:X}"
        d.mkdir(exist_ok=True)

        manifest_entries = []
        for e in bank["entries"]:
            raw = data[e["pixel_offset"]:e["pixel_offset"] + e["pixel_size"]]
            keep_alpha = bool(e["flags"] & 0x80000000)
            rgba = bgra_to_rgba(raw, keep_alpha)

            stem = safe_name(Path(e["name"]).stem)
            png_name = f"{e['index']:03d}_{stem}.png"
            raw_name = f"{e['index']:03d}_{stem}.bgra"

            write_rgba_png(
                d / png_name,
                e["width"],
                e["height"],
                rgba,
            )

            raw_path = d / raw_name
            if export_raw_data:
                raw_path.write_bytes(raw)
            elif raw_path.exists():
                raw_path.unlink()

            alpha_info = analyze_texture_alpha(
                raw,
                keep_alpha,
            )

            entry_manifest = {
                **e,
                "png": png_name,
                "has_alpha": keep_alpha,
                "alpha": alpha_info,
                "sha256_pixels": sha256(raw),
            }

            if export_raw_data:
                entry_manifest["raw_bgra"] = raw_name

            manifest_entries.append(
                entry_manifest
            )

        output.append({
            "offset": bank["offset"],
            "end_offset": bank["end_offset"],
            "size": bank["size"],
            "count": bank["count"],
            "directory": d.name,
            "entries": manifest_entries,
        })

    return output


# ---------------------------------------------------------------------------
# MATERIALS + TEXTURE ANIMATIONS
# Verified from the main track loader:
#   uint32 material_count
#   material_count * 0x60
#   uint32 anim_count
#   anim_count * 0x1A8
# ---------------------------------------------------------------------------

def parse_material_records(data: bytes, offset: int):
    if offset + 4 > len(data):
        raise ValueError("Brak material_count")

    count = read_u32(data, offset)
    start = offset + 4
    end = start + count * MATERIAL_RECORD_SIZE
    if count > 10000 or end > len(data):
        raise ValueError("Nieprawidłowa tabela materiałów")

    records = []
    for i in range(count):
        roff = start + i * MATERIAL_RECORD_SIZE
        raw = data[roff:roff + MATERIAL_RECORD_SIZE]
        dwords = list(struct.unpack("<24I", raw))
        floats = list(struct.unpack("<24f", raw))
        records.append({
            "index": i,
            "offset": roff,
            "resource_id": dwords[0],
            # Verified from ToonCar.exe and track data:
            # material record +0x38 stores the case-insensitive ToonCar
            # filename hash of the texture resource.
            "texture_name_hash": dwords[0x38 // 4],
            "dwords": dwords,
            "floats": floats,
            "sha256": sha256(raw),
        })

    return records, end


def parse_animation_records(data: bytes, offset: int, material_records):
    if offset + 4 > len(data):
        raise ValueError("Brak animation_count")

    count = read_u32(data, offset)
    start = offset + 4
    end = start + count * ANIMATION_RECORD_SIZE
    if count > 10000 or end > len(data):
        raise ValueError("Nieprawidłowa tabela animacji")

    material_by_id = {
        r["resource_id"]: r["index"]
        for r in material_records
    }

    records = []
    for i in range(count):
        roff = start + i * ANIMATION_RECORD_SIZE
        raw = data[roff:roff + ANIMATION_RECORD_SIZE]
        dwords = list(struct.unpack("<106I", raw))

        material_id = dwords[0]
        frame_count = dwords[2]
        # EXE-backed structure strongly indicates frame IDs begin at +0x0C.
        # Keep a conservative cap instead of trusting corrupt counts.
        frame_count_safe = min(frame_count, 100)
        frame_ids = dwords[3:3 + frame_count_safe]
        frame_time = struct.unpack_from("<f", raw, ANIMATION_RECORD_SIZE - 4)[0]

        records.append({
            "index": i,
            "offset": roff,
            "material_resource_id": material_id,
            "material_index": material_by_id.get(material_id),
            "frame_count": frame_count,
            "frame_resource_ids": frame_ids,
            "frame_step_per_tick": frame_time,
            "raw_dwords": dwords,
            "sha256": sha256(raw),
        })

    return records, end


# ---------------------------------------------------------------------------
# MESH
# Verified directly from ToonCar.exe function 0x460880:
#   fread(header, 0x40)
#   fread(vertex_array, vertex_count, 0x30)
#   fread(face_array,   face_count,   0x24)
# ---------------------------------------------------------------------------

def try_mesh(data: bytes, offset: int, full_validate=True):
    if offset < 0 or offset + MESH_HEADER_SIZE > len(data):
        return None

    header = struct.unpack_from("<16I", data, offset)
    vc = header[0]
    fc = header[4]
    mc = header[6]

    # Loader overwrites pointer fields, while serialized R3D stores pointer-like
    # values in several header slots. The two zero fields at +0x08/+0x0C are a
    # particularly strong signature in known track meshes.
    if header[2] != 0 or header[3] != 0:
        return None

    if not (3 <= vc <= 65535 and 1 <= fc <= 300000 and 1 <= mc <= 2048):
        return None

    vs = offset + MESH_HEADER_SIZE
    fs = vs + vc * VERTEX_STRIDE
    end = fs + fc * FACE_STRIDE
    if end > len(data):
        return None

    # Validate a few vertex positions.
    for i in {0, vc // 2, vc - 1}:
        x, y, z = struct.unpack_from("<3f", data, vs + i * VERTEX_STRIDE)
        if not all(math.isfinite(v) and abs(v) < 1e9 for v in (x, y, z)):
            return None

    indices = range(fc) if full_validate and fc <= 20000 else (
        list(range(min(100, fc))) +
        list(range(max(0, fc-100), fc))
    )

    used_materials = set()
    for i in indices:
        foff = fs + i * FACE_STRIDE
        a, b, c, pad = struct.unpack_from("<4H", data, foff)
        mat = read_u32(data, foff + 32)
        if max(a, b, c) >= vc:
            return None
        # Some asset mesh headers may declare more material slots than used,
        # but used IDs must still be reasonably small.
        if mat > 4096:
            return None
        used_materials.add(mat)

    bbox = list(struct.unpack_from("<6f", data, offset + 0x20))

    return {
        "offset": offset,
        "end_offset": end,
        "size": end - offset,
        "vertex_count": vc,
        "face_count": fc,
        "material_count": mc,
        "vertex_start": vs,
        "face_start": fs,
        "bbox_header": bbox,
        "header_dwords": list(header),
        "sample_used_material_ids": sorted(used_materials),
    }


def find_all_meshes(data: bytes):
    # Strong signature search: bytes +8..+15 of known mesh headers are 8 zeros.
    # Search every byte, not just DWORD alignment.
    starts = set()
    needle = b"\x00" * 8
    pos = 0
    while True:
        p = data.find(needle, pos)
        if p < 0:
            break
        starts.add(p - 8)
        pos = p + 1

    meshes = []
    for off in sorted(starts):
        mesh = try_mesh(data, off, full_validate=False)
        if mesh:
            mesh = try_mesh(data, off, full_validate=True)
            if mesh:
                meshes.append(mesh)

    # Deduplicate exact/contained matches.
    unique = []
    for m in sorted(meshes, key=lambda x: (x["offset"], -x["size"])):
        if any(
            m["offset"] >= u["offset"] and m["end_offset"] <= u["end_offset"]
            for u in unique
        ):
            continue
        unique.append(m)

    return unique



def tooncar_name_hash(name: str):
    """
    Exact case-insensitive filename hash used by ToonCar.exe at 0x47C6E0
    for normal (non-'@') resource names.

    The executable uppercases ASCII a-z and sums products of adjacent
    characters cyclically:
        H = sum( upper(s[i]) * upper(s[(i+1) % n]) )

    Verified against R3D material records, e.g.
        piedras25.bmp       -> 0xF15A
        Bandera_Tooncar.bmp -> 0x18D93
    """
    if not name:
        return 0

    if name.startswith("@"):
        # EXE handles '@...' through a separate numeric path.
        # Embedded texture filenames in tested track R3Ds do not use it.
        try:
            return int(name[1:], 0) & 0xFFFFFFFF
        except ValueError:
            return 0

    raw = name.encode("latin1", errors="replace")
    n = len(raw)
    if n == 0:
        return 0

    def upper_ascii(value):
        if 0x61 <= value <= 0x7A:
            return value - 0x20
        return value

    result = 0
    for i in range(n):
        a = upper_ascii(raw[i])
        b = upper_ascii(raw[(i + 1) % n])
        result = (result + a * b) & 0xFFFFFFFF

    return result


def build_texture_hash_index(primary_bank_manifest):
    index = {}
    collisions = {}

    for tex in primary_bank_manifest["entries"]:
        h = tooncar_name_hash(tex["name"])
        if h in index:
            collisions.setdefault(h, [index[h]]).append(tex)
        else:
            index[h] = tex

    return index, collisions


def build_exact_main_mesh_mapping(
    mesh,
    material_records,
    primary_bank_manifest,
    animations,
):
    """
    Map each main mesh material slot exactly as the game does:
      slot N
        -> material record N
        -> uint32 at record +0x38
        -> ToonCar filename hash
        -> embedded texture filename

    The tested tracks have more global material records than main-mesh slots;
    the first mesh.material_count records correspond to the main mesh slots.
    """
    texture_by_hash, collisions = build_texture_hash_index(
        primary_bank_manifest
    )

    # Animation frame hashes are kept as a fallback/diagnostic path.
    animation_frame_hashes = {}
    for anim in animations:
        for frame_hash in anim.get("frame_resource_ids", []):
            if frame_hash in texture_by_hash:
                animation_frame_hashes[frame_hash] = texture_by_hash[frame_hash]

    mapping = []
    unresolved = []

    for slot in range(mesh["material_count"]):
        if slot >= len(material_records):
            mapping.append({
                "material_id": slot,
                "material_record_index": None,
                "texture_hash": None,
                "texture": None,
                "source_texture_name": None,
                "resolution": "missing_material_record",
            })
            unresolved.append(slot)
            continue

        material = material_records[slot]
        texture_hash = material["texture_name_hash"]
        tex = texture_by_hash.get(texture_hash)

        resolution = "exact_name_hash" if tex else "unresolved_hash"

        mapping.append({
            "material_id": slot,
            "material_record_index": material["index"],
            "material_record_offset": material["offset"],
            "texture_hash": texture_hash,
            "texture_hash_hex": f"0x{texture_hash:X}",
            "texture": tex["png"] if tex else None,
            "source_texture_name": tex["name"] if tex else None,
            "resolution": resolution,
        })

        if not tex:
            unresolved.append(slot)

    status = (
        "exact_game_hash"
        if not unresolved
        else f"exact_hash_with_{len(unresolved)}_unresolved"
    )

    collision_report = {
        f"0x{h:X}": [x["name"] for x in textures]
        for h, textures in collisions.items()
    }

    return status, mapping, unresolved, collision_report


def write_textured_mtl(path: Path, mapping, texture_bank_dir: str):
    with path.open("w", encoding="utf-8", newline="\n") as m:
        m.write("# ToonCar R3D Blender material export v102\n")
        m.write("# Texture assignment uses ToonCar.exe filename hashes from material record +0x38.\n\n")

        for item in mapping:
            mat = item["material_id"]
            m.write(f"newmtl mat_{mat:03d}\n")
            m.write("Ka 0 0 0\n")
            m.write("Kd 1 1 1\n")
            m.write("Ks 0 0 0\n")
            m.write("illum 1\n")
            if item["texture"]:
                # MTL is in meshes/, textures are sibling ../textures/<bank>/
                m.write(
                    f"map_Kd ../textures/{texture_bank_dir}/{item['texture']}\n"
                )
            m.write("\n")


def export_mesh_obj(data: bytes, mesh, path: Path, scale: float, texture_mapping=None, texture_bank_dir=None):
    vc = mesh["vertex_count"]
    fc = mesh["face_count"]
    vs = mesh["vertex_start"]
    fs = mesh["face_start"]

    mtl_path = path.with_suffix(".mtl")
    if texture_mapping is not None and texture_bank_dir:
        write_textured_mtl(mtl_path, texture_mapping, texture_bank_dir)
    else:
        with mtl_path.open("w", encoding="utf-8", newline="\n") as m:
            for mat in range(max(mesh["material_count"], 1)):
                m.write(f"newmtl mat_{mat:03d}\n")
                m.write("Kd 0.8 0.8 0.8\nKs 0 0 0\nillum 1\n\n")

    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# ToonCar R3D Code-Guided Unpacker v102\n")
        f.write(f"# source_offset=0x{mesh['offset']:X}\n")
        f.write(f"mtllib {mtl_path.name}\n")
        f.write(f"o {path.stem}\n")

        # Verified visually: mirror source Z to correct ToonCar handedness while
        # retaining OBJ's Y-up convention. Blender then imports flat and unmirrored.
        for i in range(vc):
            off = vs + i * VERTEX_STRIDE
            x, y, z = struct.unpack_from("<3f", data, off)
            f.write(f"v {x*scale:.9g} {y*scale:.9g} {-z*scale:.9g}\n")

        for i in range(vc):
            off = vs + i * VERTEX_STRIDE + 12
            nx, ny, nz = struct.unpack_from("<3f", data, off)
            vals = [nx, ny, -nz]
            vals = [v if math.isfinite(v) else 0.0 for v in vals]
            f.write(f"vn {vals[0]:.9g} {vals[1]:.9g} {vals[2]:.9g}\n")

        records = []
        vt = 1
        for i in range(fc):
            off = fs + i * FACE_STRIDE
            a, b, c, pad = struct.unpack_from("<4H", data, off)
            u0, v0, u1, v1, u2, v2 = struct.unpack_from("<6f", data, off + 8)
            mat = read_u32(data, off + 32)

            for u, v in ((u0,v0),(u1,v1),(u2,v2)):
                f.write(f"vt {u:.9g} {1.0-v:.9g}\n")

            records.append((a+1,b+1,c+1,vt,vt+1,vt+2,mat))
            vt += 3

        last_mat = None
        for a,b,c,ta,tb,tc,mat in records:
            if mat != last_mat:
                f.write(f"usemtl mat_{mat:03d}\n")
                last_mat = mat
            f.write(f"f {a}/{ta}/{a} {c}/{tc}/{c} {b}/{tb}/{b}\n")


def export_meshes(
    data: bytes,
    meshes,
    root: Path,
    scale: float,
    main_mesh_offset=None,
    main_texture_mapping=None,
    main_texture_bank_dir=None,
    export_raw_data=False,
):
    root.mkdir(parents=True, exist_ok=True)
    out = []

    for i, mesh in enumerate(meshes):
        role = "main" if mesh["offset"] == main_mesh_offset else "asset"
        name = (
            f"{i:03d}_{role}_0x{mesh['offset']:X}_"
            f"{mesh['vertex_count']}v_{mesh['face_count']}f.obj"
        )
        path = root / name
        is_main = mesh["offset"] == main_mesh_offset
        export_mesh_obj(
            data,
            mesh,
            path,
            scale,
            texture_mapping=(main_texture_mapping if is_main else None),
            texture_bank_dir=(main_texture_bank_dir if is_main else None),
        )

        raw_name = name.replace(
            ".obj",
            ".raw.bin",
        )
        raw = data[
            mesh["offset"]:mesh["end_offset"]
        ]
        raw_path = root / raw_name

        if export_raw_data:
            raw_path.write_bytes(raw)
        elif raw_path.exists():
            raw_path.unlink()

        mesh_entry = {
            **mesh,
            "role": role,
            "obj": name,
            "mtl": path.with_suffix(".mtl").name,
            "sha256_raw": sha256(raw),
        }

        if export_raw_data:
            mesh_entry["raw"] = raw_name

        out.append(mesh_entry)

    return out



# ---------------------------------------------------------------------------
# OBJECT / PROP FORMAT
#
# Verified from ToonCar.exe:
#
# 0x449860  Root object loader
#   fread(root, 0x50)
#   +0x20 -> Part (0x449810)
#   +0x24 -> child Part count
#   +0x28 -> child pointers; each existing child is loaded by 0x449810
#
# 0x449810  Part loader
#   fread(part, 0x30)
#   +0x18 -> ObjectMesh loader 0x46FAC0
#   +0x1C -> spatial/collision structure 0x45B530
#   +0x20 -> StaticMesh loader 0x460880
#
# Part bytes +0x24,+0x28,+0x2C are a local translation. This is visible in
# Venus child parts and is needed to reconstruct compound/destructible props.
#
# 0x46FAC0 ObjectMesh:
#   header 0x50
#   group_count  at +0x14
#   vertex_count at +0x18
#   face_count   at +0x24
#   extra_count  at +0x2C
#   groups  group_count  * 0x1C
#   verts   vertex_count * 0x30
#   faces   face_count   * 0x06
#   extra   extra_count  * 0x08
#
# Group 0x1C:
#   +00 global material id
#   +04 vertex count
#   +08 vertex start
#   +0C face count
#   +10 face start
#
# Faces are 3 uint16 indices LOCAL to their group's vertex range.
#
# After the serialized Root definitions the track contains:
#   uint32 instance_count
#   instance_count * 0x44
#
# 0x44 Instance:
#   16 float Direct3D row-vector transform matrix
#   uint32 root/model index at +0x40
#
# On Venus this resolves exactly to 4 root definitions and 20 instances.
# ---------------------------------------------------------------------------

OBJECT_MESH_HEADER_SIZE = 0x50
OBJECT_GROUP_SIZE = 0x1C
OBJECT_VERTEX_STRIDE = 0x30
OBJECT_FACE_STRIDE = 0x06
OBJECT_EXTRA_STRIDE = 0x08

PROP_ROOT_SIZE = 0x50
PROP_PART_SIZE = 0x30
PROP_INSTANCE_SIZE = 0x44
SPATIAL_HEADER_SIZE = 0x68
SPATIAL_RECORD_SIZE = 0x5C


def try_object_mesh(data: bytes, offset: int, full_validate=True):
    if offset < 0 or offset + OBJECT_MESH_HEADER_SIZE > len(data):
        return None

    h = struct.unpack_from("<20I", data, offset)
    group_count = h[0x14 // 4]
    vertex_count = h[0x18 // 4]
    face_count = h[0x24 // 4]
    extra_count = h[0x2C // 4]

    if not (
        1 <= group_count <= 256
        and 3 <= vertex_count <= 200000
        and 1 <= face_count <= 400000
        and 0 <= extra_count <= 200000
    ):
        return None

    group_start = offset + OBJECT_MESH_HEADER_SIZE
    vertex_start = group_start + group_count * OBJECT_GROUP_SIZE
    face_start = vertex_start + vertex_count * OBJECT_VERTEX_STRIDE
    extra_start = face_start + face_count * OBJECT_FACE_STRIDE
    end = extra_start + extra_count * OBJECT_EXTRA_STRIDE

    if end > len(data):
        return None

    groups = []
    total_vertices = 0
    total_faces = 0

    for i in range(group_count):
        goff = group_start + i * OBJECT_GROUP_SIZE
        material_id, vcount, vstart, fcount, fstart, unk0, unk1 = \
            struct.unpack_from("<7I", data, goff)

        if material_id > 8192:
            return None
        if vstart > vertex_count or vcount > vertex_count:
            return None
        if vstart + vcount > vertex_count:
            return None
        if fstart > face_count or fcount > face_count:
            return None
        if fstart + fcount > face_count:
            return None

        total_vertices += vcount
        total_faces += fcount

        groups.append({
            "index": i,
            "offset": goff,
            "material_id": material_id,
            "vertex_count": vcount,
            "vertex_start": vstart,
            "face_count": fcount,
            "face_start": fstart,
            "unknown_0x14": unk0,
            "unknown_0x18": unk1,
        })

    # Known object meshes partition both arrays exactly.
    if total_vertices != vertex_count or total_faces != face_count:
        return None

    sample_vertices = {0, vertex_count // 2, vertex_count - 1}
    for i in sample_vertices:
        voff = vertex_start + i * OBJECT_VERTEX_STRIDE
        pos = struct.unpack_from("<3f", data, voff)
        normal = struct.unpack_from("<3f", data, voff + 0x0C)
        uv = struct.unpack_from("<2f", data, voff + 0x28)

        if not all(math.isfinite(x) and abs(x) < 1e8 for x in pos):
            return None
        if not all(math.isfinite(x) and abs(x) < 1e6 for x in normal):
            return None
        if not all(math.isfinite(x) and abs(x) < 1e6 for x in uv):
            return None

    # Validate group-local face indices.
    groups_to_check = groups if full_validate else groups[:min(4, len(groups))]
    for group in groups_to_check:
        indices = range(
            group["face_start"],
            group["face_start"] + group["face_count"],
        )
        if not full_validate and group["face_count"] > 100:
            indices = list(range(group["face_start"], group["face_start"] + 50))

        for fi in indices:
            a, b, c = struct.unpack_from(
                "<3H",
                data,
                face_start + fi * OBJECT_FACE_STRIDE,
            )
            if max(a, b, c) >= group["vertex_count"]:
                return None

    return {
        "offset": offset,
        "end_offset": end,
        "size": end - offset,
        "group_count": group_count,
        "vertex_count": vertex_count,
        "face_count": face_count,
        "extra_count": extra_count,
        "group_start": group_start,
        "vertex_start": vertex_start,
        "face_start": face_start,
        "extra_start": extra_start,
        "groups": groups,
        "header_dwords": list(h),
    }


def find_all_object_meshes(data: bytes):
    """
    Scan byte-by-byte because Luna proves these structures need not be
    DWORD-aligned.
    """
    found = []
    limit = len(data) - OBJECT_MESH_HEADER_SIZE

    for offset in range(max(0, limit)):
        # Cheap prefilter before the expensive parser.
        group_count = read_u32(data, offset + 0x14)
        if not (1 <= group_count <= 64):
            continue

        vertex_count = read_u32(data, offset + 0x18)
        if not (3 <= vertex_count <= 50000):
            continue

        face_count = read_u32(data, offset + 0x24)
        if not (1 <= face_count <= 100000):
            continue

        mesh = try_object_mesh(data, offset, full_validate=False)
        if mesh:
            mesh = try_object_mesh(data, offset, full_validate=True)
            if mesh:
                found.append(mesh)

    # Exact starts should already be unique, but keep this conservative.
    unique = []
    seen = set()
    for mesh in found:
        key = (mesh["offset"], mesh["end_offset"])
        if key not in seen:
            seen.add(key)
            unique.append(mesh)

    return unique


def try_spatial_block(data: bytes, offset: int):
    """
    Loader 0x45B530:
      read 0x68
      count at +0x30
      read count * 0x5C
      optional object at +0x38

    The optional nested object is not decoded yet. A block with that pointer
    set is therefore reported unsupported rather than silently misparsed.
    """
    if offset < 0 or offset + SPATIAL_HEADER_SIZE > len(data):
        return None

    h = struct.unpack_from("<26I", data, offset)
    count = h[0x30 // 4]
    nested_ptr = h[0x38 // 4]

    if count > 100000:
        return None

    end = offset + SPATIAL_HEADER_SIZE + count * SPATIAL_RECORD_SIZE
    if end > len(data):
        return None

    if nested_ptr:
        return {
            "offset": offset,
            "end_offset": end,
            "count": count,
            "nested_pointer_present": True,
            "supported": False,
            "header_dwords": list(h),
        }

    return {
        "offset": offset,
        "end_offset": end,
        "count": count,
        "nested_pointer_present": False,
        "supported": True,
        "header_dwords": list(h),
    }


def try_prop_part(data: bytes, offset: int):
    if offset < 0 or offset + PROP_PART_SIZE > len(data):
        return None

    h = struct.unpack_from("<12I", data, offset)
    bbox = struct.unpack_from("<6f", data, offset)

    if not all(math.isfinite(x) and abs(x) < 1e8 for x in bbox):
        return None

    object_mesh_ptr = h[0x18 // 4]
    spatial_ptr = h[0x1C // 4]
    static_mesh_ptr = h[0x20 // 4]
    local_translation = struct.unpack_from("<3f", data, offset + 0x24)

    if not all(math.isfinite(x) and abs(x) < 1e8 for x in local_translation):
        return None

    cur = offset + PROP_PART_SIZE
    object_mesh = None
    spatial = None
    static_mesh = None

    if object_mesh_ptr:
        object_mesh = try_object_mesh(data, cur, full_validate=True)
        if not object_mesh:
            return None
        cur = object_mesh["end_offset"]

    if spatial_ptr:
        spatial = try_spatial_block(data, cur)
        if not spatial or not spatial["supported"]:
            return None
        cur = spatial["end_offset"]

    if static_mesh_ptr:
        static_mesh = try_mesh(data, cur, full_validate=True)
        if not static_mesh:
            return None
        cur = static_mesh["end_offset"]

    return {
        "offset": offset,
        "end_offset": cur,
        "bbox": list(bbox),
        "local_translation": list(local_translation),
        "object_mesh_pointer_saved": object_mesh_ptr,
        "spatial_pointer_saved": spatial_ptr,
        "static_mesh_pointer_saved": static_mesh_ptr,
        "object_mesh": object_mesh,
        "spatial": spatial,
        "static_mesh": static_mesh,
        "header_dwords": list(h),
    }


def try_prop_root(data: bytes, offset: int):
    if offset < 0 or offset + PROP_ROOT_SIZE > len(data):
        return None

    h = struct.unpack_from("<20I", data, offset)
    bbox = struct.unpack_from("<6f", data, offset)

    if not all(math.isfinite(x) and abs(x) < 1e8 for x in bbox):
        return None
    if not all(bbox[i] <= bbox[i + 3] for i in range(3)):
        return None

    unknown_18 = h[0x18 // 4]
    unknown_1c = h[0x1C // 4]
    parent_part_ptr = h[0x20 // 4]
    child_count = h[0x24 // 4]

    if child_count > 10:
        return None

    # Loaders 0x4733D0 / 0x4527E0 are not yet decoded. Do not fake offsets.
    if unknown_18 or unknown_1c:
        return None

    cur = offset + PROP_ROOT_SIZE

    parent_part = None
    if parent_part_ptr:
        parent_part = try_prop_part(data, cur)
        if not parent_part:
            return None
        cur = parent_part["end_offset"]

    children = []
    for i in range(child_count):
        saved_ptr = h[(0x28 // 4) + i]
        if not saved_ptr:
            return None

        child = try_prop_part(data, cur)
        if not child:
            return None
        child["child_index"] = i
        child["saved_pointer"] = saved_ptr
        children.append(child)
        cur = child["end_offset"]

    if not parent_part and not children:
        return None

    return {
        "offset": offset,
        "end_offset": cur,
        "bbox": list(bbox),
        "parent_part_pointer_saved": parent_part_ptr,
        "child_count": child_count,
        "parent_part": parent_part,
        "children": children,
        "header_dwords": list(h),
    }


def parse_instance_record(data: bytes, offset: int, root_count: int):
    if offset + PROP_INSTANCE_SIZE > len(data):
        return None

    matrix = list(struct.unpack_from("<16f", data, offset))
    root_index = read_u32(data, offset + 0x40)

    if root_index >= root_count:
        return None

    if not all(math.isfinite(x) and abs(x) < 1e10 for x in matrix):
        return None

    # Direct3D affine row-vector matrix. Translation is _41/_42/_43.
    # Known track records have zeros in _14/_24/_34 and one in _44.
    eps = 1e-3
    if (
        abs(matrix[3]) > eps
        or abs(matrix[7]) > eps
        or abs(matrix[11]) > eps
        or abs(matrix[15] - 1.0) > 0.01
    ):
        return None

    return {
        "offset": offset,
        "root_index": root_index,
        "matrix_row_major": matrix,
        "translation": [matrix[12], matrix[13], matrix[14]],
    }


def try_prop_scene_table(data: bytes, offset: int):
    """
    Candidate:
      uint32 root_count
      Root[root_count] recursively serialized
      uint32 instance_count
      Instance[instance_count] (0x44)
    """
    if offset < 0 or offset + 4 > len(data):
        return None

    root_count = read_u32(data, offset)
    if not (1 <= root_count <= 256):
        return None

    cur = offset + 4
    roots = []

    for i in range(root_count):
        root = try_prop_root(data, cur)
        if not root:
            return None
        root["index"] = i
        roots.append(root)
        cur = root["end_offset"]

    if cur + 4 > len(data):
        return None

    instance_count = read_u32(data, cur)
    if not (1 <= instance_count <= 100000):
        return None

    instance_count_offset = cur
    cur += 4

    instances = []
    for i in range(instance_count):
        record = parse_instance_record(data, cur, root_count)
        if not record:
            return None
        record["index"] = i
        instances.append(record)
        cur += PROP_INSTANCE_SIZE

    return {
        "offset": offset,
        "end_offset": cur,
        "root_count": root_count,
        "roots": roots,
        "instance_count_offset": instance_count_offset,
        "instance_count": instance_count,
        "instances": instances,
    }


def find_prop_scene_table_strict(data: bytes, object_meshes=None):
    """
    Prefer candidates close to known ObjectMesh data and require at least one
    parsed root to actually contain ObjectMesh geometry.
    """
    candidates = []

    # Fast candidate set from every small DWORD. Track serialization is usually
    # DWORD aligned, but include byte positions near known object meshes too.
    for offset in range(0, len(data) - 4, 4):
        count = read_u32(data, offset)
        if 1 <= count <= 64:
            candidates.append(offset)

    if object_meshes:
        for mesh in object_meshes:
            # Common simple root layout:
            # count + root(0x50) + part(0x30) + ObjectMesh
            approx = mesh["offset"] - 4 - PROP_ROOT_SIZE - PROP_PART_SIZE
            for delta in range(-256, 257):
                if 0 <= approx + delta < len(data) - 4:
                    candidates.append(approx + delta)

    best = None
    seen = set()

    for offset in candidates:
        if offset in seen:
            continue
        seen.add(offset)

        scene = try_prop_scene_table(data, offset)
        if not scene:
            continue

        object_mesh_count = 0
        for root in scene["roots"]:
            parts = []
            if root["parent_part"]:
                parts.append(root["parent_part"])
            parts.extend(root["children"])
            object_mesh_count += sum(1 for p in parts if p["object_mesh"])

        if object_mesh_count == 0:
            continue

        scene["object_mesh_count"] = object_mesh_count

        # Rank larger, richer tables higher.
        score = (
            scene["instance_count"] * 100
            + scene["root_count"] * 10
            + object_mesh_count
        )
        scene["score"] = score

        if best is None or score > best["score"]:
            best = scene

    return best



def try_flexible_root_header(data: bytes, offset: int):
    """
    Recognize the serialized 0x50 Root header without trying to decode the
    optional +0x18/+0x1C auxiliary structures.

    This is useful for tracks such as Luna: the game loads those auxiliary
    structures first, but the actual renderable parent/child Part records are
    still serialized at the END of each Root. We can therefore recover the
    visual hierarchy from Root boundaries + Part chains without guessing the
    size of unknown collision/animation data.
    """
    if offset < 0 or offset + PROP_ROOT_SIZE > len(data):
        return None

    h = struct.unpack_from("<20I", data, offset)
    bbox = struct.unpack_from("<6f", data, offset)

    if not all(math.isfinite(x) and abs(x) < 1e8 for x in bbox):
        return None

    if not all(bbox[i] <= bbox[i + 3] for i in range(3)):
        return None

    extents = [bbox[i + 3] - bbox[i] for i in range(3)]
    if max(extents) < 1e-4:
        return None

    unknown_18 = h[0x18 // 4]
    unknown_1c = h[0x1C // 4]
    parent_part_ptr = h[0x20 // 4]
    child_count = h[0x24 // 4]

    if not (0 <= child_count <= 10):
        return None

    if not parent_part_ptr and child_count == 0:
        return None

    # Children are stored in the fixed pointer slots beginning at +0x28.
    if 10 + child_count > len(h):
        return None

    child_ptrs = [h[10 + i] for i in range(child_count)]
    if any(ptr == 0 for ptr in child_ptrs):
        return None

    # In known Root headers all unused child pointer slots are zero.
    if any(h[i] != 0 for i in range(10 + child_count, len(h))):
        return None

    # Saved pointers come from the original 32-bit process image/heap. This
    # rejects common false positives made from float constants and tiny counts.
    pointer_values = [
        unknown_18,
        unknown_1c,
        parent_part_ptr,
        *child_ptrs,
    ]
    if any(ptr and ptr < 0x10000 for ptr in pointer_values):
        return None

    return {
        "offset": offset,
        "bbox": list(bbox),
        "header_dwords": list(h),
        "unknown_18_pointer_saved": unknown_18,
        "unknown_1c_pointer_saved": unknown_1c,
        "parent_part_pointer_saved": parent_part_ptr,
        "child_count": child_count,
        "child_pointers_saved": child_ptrs,
    }


def try_instance_table_without_root_count(data: bytes, offset: int):
    """
    Parse:
        uint32 instance_count
        instance_count * 0x44

    Unlike parse_instance_record(), the Root count is not known yet. Root
    indices are only constrained to a conservative byte-sized-ish range here;
    the actual Root count is recovered afterwards from the scene header.
    """
    if offset < 0 or offset + 4 > len(data):
        return None

    instance_count = read_u32(data, offset)
    if not (1 <= instance_count <= 100000):
        return None

    end = offset + 4 + instance_count * PROP_INSTANCE_SIZE
    if end > len(data):
        return None

    instances = []
    max_root_index = -1
    cur = offset + 4

    for index in range(instance_count):
        matrix = list(struct.unpack_from("<16f", data, cur))
        root_index = read_u32(data, cur + 0x40)

        if root_index > 255:
            return None

        if not all(math.isfinite(x) and abs(x) < 1e10 for x in matrix):
            return None

        eps = 1e-3
        if (
            abs(matrix[3]) > eps
            or abs(matrix[7]) > eps
            or abs(matrix[11]) > eps
            or abs(matrix[15] - 1.0) > 0.01
        ):
            return None

        instances.append({
            "offset": cur,
            "index": index,
            "root_index": root_index,
            "matrix_row_major": matrix,
            "translation": [
                matrix[12],
                matrix[13],
                matrix[14],
            ],
        })

        max_root_index = max(max_root_index, root_index)
        cur += PROP_INSTANCE_SIZE

    return {
        "offset": offset,
        "end_offset": cur,
        "instance_count_offset": offset,
        "instance_count": instance_count,
        "instances": instances,
        "max_root_index": max_root_index,
    }


def collect_renderable_part_candidates(data: bytes, object_meshes):
    """
    A renderable Part with ObjectMesh always serializes its 0x30-byte Part
    header immediately before that ObjectMesh, because ObjectMesh is the first
    optional payload loaded by 0x449810.

    This gives a very strong, non-hardcoded candidate set. try_prop_part then
    validates the complete Part, including optional spatial and StaticMesh data.
    """
    parts = {}

    for object_mesh in object_meshes or []:
        part_offset = object_mesh["offset"] - PROP_PART_SIZE
        if part_offset < 0:
            continue

        part = try_prop_part(data, part_offset)
        if not part:
            continue

        parsed_object_mesh = part.get("object_mesh")
        if not parsed_object_mesh:
            continue

        if parsed_object_mesh["offset"] != object_mesh["offset"]:
            continue

        parts[part_offset] = part

    return parts


def find_part_chain_ending_at(parts_by_end, boundary, count, min_start):
    """
    Walk backwards through validated Part records.

    Root loader 0x449860 serializes:
      auxiliary structures
      parent Part (if present)
      child Part 0
      child Part 1
      ...

    Therefore the final Part ends exactly at the next Root header (or at the
    instance-count DWORD for the final Root).
    """
    if count == 0:
        return [] if boundary >= min_start else None

    def walk(end_offset, remaining):
        if remaining == 0:
            return []

        for part in parts_by_end.get(end_offset, []):
            if part["offset"] < min_start:
                continue

            prefix = walk(part["offset"], remaining - 1)
            if prefix is not None:
                return prefix + [part]

        return None

    return walk(boundary, count)


def find_prop_scene_table_flexible(data: bytes, object_meshes=None):
    """
    Generic visual Root/Part reconstruction.

    This intentionally does NOT decode Root +0x18/+0x1C auxiliary payloads.
    Instead it derives Root boundaries from:
      - the real 0x50 Root headers,
      - the exact renderable Part chains,
      - the exact 0x44 instance table.

    This is sufficient to place visible props and preserves the same parent /
    destroyed-child semantics used by the simple Venus parser.
    """
    object_meshes = object_meshes or []
    if not object_meshes:
        return None

    track_meshes = find_all_meshes(data)
    parts = collect_renderable_part_candidates(data, object_meshes)
    if not parts:
        return None

    parts_by_end = {}
    for part in parts.values():
        parts_by_end.setdefault(part["end_offset"], []).append(part)

    # In known tracks the instance-count DWORD begins exactly where the last
    # serialized Root payload ends. Mesh end offsets are therefore excellent
    # candidates. Check a few neighboring bytes as well because Luna proves
    # the scene block itself may be unaligned.
    instance_offsets = set()

    for mesh in list(track_meshes) + list(object_meshes):
        for delta in range(-4, 5):
            candidate = mesh["end_offset"] + delta
            if 0 <= candidate <= len(data) - 4:
                instance_offsets.add(candidate)

    instance_tables = []
    seen_tables = set()

    for offset in instance_offsets:
        table = try_instance_table_without_root_count(data, offset)
        if not table:
            continue

        key = (
            table["offset"],
            table["end_offset"],
            table["instance_count"],
        )
        if key in seen_tables:
            continue

        seen_tables.add(key)
        instance_tables.append(table)

    if not instance_tables:
        return None

    # Richer tables are much less likely to be accidental matrix sequences.
    instance_tables.sort(
        key=lambda item: item["instance_count"],
        reverse=True,
    )

    earliest_object_mesh = min(m["offset"] for m in object_meshes)

    for table in instance_tables:
        # Root definitions normally begin shortly before the first prop mesh.
        # Use a generous 64 KiB look-behind; all auxiliary payloads themselves
        # are after the Root header, so this is intentionally conservative.
        scan_start = max(4, earliest_object_mesh - 0x10000)
        scan_end = table["offset"] - PROP_ROOT_SIZE

        if scan_end <= scan_start:
            continue

        root_candidates = []

        # Serialized structures keep a consistent byte alignment. We still scan
        # byte-wise because Luna is +2 mod 4 rather than DWORD-aligned.
        for offset in range(scan_start, scan_end + 1):
            # Very cheap prefilter: child count is at +0x24.
            child_count = read_u32(data, offset + 0x24)
            if child_count > 10:
                continue

            root = try_flexible_root_header(data, offset)
            if root:
                root_candidates.append(root)

        if not root_candidates:
            continue

        # Find a Root whose immediately preceding DWORD is root_count. The
        # instance table gives a lower bound through max(root_index).
        starters = []

        for root in root_candidates:
            if root["offset"] < 4:
                continue

            root_count = read_u32(data, root["offset"] - 4)

            if not (
                table["max_root_index"] + 1
                <= root_count
                <= 64
            ):
                continue

            starters.append((root, root_count))

        if not starters:
            continue

        for first_root, root_count in starters:
            candidates = sorted(
                (
                    root
                    for root in root_candidates
                    if first_root["offset"]
                    <= root["offset"]
                    < table["offset"]
                ),
                key=lambda root: root["offset"],
            )

            def required_part_count(root):
                return (
                    (1 if root["parent_part_pointer_saved"] else 0)
                    + root["child_count"]
                )

            def solve(current_root, completed):
                # completed contains roots whose Part chain has already been
                # resolved. When root_count-1 are complete, current_root is the
                # final Root and must end at the instance table.
                if len(completed) == root_count - 1:
                    chain = find_part_chain_ending_at(
                        parts_by_end,
                        table["offset"],
                        required_part_count(current_root),
                        current_root["offset"] + PROP_ROOT_SIZE,
                    )

                    if chain is not None:
                        return completed + [(current_root, chain)]

                    return None

                current_required = required_part_count(current_root)

                for next_root in candidates:
                    if next_root["offset"] <= current_root["offset"]:
                        continue

                    chain = find_part_chain_ending_at(
                        parts_by_end,
                        next_root["offset"],
                        current_required,
                        current_root["offset"] + PROP_ROOT_SIZE,
                    )

                    if chain is None:
                        continue

                    result = solve(
                        next_root,
                        completed + [(current_root, chain)],
                    )
                    if result is not None:
                        return result

                return None

            solved = solve(first_root, [])
            if solved is None:
                continue

            roots = []

            for root_index, (root_header, chain) in enumerate(solved):
                parent_part = None
                children = []

                chain_index = 0

                if root_header["parent_part_pointer_saved"]:
                    parent_part = chain[0]
                    chain_index = 1

                for child_index, part in enumerate(chain[chain_index:]):
                    child = dict(part)
                    child["child_index"] = child_index
                    child["saved_pointer"] = (
                        root_header["child_pointers_saved"][child_index]
                    )
                    children.append(child)

                root_end = (
                    solved[root_index + 1][0]["offset"]
                    if root_index + 1 < len(solved)
                    else table["offset"]
                )

                roots.append({
                    "index": root_index,
                    "offset": root_header["offset"],
                    "end_offset": root_end,
                    "bbox": root_header["bbox"],
                    "parent_part_pointer_saved": (
                        root_header["parent_part_pointer_saved"]
                    ),
                    "child_count": root_header["child_count"],
                    "parent_part": parent_part,
                    "children": children,
                    "header_dwords": root_header["header_dwords"],
                    "auxiliary_0x18_pointer_saved": (
                        root_header["unknown_18_pointer_saved"]
                    ),
                    "auxiliary_0x1c_pointer_saved": (
                        root_header["unknown_1c_pointer_saved"]
                    ),
                    "auxiliary_payload_decoded": False,
                    "visual_parts_recovered_by_boundary_chaining": True,
                })

            # Revalidate every instance against the recovered exact root_count.
            if any(
                instance["root_index"] >= root_count
                for instance in table["instances"]
            ):
                continue

            object_mesh_count = 0
            for root in roots:
                for _, part in iter_root_parts(root):
                    if part.get("object_mesh"):
                        object_mesh_count += 1

            return {
                "offset": first_root["offset"] - 4,
                "end_offset": table["end_offset"],
                "root_count": root_count,
                "roots": roots,
                "instance_count_offset": table["offset"],
                "instance_count": table["instance_count"],
                "instances": table["instances"],
                "object_mesh_count": object_mesh_count,
                "score": (
                    table["instance_count"] * 100
                    + root_count * 10
                    + object_mesh_count
                ),
                "parser_mode": "flexible_root_boundary_chaining",
                "auxiliary_root_payloads_decoded": False,
            }

    return None


def find_prop_scene_table(data: bytes, object_meshes=None):
    """
    Unified prop-scene parser.

    1. Try the fully decoded simple layout first.
    2. If optional Root payloads prevent exact cursor walking, reconstruct the
       renderable Root/Part hierarchy from verified boundaries instead.

    No map names and no track-specific offsets are used.
    """
    strict = find_prop_scene_table_strict(
        data,
        object_meshes,
    )

    if strict:
        strict["parser_mode"] = "strict"
        strict["auxiliary_root_payloads_decoded"] = True
        return strict

    return find_prop_scene_table_flexible(
        data,
        object_meshes,
    )



def transform_point_row_matrix(point, matrix):
    """
    Direct3D-style row-vector transform:
      [x y z 1] * M

    File matrices store translation in elements 12,13,14.
    """
    x, y, z = point
    return (
        x * matrix[0] + y * matrix[4] + z * matrix[8]  + matrix[12],
        x * matrix[1] + y * matrix[5] + z * matrix[9]  + matrix[13],
        x * matrix[2] + y * matrix[6] + z * matrix[10] + matrix[14],
    )


def transform_normal_row_matrix(normal, matrix):
    x, y, z = normal
    nx = x * matrix[0] + y * matrix[4] + z * matrix[8]
    ny = x * matrix[1] + y * matrix[5] + z * matrix[9]
    nz = x * matrix[2] + y * matrix[6] + z * matrix[10]

    length = math.sqrt(nx*nx + ny*ny + nz*nz)
    if length > 1e-12:
        nx /= length
        ny /= length
        nz /= length

    return nx, ny, nz


def resolve_material_texture(material_id, material_records, texture_hash_index):
    if not (0 <= material_id < len(material_records)):
        return None

    h = material_records[material_id]["texture_name_hash"]
    return texture_hash_index.get(h)


def write_props_mtl(
    path: Path,
    material_ids,
    material_records,
    primary_bank_manifest,
):
    texture_index, collisions = build_texture_hash_index(primary_bank_manifest)

    resolved = {}
    with path.open("w", encoding="utf-8", newline="\n") as m:
        m.write("# ToonCar placed props materials v102\n")
        m.write("# Exact game hash: material +0x38 -> texture filename hash\n\n")

        for material_id in sorted(material_ids):
            tex = resolve_material_texture(
                material_id,
                material_records,
                texture_index,
            )

            m.write(f"newmtl mat_{material_id:03d}\n")
            m.write("Ka 0 0 0\n")
            m.write("Kd 1 1 1\n")
            m.write("Ks 0 0 0\n")
            m.write("illum 1\n")

            if tex:
                m.write(
                    f"map_Kd ../textures/{primary_bank_manifest['directory']}/"
                    f"{tex['png']}\n"
                )
                resolved[material_id] = tex["name"]
            else:
                resolved[material_id] = None

            m.write("\n")

    return resolved


def iter_root_parts(root):
    if root.get("parent_part"):
        yield "parent", root["parent_part"]

    for child in root.get("children", []):
        yield f"child_{child.get('child_index', 0):02d}", child


def export_placed_props_obj(
    data: bytes,
    scene,
    root: Path,
    scale: float,
    material_records,
    primary_bank_manifest,
):
    """
    Bake local child translations + exact game instance matrices into vertices.
    This avoids any ambiguity in Blender matrix-axis conversion.
    """
    root.mkdir(parents=True, exist_ok=True)

    obj_path = root / "scene_props.obj"
    mtl_path = root / "scene_props.mtl"

    used_material_ids = set()
    for prop_root in scene["roots"]:
        for _, part in iter_root_parts(prop_root):
            om = part.get("object_mesh")
            if om:
                used_material_ids.update(
                    g["material_id"] for g in om["groups"]
                )

    material_resolution = write_props_mtl(
        mtl_path,
        used_material_ids,
        material_records,
        primary_bank_manifest,
    )

    vbase = 0
    vtbase = 0
    vnbase = 0
    exported_parts = 0

    with obj_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# ToonCar R3D placed prop scene v102\n")
        f.write("# Prop positions are baked from the game's 0x44 instance records.\n")
        f.write(f"mtllib {mtl_path.name}\n\n")

        for instance in scene["instances"]:
            prop_root = scene["roots"][instance["root_index"]]
            matrix = instance["matrix_row_major"]

            for part_name, part in iter_root_parts(prop_root):
                om = part.get("object_mesh")
                if not om:
                    continue

                local_tx, local_ty, local_tz = part["local_translation"]

                f.write(
                    f"o prop_{instance['index']:03d}_"
                    f"root_{instance['root_index']:02d}_{part_name}\n"
                )

                # Write all mesh vertices once for this placed part.
                for vi in range(om["vertex_count"]):
                    voff = om["vertex_start"] + vi * OBJECT_VERTEX_STRIDE

                    x, y, z = struct.unpack_from("<3f", data, voff)
                    nx, ny, nz = struct.unpack_from("<3f", data, voff + 0x0C)
                    u, v = struct.unpack_from("<2f", data, voff + 0x28)

                    local_point = (
                        x + local_tx,
                        y + local_ty,
                        z + local_tz,
                    )
                    wx, wy, wz = transform_point_row_matrix(
                        local_point,
                        matrix,
                    )
                    wnx, wny, wnz = transform_normal_row_matrix(
                        (nx, ny, nz),
                        matrix,
                    )

                    # Same source -> OBJ mapping as the verified static track:
                    # x, y, -z. Blender's OBJ importer then performs its usual
                    # Y-up -> Z-up conversion.
                    f.write(
                        f"v {wx*scale:.9g} {wy*scale:.9g} {-wz*scale:.9g}\n"
                    )
                    f.write(f"vt {u:.9g} {1.0-v:.9g}\n")
                    f.write(
                        f"vn {wnx:.9g} {wny:.9g} {-wnz:.9g}\n"
                    )

                for group in om["groups"]:
                    f.write(f"usemtl mat_{group['material_id']:03d}\n")

                    for fi in range(
                        group["face_start"],
                        group["face_start"] + group["face_count"],
                    ):
                        a, b, c = struct.unpack_from(
                            "<3H",
                            data,
                            om["face_start"] + fi * OBJECT_FACE_STRIDE,
                        )

                        # Face indices are local to the group's vertex subrange.
                        a = vbase + group["vertex_start"] + a + 1
                        b = vbase + group["vertex_start"] + b + 1
                        c = vbase + group["vertex_start"] + c + 1

                        ta = vtbase + group["vertex_start"] + (
                            struct.unpack_from(
                                "<H",
                                data,
                                om["face_start"] + fi * OBJECT_FACE_STRIDE,
                            )[0]
                        ) + 1
                        # Since vertex/UV/normal arrays are 1:1, the indices are
                        # identical after applying the same base.
                        tb = b
                        tc = c
                        na = a
                        nb = b
                        nc = c

                        # Mirror-Z export requires the same winding correction
                        # used for the static track mesh.
                        f.write(
                            f"f {a}/{a}/{a} {c}/{c}/{c} {b}/{b}/{b}\n"
                        )

                vbase += om["vertex_count"]
                vtbase += om["vertex_count"]
                vnbase += om["vertex_count"]
                exported_parts += 1
                f.write("\n")

    scene_json = {
        "root_count": scene["root_count"],
        "instance_count": scene["instance_count"],
        "table_offset": scene["offset"],
        "instance_count_offset": scene["instance_count_offset"],
        "exported_part_instances": exported_parts,
        "material_resolution": material_resolution,
        "roots": scene["roots"],
        "instances": scene["instances"],
    }

    # Raw parser dictionaries include no bytes and are JSON-safe.
    (root / "scene_props.json").write_text(
        json.dumps(scene_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "obj": str(Path("props") / obj_path.name),
        "mtl": str(Path("props") / mtl_path.name),
        "json": str(Path("props") / "scene_props.json"),
        "root_count": scene["root_count"],
        "instance_count": scene["instance_count"],
        "exported_part_instances": exported_parts,
        "material_resolution": material_resolution,
        "table_offset": scene["offset"],
        "table_end_offset": scene["end_offset"],
    }




ANIM_NODE_SERIALIZED_SIZE = 0x110
ANIM_KEY_SIZE = 0x28
ANIM_SET_HEADER_SIZE = 0x24
ANIM_MAP_PHASE_STEP = 1.0 / 6.0
ANIM_MAP_FLAGS = 0


def _read_u32_array(data: bytes, offset: int, count: int):
    if count == 0:
        return []
    return list(struct.unpack_from(f"<{count}I", data, offset))


def _json_index(value):
    return -1 if value == 0xFFFFFFFF else int(value)


def parse_animated_vec_list(data: bytes, offset: int, limit: int):
    if offset + 4 > limit:
        return None

    count = read_u32(data, offset)
    if count > 4096:
        return None

    cur = offset + 4
    entries = []

    for index in range(count):
        if cur + 4 > limit:
            return None

        point_count = read_u32(data, cur)
        cur += 4

        if point_count > 200000:
            return None

        byte_count = point_count * 12
        if cur + byte_count > limit:
            return None

        points = []
        for point_index in range(point_count):
            point = struct.unpack_from(
                "<3f",
                data,
                cur + point_index * 12,
            )
            if not all(math.isfinite(v) and abs(v) < 1e9 for v in point):
                return None
            points.append(list(point))

        cur += byte_count
        entries.append({
            "index": index,
            "point_count": point_count,
            "points": points,
        })

    if cur + 4 > limit:
        return None

    mapping_count = read_u32(data, cur)
    cur += 4

    if mapping_count > 4096 or cur + mapping_count * 4 > limit:
        return None

    mapping_raw = _read_u32_array(data, cur, mapping_count)
    cur += mapping_count * 4

    return {
        "offset": offset,
        "end_offset": cur,
        "count": count,
        "entries": entries,
        "mapping_count": mapping_count,
        "mapping_raw": mapping_raw,
        "mapping": [_json_index(v) for v in mapping_raw],
    }


def parse_animated_node_tree(
    data: bytes,
    offset: int,
    limit: int,
    parent_index=None,
    nodes=None,
):
    if nodes is None:
        nodes = []

    if offset + ANIM_NODE_SERIALIZED_SIZE > limit:
        return None

    node_index = read_u32(data, offset + 0x04)
    child_count = read_u32(data, offset + 0xF0)
    flags = read_u32(data, offset + 0xF8)
    optional_0x2c = read_u32(data, offset + 0x2C)

    if node_index > 4096 or child_count > 256:
        return None

    matrix = list(struct.unpack_from("<16f", data, offset + 0x30))
    if not all(math.isfinite(v) and abs(v) < 1e12 for v in matrix):
        return None

    node = {
        "index": int(node_index),
        "parent_index": parent_index,
        "offset": offset,
        "static_matrix_row_major": matrix,
        "child_count": int(child_count),
        "flags": int(flags),
        "saved_mesh_pointer": int(read_u32(data, offset + 0x28)),
        "saved_optional_0x2c_pointer": int(optional_0x2c),
    }
    nodes.append(node)

    cur = offset + ANIM_NODE_SERIALIZED_SIZE

    # 0x475130 serializes an optional fixed 0x4C payload before children.
    # None of the currently tested Luna/Castilla map animations use it, but
    # accepting it makes the tree parser less map-specific.
    if optional_0x2c:
        if cur + 0x4C > limit:
            return None
        node["optional_0x2c_payload_offset"] = cur
        node["optional_0x2c_payload_size"] = 0x4C
        cur += 0x4C

    for _ in range(child_count):
        parsed = parse_animated_node_tree(
            data,
            cur,
            limit,
            parent_index=int(node_index),
            nodes=nodes,
        )
        if parsed is None:
            return None
        cur = parsed

    return cur


def parse_animation_set(data: bytes, offset: int, expected_end: int):
    if offset + ANIM_SET_HEADER_SIZE + 4 > expected_end:
        return None

    header = list(struct.unpack_from("<9I", data, offset))
    track_count = header[0]

    if not (1 <= track_count <= 256):
        return None

    cur = offset + ANIM_SET_HEADER_SIZE
    mapping_count = read_u32(data, cur)
    cur += 4

    if mapping_count > 4096 or cur + mapping_count * 4 > expected_end:
        return None

    mapping_raw = _read_u32_array(data, cur, mapping_count)
    cur += mapping_count * 4

    tracks = []

    for track_index in range(track_count):
        if cur + 4 > expected_end:
            return None

        key_count = read_u32(data, cur)
        track_offset = cur
        cur += 4

        if not (1 <= key_count <= 100000):
            return None

        keys_end = cur + key_count * ANIM_KEY_SIZE
        if keys_end > expected_end:
            return None

        keys = []

        for key_index in range(key_count):
            key_offset = cur + key_index * ANIM_KEY_SIZE
            values = struct.unpack_from("<10f", data, key_offset)

            if not all(math.isfinite(v) and abs(v) < 1e12 for v in values):
                return None

            q = list(values[0:4])
            translation = list(values[4:7])
            scale = list(values[7:10])

            q_len = math.sqrt(sum(v * v for v in q))
            if q_len < 1e-6 or q_len > 10.0:
                return None

            keys.append({
                "index": key_index,
                "offset": key_offset,
                "quaternion_xyzw": q,
                "translation": translation,
                "scale": scale,
            })

        cur = keys_end

        tracks.append({
            "index": track_index,
            "offset": track_offset,
            "key_count": key_count,
            "keys": keys,
        })

    if cur != expected_end:
        return None

    mapping = [_json_index(v) for v in mapping_raw]

    for value in mapping:
        if value >= track_count:
            return None

    return {
        "offset": offset,
        "end_offset": cur,
        "header_dwords": header,
        "track_count": int(track_count),
        "mapping_count": int(mapping_count),
        "mapping_raw": mapping_raw,
        "mapping": mapping,
        "tracks": tracks,
    }


def parse_animated_model_and_tracks(data: bytes, root_entry):
    root_offset = root_entry["offset"]
    aux_start = root_offset + PROP_ROOT_SIZE

    parts = [part for _, part in iter_root_parts(root_entry)]
    aux_end = (
        min(part["offset"] for part in parts)
        if parts
        else root_entry["end_offset"]
    )

    if aux_start >= aux_end:
        return None

    if not (
        root_entry.get("auxiliary_0x1c_pointer_saved")
        or root_entry.get("auxiliary_0x18_pointer_saved")
    ):
        return None

    cur = aux_start

    # 0x471870 / 0x471920:
    # uint32 mesh_count, sequential ObjectMeshes,
    # uint32 mapping_count, mapping indices.
    if cur + 4 > aux_end:
        return None

    mesh_count = read_u32(data, cur)
    cur += 4

    if not (1 <= mesh_count <= 256):
        return None

    meshes = []

    for mesh_index in range(mesh_count):
        mesh = try_object_mesh(data, cur)
        if not mesh:
            return None

        mesh_info = dict(mesh)
        mesh_info["index"] = mesh_index
        meshes.append(mesh_info)
        cur = mesh["end_offset"]

    if cur + 4 > aux_end:
        return None

    mesh_mapping_count = read_u32(data, cur)
    cur += 4

    if (
        mesh_mapping_count > 4096
        or cur + mesh_mapping_count * 4 > aux_end
    ):
        return None

    mesh_mapping_raw = _read_u32_array(
        data,
        cur,
        mesh_mapping_count,
    )
    cur += mesh_mapping_count * 4
    mesh_mapping = [_json_index(v) for v in mesh_mapping_raw]

    for value in mesh_mapping:
        if value >= mesh_count:
            return None

    # 0x475DE0 / 0x475E90 vector attachment list.
    vec_list = parse_animated_vec_list(data, cur, aux_end)
    if vec_list is None:
        return None
    cur = vec_list["end_offset"]

    # +0x28 optional morph/deformation structure marker.
    if cur + 4 > aux_end:
        return None

    morph_marker = read_u32(data, cur)
    morph_marker_offset = cur
    cur += 4

    # 0x47BAA0 morph payload exists in the engine but is not needed by the
    # Luna/Castilla transform animations decoded here. Do not silently guess
    # its serialized length.
    if morph_marker:
        return {
            "root_index": root_entry["index"],
            "offset": aux_start,
            "end_offset": aux_end,
            "supported": False,
            "unsupported_reason": "morph_payload_0x47BAA0_present",
            "morph_marker_offset": morph_marker_offset,
            "morph_marker_saved_pointer": int(morph_marker),
        }

    # 0x475130 / 0x4751A0 recursive 0x110 node hierarchy.
    nodes = []
    node_tree_offset = cur
    node_end = parse_animated_node_tree(
        data,
        cur,
        aux_end,
        parent_index=None,
        nodes=nodes,
    )

    if node_end is None:
        return None

    cur = node_end

    if not nodes:
        return None

    node_indices = [node["index"] for node in nodes]
    if len(set(node_indices)) != len(node_indices):
        return None

    # Mesh attachment mapping is indexed by node index.
    for node in nodes:
        node_index = node["index"]
        node["mesh_index"] = (
            mesh_mapping[node_index]
            if node_index < len(mesh_mapping)
            else -1
        )

    # Root +0x18 animation set follows the model payload.
    animation_set = None

    if root_entry.get("auxiliary_0x18_pointer_saved"):
        animation_set = parse_animation_set(
            data,
            cur,
            aux_end,
        )
        if animation_set is None:
            return None
        cur = animation_set["end_offset"]
    elif cur != aux_end:
        return None

    if cur != aux_end:
        return None

    if animation_set:
        mapping = animation_set["mapping"]
        for node in nodes:
            node_index = node["index"]
            node["track_index"] = (
                mapping[node_index]
                if node_index < len(mapping)
                else -1
            )
    else:
        for node in nodes:
            node["track_index"] = -1

    return {
        "root_index": root_entry["index"],
        "offset": aux_start,
        "end_offset": aux_end,
        "supported": True,
        "mesh_count": int(mesh_count),
        "meshes": meshes,
        "mesh_mapping_count": int(mesh_mapping_count),
        "mesh_mapping_raw": mesh_mapping_raw,
        "mesh_mapping": mesh_mapping,
        "vector_attachments": vec_list,
        "morph_marker_saved_pointer": int(morph_marker),
        "node_tree_offset": node_tree_offset,
        "node_tree_end_offset": node_end,
        "nodes": nodes,
        "animation_set": animation_set,
        "game_animation": {
            "controller_function": "0x474620",
            "map_initialization": "0x401850 / 0x401993",
            "flags": ANIM_MAP_FLAGS,
            "phase_step_per_update": ANIM_MAP_PHASE_STEP,
            "updates_per_key_interval": 6,
            "loop_mode": "forward_loop",
        },
    }


def decode_animated_prop_definitions(data: bytes, scene):
    if not scene:
        return []

    definitions = []

    for root_entry in scene["roots"]:
        if not (
            root_entry.get("auxiliary_0x1c_pointer_saved")
            or root_entry.get("auxiliary_0x18_pointer_saved")
        ):
            continue

        parsed = parse_animated_model_and_tracks(
            data,
            root_entry,
        )

        if parsed:
            definitions.append(parsed)

    return definitions


def export_animated_props_obj(
    data: bytes,
    scene,
    root: Path,
    scale: float,
    material_records,
    primary_bank_manifest,
):
    definitions = decode_animated_prop_definitions(
        data,
        scene,
    )

    supported = [
        definition
        for definition in definitions
        if definition.get("supported")
    ]

    if not supported:
        return None

    root.mkdir(parents=True, exist_ok=True)

    obj_path = root / "animated_props.obj"
    mtl_path = root / "animated_props.mtl"
    json_path = root / "animated_props.json"

    used_material_ids = set()

    for definition in supported:
        for mesh in definition["meshes"]:
            used_material_ids.update(
                group["material_id"]
                for group in mesh["groups"]
            )

    material_resolution = write_props_mtl(
        mtl_path,
        used_material_ids,
        material_records,
        primary_bank_manifest,
    )

    vbase = 0
    exported_templates = []

    with obj_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# ToonCar decoded animated prop templates v102\n")
        f.write("# Local ObjectMesh geometry; hierarchy/animation is in JSON.\n")
        f.write(f"mtllib {mtl_path.name}\n\n")

        for definition in supported:
            root_index = definition["root_index"]

            for node in definition["nodes"]:
                mesh_index = node.get("mesh_index", -1)
                if not (0 <= mesh_index < len(definition["meshes"])):
                    continue

                mesh = definition["meshes"][mesh_index]
                node_index = node["index"]

                object_name = (
                    f"anim_root_{root_index:02d}_"
                    f"node_{node_index:02d}_mesh_{mesh_index:02d}"
                )

                node["object_name"] = object_name

                f.write(f"o {object_name}\n")

                for vi in range(mesh["vertex_count"]):
                    voff = (
                        mesh["vertex_start"]
                        + vi * OBJECT_VERTEX_STRIDE
                    )

                    x, y, z = struct.unpack_from(
                        "<3f",
                        data,
                        voff,
                    )
                    nx, ny, nz = struct.unpack_from(
                        "<3f",
                        data,
                        voff + 0x0C,
                    )
                    u, v = struct.unpack_from(
                        "<2f",
                        data,
                        voff + 0x28,
                    )

                    # Same verified source -> OBJ conversion as every other
                    # ToonCar mesh. Blender's importer then converts OBJ Y-up
                    # to Blender Z-up.
                    f.write(
                        f"v {x*scale:.9g} "
                        f"{y*scale:.9g} "
                        f"{-z*scale:.9g}\n"
                    )
                    f.write(
                        f"vt {u:.9g} {1.0-v:.9g}\n"
                    )
                    f.write(
                        f"vn {nx:.9g} {ny:.9g} {-nz:.9g}\n"
                    )

                for group in mesh["groups"]:
                    f.write(
                        f"usemtl mat_{group['material_id']:03d}\n"
                    )

                    for fi in range(
                        group["face_start"],
                        group["face_start"]
                        + group["face_count"],
                    ):
                        a, b, c = struct.unpack_from(
                            "<3H",
                            data,
                            mesh["face_start"]
                            + fi * OBJECT_FACE_STRIDE,
                        )

                        a = (
                            vbase
                            + group["vertex_start"]
                            + a
                            + 1
                        )
                        b = (
                            vbase
                            + group["vertex_start"]
                            + b
                            + 1
                        )
                        c = (
                            vbase
                            + group["vertex_start"]
                            + c
                            + 1
                        )

                        f.write(
                            f"f {a}/{a}/{a} "
                            f"{c}/{c}/{c} "
                            f"{b}/{b}/{b}\n"
                        )

                vbase += mesh["vertex_count"]
                f.write("\n")

                exported_templates.append({
                    "root_index": root_index,
                    "node_index": node_index,
                    "mesh_index": mesh_index,
                    "object_name": object_name,
                    "vertex_count": mesh["vertex_count"],
                    "face_count": mesh["face_count"],
                })

    animated_root_indices = {
        definition["root_index"]
        for definition in supported
    }

    instances = [
        instance
        for instance in scene["instances"]
        if instance["root_index"] in animated_root_indices
    ]

    # Trim heavy mesh parser internals from JSON. Blender only needs hierarchy,
    # transforms, keys and template object names.
    json_definitions = []

    for definition in definitions:
        if not definition.get("supported"):
            json_definitions.append(definition)
            continue

        json_definitions.append({
            "root_index": definition["root_index"],
            "offset": definition["offset"],
            "end_offset": definition["end_offset"],
            "supported": True,
            "mesh_count": definition["mesh_count"],
            "mesh_mapping": definition["mesh_mapping"],
            "nodes": definition["nodes"],
            "animation_set": definition["animation_set"],
            "game_animation": definition["game_animation"],
        })

    details = {
        "status": "transform_animation_decoded",
        "definitions": json_definitions,
        "instances": instances,
        "templates": exported_templates,
        "material_resolution": material_resolution,
        "source_functions": {
            "animated_model_save": "0x4527E0",
            "animated_model_load": "0x452840",
            "object_mesh_list_save": "0x471870",
            "object_mesh_list_load": "0x471920",
            "node_save": "0x475130",
            "node_load": "0x4751A0",
            "animation_set_save": "0x4733D0",
            "animation_set_load": "0x4734E0",
            "track_sampler": "0x473020",
            "map_animation_setup": "0x401993 -> 0x474620",
        },
        "key_layout": {
            "size": "0x28",
            "quaternion": "+0x00 XYZW",
            "translation": "+0x10 XYZ",
            "scale": "+0x1C XYZ",
        },
        "phase_step_per_update": ANIM_MAP_PHASE_STEP,
        "updates_per_key_interval": 6,
        "loop_mode": "forward_loop",
    }

    json_path.write_text(
        json.dumps(
            details,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "obj": str(
            Path("animated_props") / obj_path.name
        ),
        "mtl": str(
            Path("animated_props") / mtl_path.name
        ),
        "json": str(
            Path("animated_props") / json_path.name
        ),
        "status": "transform_animation_decoded",
        "definition_count": len(supported),
        "animated_instance_count": len(instances),
        "template_mesh_count": len(exported_templates),
        "unsupported_definition_count": len(definitions) - len(supported),
    }




# ---------------------------------------------------------------------------
# GAMEPLAY TRACK DATA
# Verified from ToonCar.exe:
#
# Script compiler 0x446DB0:
#   Way      -> Vec3 list 0x4B28E8
#   Lap      -> Vec3 list 0x4A7B08
#   Conos    -> Vec3 list 0x4A79D0
#   Sorpresa -> Vec3 array 0x4A7520, count 0x4B235C
#
# Vec3 list serializer 0x475C50 / loader 0x475C80:
#   uint32 count
#   count * float[3]
#
# Track serializer 0x447630 stores:
#   Way, Lap, Conos,
#   camera-group count + Vec3 lists,
#   fixed 0x280-byte block,
#   Sorpresa count + count*Vec3,
#   count + count*0x1C,
#   count + count*0xB4,
#   then Root/Instance prop data.
#
# Runtime 0x403010 loads Way then calls 0x446230. 0x446230 builds
# a CLOSED path using next=(i+1)%count and derives direction, length and
# cumulative distance. Racer/driver constructors attach that processed path
# at 0x416953 and 0x4176A3.
#
# Runtime 0x414880 iterates the Sorpresa Vec3 table and creates one pickup
# object for every XYZ.
# ---------------------------------------------------------------------------

GAMEPLAY_FIXED_BLOCK_SIZE = 0x280

# Built-in original ToonCar Sorpresa.r3d.
EMBEDDED_SORPRESA_R3D_B85 = """c-ri}1(X%n)-^hlD_?Hh2@o_P5F|hnLJ}ZAAdb7cySqEIySux)ySvfdxHQ&{LrAcHu3e{_PVW7Zdz1IZ8*luV!5q6zRh>Hf)TuewT-*970BcvSS+!>V+Swn>S-uMYU;KMYLwnuJ*NLBwXW;SU$N$BD@n8JkhEtJve5nkNf4=zNd+}fV7ysAcfv#-8<DWzWe)@mmGahQ!eRL0xAKcaO^PMMna7WvB|87(H=%*j?;NERK`0*z0-@So*cdp~^t*f|my`E^m?Q8Y8ef0`%^R?iXY!g!6Y`Bb@^_Os?{^CEQ;S!Hu!Y!U7xT$;34S9X_vO1=YbNlu+x&9U0xzT{TH?Q*Zui>ZL*NGdrf9D1s+`WZ|tmEUKSpSEt_oJV5_fx*_qyIiz|Hc2m&`^z&*#W3X^h0$<I8Njz<78P1P8O%*L~$C96{euJFcnp~2?~`t2{@XUgleL;Fby?CbwRqq(fo8&<|d;oGXW*(u_#E1LT*wdauXwvlO%{lKKJFPMx!7t7R7R2b}~wGQc#?&p^*FX(&CYu9E;3^NMyu?BRwt*X>p-CgdvstGUFqVsmgFwrp5ADbO=(Sf)tXaj1J~D5gVf3E7wVn)sPw!id1!eusS~}A^=I@0Z57D`yvB*tzcyEnsRPt0zWGuT$Ne;F1g8(DByP}%7{ftW;{x>5>b|&gwpIph4P$absvuAr{ZWqnz~QNi!ymXGjXyc3#ZGnak@MQXDV}XhTCV$vT&}PWd+MdlxN^fX*$lAXW~L-7A{uhpuQ#_*H4z<_L*|rIa7f<XUlQ-d?oIkufoq4tMRD5mcQq5Jid7vkMCT-;~%N*_v;Dj{G;pqy>Ae=b-1f5Z)kDg5zqVi65n$kkMEw-@H4j`@z~>Qy7O-7@MAOltcxFabjRept9;+(e}?RD!i`^D^G-9|YIdyodm7J^&%OU2#=CpE_uXq2|Nd{m!{0b|1BD@02sBuSAk(dgcGg3pmjx1hZ4vKngE%iMM7x<I+}VsUMU<;KqCL3J(;6|}c1ZAXM7$1BUUmp~w?Txf6+)cM5om9W0DBYoJD4KK!3@F9779VmmORfE;hqkN@^wL^w=<&rToLKxj1YGR1iRY8&(Q+D4(9N(HHL?kA-ru&G}xNK%g$6C_j4c|E#dEE1wSV#Ep@Lg5$IxrAYLcX%@#rK_6X*6gSZ{&Y{PS`;BCk2@Ooa>+V$Pc^x$Tp2X_m7cvu>6pFTg&2tNFLAAX*X9lwX8IRf~-0$2v~JA@FSZZ-&YwM3Y!HNsu^y<N-^=ECpCeG%@wN1iqc@xBhcr;bSScS2Ht3z7qzkrL#F)F2mR1iK=YNDp!6wj0s{osjHrk7PevBzfB+#oJ0Djjyu;?U5hmih^)A<cBz;AjAcQp)M$k@lvRW_eM=>5Kg3p;B;0b&gMkne11GG6ei$GNeb%AQgN+3joV4MRFsGd`LQ^i9fotnu_RwqC-|V2*Eq)VREi(YWrpHnZY0l*Lql0IZj`6vMnwj0R0=W`8p=~~sU!iHiiyHF)a69tbY?J4X9WEM=lL0RO^ConqCPK1L!pKSg;@2Og3E$Dq976tMX_ioiN*D@MBJ`Q$4|A{xPLMq4^9{2!P#Ows$))jp`571<1@v0%ws>+X5n6Krh3oKiWJ;Bn#%X4;pgMIczCJ+kI(VE3w+;YzUOk4uB^c0E5yapf5zo<4Ofp6)zsQ!)XKB;u$O;{`_#|-)Zm}aYPfr%8AP9T+c!nCtA2^=+;``shP#4O8t(iWJa*&wFL9mqxphLT+l4|rK9-EfRWamw-T$fq6^Y*PJ+K09I~O3pa1$cz4<ORf5K(TXh;T4Kn7tl?Yz`BL5MsyIjshcuIvFE^x)9=Mj1UK71lbwF-$oyPR!87#c?3R|hvCiAmt69-(u1#!A^hx&5a3`!4IqbHL<`Ii<YKLO<?m<)U-HbuQXlT-dT=#61SjJI#D2J#9)hdsVeUJu_~}J%dhuKzdsFI!8Q*JxKv!~+niB5gf@psaL<f2zCdgYufG73Em0Dm2Kl0Yg##r%PuIp&D4|aOH;b63%+l1kMIGY@RtI2-2n;j$$!Hwrh>B;Zo!SCg1sRu89Pj72}PnKTh2b<t+afsjj5bwbu_z?lN`n)&1XHF(cKVv+t5KCSs_&Xw%oKFw)Kvskga-w{Z9qEhAa8K$2^?@3YNZ!YJm?PHJ7>RBsgfY^*&5=rE_*f#{*8*97*2wa=LT;cf@`G$q6yc2W7*|v$Q411%a3UoDCsH+>Obf)R%pjKjI7WU~#k!#?+6@)q&M4%23xaJ?5@M%N8R3NLI8W+80M6xvk=yaOT$F&T73sKAMh&4BTr5pN9eIB)KL%&X)zev_>bkWFUZ{$3L1nZvs-m1w6YGlOaUM7t<&2||Iz($Y9`BA53LZF}?5l7_kVd5Y;%xep?YfLWA_$lB!_iP2O+AXob>enK5`H|EPGsPxnl#)aZdWGZdPy9vmL{Tss4r$oZ+oLE1GkUm;O?<p{6r0Sc&1R(f;#f$bdJKq<C(Z$os0)ZlX1T)1rMu<+7vvlO~d08nbejnYDXTy_gyL>?~XpfP0@x*4RY)}&$-ZuVjT)JexA+6<5L;Ggj~0-h<a0^(;aR%lr?*=phUy@VjXy`^Sbj{r-#)U_^~h)^{FoSsW6y4zy81S|5RQq!mW3~d*2fH99n`f^R0+-(dV;jf=E|W#s5%yLxejRA&lG=&PTahA=1Mdk?sO3YJup0DFW?`;cKl=PUykg{4h)E0bl!C8<6{qrR1&nfk5#G<ZXbXnTn@=66>uE6}R2Uec`>M;Q_@*;ia?5A#(LF+<4s6+KBO6=K&P&ZOMBlMEJTQHrR(=K9K%C3>gVg$YCCll|&>)BRwV@31R*`&y8Ndj$Xo4eU`KFAvhT9gM;BdZXbZN(E+%q&pW8}!NugDdhN#7N(XdW;6bi?P$%3?sTIb2uhAa3818{9;YN6x?1QhxVfa%Y0<86vPKMJTM0?pHG0+8RVP43N2|z(Y7>bx5l`y9$WnNU09ED=$7<sXQ)CDi320A0v&mJlCWGVD!$)4tf1yZ@4;bn<T#eW;*1v}9bIH5Sw4W&`!HRE_CQJWk<1fn+4AGImM`v6oY`l2e%3+0inC<%8&VK99~fGzR@tWiJ|^L1Ic6OJVY;B;CjE|B-a_lBd{xXL`FzPbPnHHElRQ-I4=xwy!j<w9v1&J`x&Ol~x_hMz|dag6#=8%xA^pejPNfZD|CYT#=@l&eCmXibuEJ%C)-Ax*<sZl6mJQV{-M$O*@lf@oZ!C%9gkKu?lF4M@SAO5uJ6?jB8JtWQ^57r)a`K@U)qi<_+b%@akqbGi)o&Q{<j=1vcY$IPD|*Husp$hA{><bM`<pNjj`5y3;^F?~c6s2PvX<Y|1XuhjX0N-geQ7u0C{JuA9GoXyuzr{k|^jnbQ34aYO^=#*$rBlk;nzNAX?A_BEtrR^7=BYx)!Kl7q^l`_q@$Y-9;#*f7jxSZmQt7*=7T$A{l`N0MLc1d33xy3dF8?HpC@oI$G?n11K0b<<Er~!=gF2>wu-14+TqQ48{7GqSn4^qQ?krM2Qcz<%w+krlqxddafpDlTA%Xlv_--g_$kM<R9u$B1F<ID^Em>;N;T=piXJtXFnTTbMkz>$1(6h1cNzW8+?dUx^u0nS#6`{L~*7~A85eVO-%AU!?`xv2>#%uGc|ZYIj|vrtx$jgp)U<fkVwrw>OgJwcG0y~-(EO^>M0a56rqKG((gkgA7p-ql3ozcY`y2*ek#bfYhDVf|c;_QIK5chcWQ?1q!xE;t_9sb0HM54?DuAN@iQ??s5c_$3Q<pOS+;kQwEVyo7L+kn`nPNvO(AL3Kd}Y6>$^ou7fDd1<J~Oh9o`IP#<YkrV2U%m4?Z``8f72j~r?%p~`7105Criy~Z562W**Zdb+mC>$l%YZ81>o8+%ho#2NGa<U@Y9mQde$Pct3H0}!u`MN07fxf^U$CLe44so$K3H6m(iu>126ceRrI8pWs)E_UwW$MAj@(i3Wh{xHi2%KUrax#g2Al_4PzcSKE<2+H(3`fcTQxfyZ^Rw9@D!yOL4aX&xm-C~TFNEV<MliV_%$!6k>vAJhz98QI262bJ{@&3H++*y!Ta$@f)yz+hW|H%{xOJ=mH)@M;>r@$TpQ*%;bv5|u;&J?Z`6M3JpTVR0lX!HIoUN-=`NaK`Ik->#xX(O8`2V0LRpb51T#fg_|MNvU?(6t|tr_=azv91+=b{5@yIBlxrU8#m<v!uQoFn~nqAB&f)^+;%2Id6U=<To4<6k?Wm4fTXb;oM8&#cSG!>UN!%=N;x3^&{^4C7~&{wDwHPvjvhi213P0V1q-BEoVL!c5j8%x*V*rvYN!P04#JB=|WnPvvtP>4)6d5acIDpdcv<`N`49k#@M~KqOHMqP-mv$y{67095|ZXWyUv_oH7|8epU8fWVvHz>^-qgL$|3`zG$|N{PR2=11uJk0{UXXD2y=nU?Q6TWhi3!vT?gZio-{Q*l2hB_2gtX(%V>tBUhcU0#gZ$}-eel;UV<AvGZbne+qV6@pxBnClxUeQ`3@;=C67b)|{u&q1XDPRtXW#UqFx;kKi}Zi3vG+(B<A><;aK{o(B_w{hQ2<ptc0_bLtWXZ#Oh?i1#0s@hGGg4~hC`&GcWUrLTwk?Xa^xj0dlk5iRJIDNDPXR1nYs-ghLin36Z6_1jnFyuveB9mOt2(U+nza90!o*;)qT~HXtSRU<3?@z=s_Q!c^{EsL96a7#_?pJZUg2yY!xAI6A6a?BK&)*t3epckZjoK~<wnJ&C6TQ2;${B?JS0sj3<>1<}LR=-!ubryEwbNC&N?bczP1NA}xmsL5bCk!a1yy;tRGfly*~|&jf^dxaz)^Y<K}EPr6RL=#%s-CBdy)I}_SvDhlow8~9*wKw-I-emt`tV&0`)*~U%))$68SG0aFrh5c4abSKS8g4hrAX3-=)XDS(AfXwRyNr&AN56RB`{#`5OFq@dSRpat8Ms&f(#;Iy}5yheub>;^D<wJfaVHa6Aw9k1+?Jo;<8c*YQ4AD<$p<M}=<!;k)8}?UPdZ_L3(Ym-FkI`SwPT?~g<mPUUI3BD!#?T+@_myzUL=`8UrHXPMWZCz!|IzCc{W<GYve_zw5omi?!x1FXx5Wc*m<PXypWWh}3mt+fgK2LDCR^ZDGQF$Sdgm?GM74<ao$Biv#GBJB221B{U1!(%}%$c*qtZd?coQ({n-nTU$qG*mKHRpe!$Br6#O+@BF0jKm=7fEWD$b8SrnjGlM^^1z4rfVUOnIdgC~=JXPCotfu5t9<`};-ahKzO>2d!GpPhx2@#+<UeD-x4qU5C~brR)Pa3l)?mU9L-EDOJ<#sW7t!+NXB67EcnO0(`v_CUj)c9HIZjqrDLp8r7f6qbK$M?5^ASt9o9n~5iSt@pLu0&mWNsjB1RDPjFjvrcuR-I!?cr_Y{dU+M+^R|^ec}IZxbhr#)BW%=)qFspodLDc5_;R$VDj*x7}Tc++W)G)gIV8c<43~9))E&^9>uxZa-1yBM@?P|iW5VSD;mI9pW&}*Kwg*&^2q<<C{Oa=3uUohjPE|m`&ZF}3u@^7t70`RC}-?1XO2)5>ZsZaavAe;{H;{%&-D`zV9Q*<2Bl$6IFaasQyF2*<qlxMq!IYK--qbHIyTl<wukoXgP9XY!^hbcw=SH*t@Fol{bU8|kLD@gP?sHz)ARw-u2>~`2~jPGA%y#h<bJ9j&Sh!b7v77XzfqB@p*)d!yl6lSH6TK9{~~cdGlUu-ZG%y`Rvb$YkbrA^@6B@RfcSsr0@CiXc=AYm+wY@a*88oO@%4aDFn7v01bI5+;f+hwfD3p?4Y+@ynjWA84^M~&WNDl~DY?EbHj4)r-+ir-^S?rUmB#<Z*sq}Ddws<d(62v|++D7#pv&cj|HA!SC&_K{`mU}o=iX(O^@Q}>Un8#K@dM`dKVP8^P$Mq!bE@O<pd^g>OOlpvNSnaTlfTLTo6Lg>`0QlzdB_ZQK$<^2s?$D%nyp8O*(Pehe#CoPA|=EHSy6t-PY6Y624hx!x{6uH%L{S5f-$zN07r{*P?nvBJo<u^Fh9ijII4E?0P*_dye~QLLoN&dy)4OXbK%(m4a#S0{8xV8Opm#~@ZK0+!hO{rU<y~pYeyclB^T|D4r0slxfs&x9rSMVBD%DE0Uh6X4sBljN7HZJN{3#ZJHS})5Naw*QB_jF{2&F%;X(8dPTF-P4=_Hc+5<EkR5^%~;eM?Y{u>GZ_b50T>}}$|aNg#?HrO28rhI@S_c<HvQXarr^n!VTgWeA21beY-#cX`l`}gbHyGsXH7#+d+<5f6cTY*z$xj34gfRe;u<VSiUm%m4Lkh3cDLS32nyQ3tgk^hqW`zT1vujIZ;;Xm^L@dsrQu8RK>>m}~z_*){!*Ba?w=1BE4L$aGOGQ2HN5M&4Yee3akpU%Hq-w)sK2nRDm+^9Q=TXn~AogSg#Xg)3$Br<MCsCL9-%u57{`>`7LrJvz!S|GhXV|j55Zd9h?4!!#w#@~Cyjq)Uw;|u>U=P?(dM-ZQIA&dOa3FE#9)u$kR6{j;oaUw04IYc<Tjd$UPPrCeWy+7*u4qR=_@blFRcy!|e9$Yzz2N$aF;A9asKyvbYT~4mcw*~dp|APND8uta~e#L!3LxtuKu4;HBJQr_XSK5sK(uSwwzj%7Zf8jnk{<HMa3h&AJ2RDgZc>GAlqFm89u5t<L)rEYmy-+?!Jb_%}H~C+Vl6XJnmyXB^aYAO0J(7J)5MjFufre}0Z?qoaw)<eYZxal5uUB!abDKBRJ?zr11*VT50eefvVh<Obt}a(vP??{}ydZ`e;G=wi^q2Wd>{tBPV!yW)<GRFt@z{;IwY2e*hZ6HWbur(UvEJLk44#blE*1u`GT0B}{X3wydo$L|n~V`3cfgljUPr%oUqhb`ui(RWFXMx^UqaVbFRI_X`S1OH`<5^~ydO2?C8|vzD=`MKfnEr3vZfXpC=a0809C$!Nc9Cc(eFD+TSTMxZ{`6U^!LD?vEGipU%bD=;T@_CK=OZw!?J%TEcb7L;f{4M+PNMZ=1pRK+yDFZ?a&GqhKF(C1bsktsTxO6ksgJ@7+>UtyCauzU-(~0?ibU;i3ccR{x5BSB~czIjrO1(cq;x^#rmR*=aiEBB@r&<zZ2uXj{l7N89wGnayLPw9di|{g9x=g1ikh1Fy@nv|8{-5vcC3a2Dp9cH11FXt{pGMm5MB!&xuiaz^PP!l?w>(Pd4NJxtvgPKc3uABloj#pMF?yhw=9|WBK*cB>Ma~tzVE_zmOH$gfr>PFOsEA!wWgVjA4QHu-?5IV?TZO->!GJj;-NhZHk9CFX7QO`sw;pcyxjJ_Zb~`g_DZ+I*u!T);!_<HQoM8r5Y5U3v@crjQi3zr^BO$qj*Fg@QA+Pk;(&#wD>QdC2e}-e@2`i!u_4GWX>dX?buS)w`<2%STtt}g8AGgGY41NaGe~#dQ7X!y|bDhxOqy`02#~i8~nd>t`a42zR2LcOAm5T{mwCb4npbuz4bT1W8YHjTQ-?7t0TI%ei84!`8?Xb_H5JrYs4o#-h-Qi4bIe7Y8sH2iQL3UBn5jR(vuqCWUl%EeXR^te}I>c|L)B1-RJ|{nd3`4fTyKCyrhlaR_k+@T;GSD-&we?zYhnuZ@~7o%dvFkL`)gf9pgT1i4nb8;`<(N;hXL)@Kx70G35O>F|hL+__X7z=)vE-?W@l={nnp+&;^zz2C8i^H#HH7VFC02j?^1t)efk609_w|Xn^8>qX*cp%m1Z)QuxpKPyXAJ_jX5isM0~&03|0lxE-c@Hsa9cmDs*=A(qb^kEws4zMuAbA5PY0s$JkjaW*P5<53hJfb0-g<b*gA<TCkRM7|e<(bq?~YhHj_P#WW*jSV0-lopWxWugt?^b0!v3o?DJkmzPa&QpK&w!>=gdhA{{1G9$q{EzGVdGD^w1#EERY%Ok{Im$dh^8n|w=>gILa6HKy#}mBCeSf6~brQo1q7}!5|Bc-LiMXfupNX4gNsRxoiu=m{GrnKSV$5f(uV!A6$LBWA%?y4P2Vk&y8RmZT;eTB3ejjy5AidyE4QI(sa_DlcuFbtM=2vO$ZS~E#->3t^Z)vj=-iz-)*9cu(zHtAJj{B<bp8UUd6b~+!;Xco~cUsygGjY8-9XG2p@Y9(>BnCQR)PSD9$+^#ZcR_@|8@Vpywk~TPLB@RDyP)y^+VS7#|M^N3#ds_3C;Hl|e*Xa5BXBilEIYgrTj!3(m%MMHqkVbbK5F-h(oykL9bSL#pXy|3q=(Zr6*yK_h|=s7WW|Oe&fgWGE>_F~n7>PZfW&qy=|k67d4Q+HdrRTI^z9qMkLUT5^D?$4(A63q)B+oN{X;u9Vg2%jSU7VEW)B~LdHvg9=I8A&{j;{1(zgvJf1+U$F`>`f7}dKKzV6mS>3?Hg<+q+V>IbC7$Dkl187WaAi12ZPkK_Q<pvJa8)dtYm57^W$pp6Akd5Ym)%@;^6An{&rw<@I{z@DYao=w=lX$3YdolC8ngt>pBzEj7IKyg+wPL?tU$VozRLI`r3#s9o8SLOh28vnUp^q{m!1EgJmucI_AC?)@k#Uq3|qa@5(<@;%#76`N9_dBu`#@kk6*UGtAGG@>puJ7zgV{p16pWd<>4K+o$RFbSbx$ys_@IR6KPxHsQj3Dw~a{ffzl)k%TSxwx(ulS#f+sp^9mnPzJezfBK1>$^0u!{TDao)&h+=_HCfQ$Zi7;IgEl@q`E!}XpwZ9J}=B+qZuk<$`WrR`nk#iboy_%9q*{Aau+hgHAZC0)KRkeosK@`V55@5O^R#(v@cj~X6v-+gMvos)TJs7k@voJiEB2IDw&(#vB1f5bUYXKUsIH?+LqK68XS%o%Q+(fSI0hyS<FRgnMm65f^wb23tGJ+=oo!+7UvY+gJ?-K$|Aw8XdF-=c=LP#QY8%NrQ*-s||J!>dZ4+PwNpZE~@*!imZfR2Ai*C@mgo5&o)QAcPv=XQyiiv@z7>0*0zDK*rh$@BP%c01E`VSR=^QMvd$7pdS#=zj@UXESNDF)5iRO<>Llp?T8Pt^821x`gK<<7}ybW2ekhMX7y{Qe)I1b1LPjIqlU`wJm}Mp;Lm(T>ko`TbbzPU4rplzSLuhO9;wn?YoBb)OI%G3H;n@j58$NQ2Sf+<z{x;zeJS_S3+#m6*0tEYY(5rFpMaTTf53`AQQsl``XGjW?^tmbj%Fun?SSE)$PIH<@n19`pD{f@oEkuG3;&Ci|M%25AK^hQpa$@mK>7$vL<guJ1;GwTVXp6Mwil*5R$<4oIao1$4Ax8>`iJZL?H8Y_KB4QUs&KWo7?;b^)c7Fr`^OV~=mC6`7M#lnVeF4pV{~p;lmExFpK$+pF7DJY_E#jMp1Hvj?uXz!bNv&EzNp}LjB_!8oBj?Q-mnB4=TE`<$zT8BdVfFobChN!k*mU=bDFQdaZ=;Hc<M&}EAH27;{`92KH>d|oTf2EDxbfo<Nwu~UvOW>?mf6#MekjR%jHRm_j&y7QUmPiC%6A0&P9a!k^AKTy=$5mXv_(2pZVSRFZpAvn+dtUALe`3W8d0ESUGDP<__+Hsh_rO(#`Q7Kf&0KS}T7gzN)#73i@>K1RqyN)Rg9<JSSB>3-N*Oh;X+>pre^;3y{8lUt6s&(BHv?`^kA{b2T0?#N7tL<b8;TJz)n2#^wE6*J0Vb85lR>d(0g974|O}g*|hA!1ifFv0?0IO~3X0FFG`#@wXnw`pW(5(fTE{e&ru%^ZN5RxMLG?QsPvfp^Om>bhd@3jj_@I858VbrHw^Wu+-COz+t5aq5(1v$VH7)+N(6c#b_V*9i&DaWX``0dp9h{^0`wnapZTHGv@0*U0<Vp+fbdCii(Ullq7{BH^PhY-%aCxDC0k4e}0%d3K;+M`C7&aOP^q2gw7Ap{}=NaE0g>n!d0O}^#eE~(cO%`e+Pf}1z0?F43<v%9{U&mx%xUXkGgiE91Yd<|0St7O}~C3`3e6e@4vv9ceyB5aa_26hq!+_U(<mT%>Qe$aJ@WP<p&q#+?)v1WrX2O3iF6~U*s~^_h-B^+PV_!=1sxMX(Rqrz1y<h($4hw=0(j@-#(}D|BmF;r!^gD<o{(|`<=A2pU72sC}RZZ<JFiR#ee1mH*{_JzvBP>OT0Hn(^13klM&>IV0swC9jmeA+n#@jbD#BoAIWjy<bDI|enImuGABgF1^;ILUxK_yX9Uv!*&f)0J*(!aTy@^~AF%A3Zdg3zy(SHv-T#*mk0tf|;e)r7PibBsTQft{lK&NX>BvutMslbRB0cR@9uO#F042`L*j<?);6&a#SuoyPYy5Z9_^-z7v$QhUhpnrZVg`NX(1HChfBg3_-#j12>t^A|vI*+97QJ6M=-prZ&W*pd+|TcIb$qYI^GfSiE}Vso_$Z`AhpO>FAs!A|zalljTL+~9x_JRI_SlU&r1&rG7N&=E<AL_8z6)1!-@{zS3>|>gq3zhddNJlq9*gh3`~nLn{_v;kyK(6pRLFdz)F>3u|L2hZIZbT<uE=7(m>cq>KR}x&;QoaFk)9|Db5U&rWkfk+XL+QD+84tZYO{YM)-RZbi9Za%@~I>Kbba?x|0MR;AI%~EQ<VQdk?hZW-4iF1rQI(C7jmO;ow>d6U$}oyheqz-tV&~Ukf1!mMaBIPoJkE({I8AoQsbXos9QUh&BpXm-(uB_QGdGL`nxx3eeKfEeoN*;oqxiA;qf)ycpJt2A`K^X+;8H4zQ%nS&wo|Nf5rXNx^erpxK~$#vxRZU;qMe+bp&R6H)8vWIe&_C!QL*Kc3o%wQqRx1RIRlG{HFgeVf=7}r}0i4*|d~#e>^4+{|c)neZ%`V1Y0Hz#QHIxVfnY+|5qKA+)CP5dbNGI$(Kx}N3Ws(m-t_h9F5d)KSX&ukpGry9+3C|(SRTq3pE}%*ophytd#}`|CI)i|1!?c{O}&EUpgP-M}Cjb`+SJ`lSaVh$R;@MUki&Z3vhs%zjo9oP4}&N9p??`fEm1J;;9DGKlN<$65e_9d8}G61F7VGQe<Eg|7ARZ^f!9z<^>7=8*>2RzT^NdDhFt62hifZaNd*Wcqy$o0?R|pRaP#<)Um@c;M0$>aLTAZUEht1XP`JK0!4{oD2NSI;{b9P|Fg;W9P&LY$O+lO&IDfvI#3532`4pIsF41@m|RzF0rdWg|FPaEkMlwi^MYs>Q<&~vN8O)*?*@I2Wz$Fh>G~enuoU$bIk;4ojtj*}IGq`Wngr(l$-avJ=Q6_?`{NY<Z&#<2_c<E>h5NNxsy*Old5YqHJv~BwUKHx+A<m=*;!IjFYI!Xg4=pik?cB*2IdlM4%ozKp>utP;{J&kNjjfS!H<CY#_ZJ_~$bV@MRNODnjSmq1XOa6^+BiPx$5Y&|)!uWr8E(j&pQE@#zfhAJf+%NGSd#mjmd?hOIivp+=N8SFpmG1&IgS4pWj?}*-{${m@;}TG_J=l7M`vN?nD6j)|4(pu#T4kTn2dvq$70v4Z`FNVIQU=dsA$sY4_l#sr`Pd*%NMAl|3K%qZ=y0c9c9@m$WMt;{Ezl=YT|#0Dz$Bi|FRuK4QTWLfv#2vkokPf3(OAgz}m(0FzUOn(7Q)hmH+!$AA-LXb8)>bFj+qvTc-?G_e<(1_pA|8N2#B9C-F_9nLXRSf<5b(AuiYtaY4R_^7l~l1%2(!)mQ^B8;$>}9l-SPFChF^<AaU%>-q@}C=Kv1J4F7=ct!(Q9^Q#fD;8k<@Ndzl_Xn6cZaDn^WPOipUXI)tf8@mkATQb<xshJTk}(;=dFJslE-*8|jy}Kv*+CA-3D*2TKK*@BxQA*FP;CN{p1M8(;lD4vfrsM1*{=0iFnJUP_Wcm^CXM`4_3hQ}RX8#(Un)r>_mfdq7>`rZH!r?jVt;zDiv2ev_E)B;Ht*Xy2=`@<-VGi1ukxI$#c`<5kH(eUNYqhB&hY)mlKj*dXuAWOm0y){NDHTo`P23Gw=*DT&uZMiql^Dd+&`upgI}uod+FmlnWu4I#t1cXzcKd9dv2Y2GKWubzgoTjW>vbn_tM_Bd-X!h8b1PhtET-a&UJ177O#Is^<O@|bX?78P~(Gt)Bl$uFT?>>d)Hv?oQW9s?I7k7-C)0W6>N7cSAOd7((%|dzCU%e^FQg4KzxbhDnof6rJq%FRO(Zlk$|GKIJE{qs<Z);|6z>rk^_V|S!kS>dBJMiO6357e67v1wb1(h?Mz^Pa3|ycJdFPSD}2zci^>6P^!BK^$iemoa5=nD`Mr(f`>Eer^j_|p0=-{j{iMC;3u^s{0ln4up-5kuckG5RFK7A;8|D8!ZH(Yysd2w?EO6tP<i>HyGFH*W_+V3Cp@;cl1#cSzHJ;f<e-E~;U5d%0hvB21-SN%f0czhLt?%f8AHu`*05ZZnkQ3=c{`(<6$_KgRb#A!U2GBGf*pJ|Lc7P+Yl@D-M<AaMLwebO^QC{=`UO1W%fTQsN)BzvFxm&=3ac|MIG3ei?7lwa52=<11|8RY$eElgh0-SImKS9ksJ}>hFvm$Vsygi*Bg!7F3SEva$%90iT8@YeGHe1c@mGOUy`z1-JFOJjrUl61Ce<d$k%?CJ@7KB`CoQwVrte!myLq7i)W4;@r=5hVede0m_5LLM`x-}HY{U7VJH4#)B;4uxt|K{9h%onKfyV8Fzb90(v|G(hAXv6s;T%)eZ?`C&+8&=Pog5h5c{C|XV4=<AUmswBUe1_lS|J`%td_EjuxqCHM&K!py27iX0@4pMDBU|BRyi46fsgLN<itj#9_w8SKlF5Cw_SDbcc}=xX_U!l;^2qzVq)24Pg(4-~7cnwNNbz6h&s(Umfhq@Z(sBUR2I!>Cv-gqtg1T62d3ZOrtXhmo!lw_qqtA!EptolmQi9!(;7?ECXbk6r>y>uynDMo0_gORYW2_pcwF66ir}S-$k@R}sbbTGuM|=qva{~ms+abi=k$yv4b3n!!YwH1M^ATh`fQ$hWF4!CF<+HV4n=5FbjR%sk!7@KUxbI2d?<Hd#sUI><VE^Vdm^EoEKK-yKKI`)l^!IN6!}Xmr>PrMW7^%5{+2LNuBlmN}|A)CFNBB>Er<3<-zP8BlvtwMeK{~gy1IT~Ie?gII2hcR2BHkBO@qS7J%3^$w<ZI1%y&bFPPQl>MKE|MapTOwA&Ocn=)ze4t`T>gn()VAN7lS%-_bmPU`MgNQ{i~%K_Z9zZGPIK1mwEd2C5gDgod1fB`zi-u{IAcKbpp7b<$3CfjB^aMK8zj9=cqo1Zw7u2lY_hdaJ{!Io`Qx`h2%VA=<Tx__a%RBIHvUjs@P9{pDxrq|FP^QG|dMT?%((q+`syS`_evetu%q2%>fql{`04f!JyCkz*&FWpW@s{om%7J`D%^(GJfg&@5lci>&nP~2iWXei&f11za7vQy~zK48<ru=*#rSLhgD8xuxg6ZQE5+YtWP5(my))W;XPYoQ0F)Bar;*=d(>CRiVi?lOb{}n0+1Bqg=jB(wMK}VC+KLdxbCm(2k^HyRs8^dGJlYe^*6j#EH;46kv-T)KFymtLFLmux^}^|N#hYn{>mKM<Y0G1xSGSma67EFEmCzouxK=P&;Cy3QgRO^XPWqND~ub^mATd&I5U@*Ilw;Ty^p<x=J(BIO#p2^pc?nXSZ_~`+c9?Asj+@iQVR%2-IyS0`;#$DvNnLXjBU0vQQBd!YcrP4nTEmrKUF!)jLGBG`UC%IeJ6g=4g1zCM1+eOlKmXi8X?)?<i7O(G5(7NWHZL63Gazi9~;7&{I_8);E0?+CpAByP}=$;Jm~p-6e{904XB9qLk7>2^$K>aUVsr_eL>CYf%!8gBiO_KKd$feAwA)AXp6RXNI^WgP7gpX*O9-M8N)9Z#gO|+^!KUc{1b@Zzgdy0=IP5=AA$7y$+$r2ACywK--wHOQ8=Cwgd|T(7;RsTi9ZZgxy#}?(-Gq7@E_NE*0&$igPP*rrAlo+jr5zz+#8vLBixt1J{g;Lra)`!{{{E+pN!oVt~cku;{H(`_lvc*!Be@oUYvlOAV>0lgYy1^=wFU(Tl1$lw{F%rjrVm`TJA63<>v9<;eR;_!kplAcoQ})n1W%0KUe&hIVhr`u|D<)VeE0DjviVvR(X=fzE$xiOTKzv`6+2j8S`OlHLgN(5`&$qkjnd?8s@3y2q*ZvAllOo;cixHEMS0|``c_@psn^=#sR1{I9cOE`r<70Rope)vjuAw&%uaqhM;TbP6~_W&O~A;V_{q{KgSDE^nH^1iGEt|SOTLp)3JB%FsvKh7Yn~=hiUygVJSVX!S2nlX8s`a4BVxSpBf`$f1G68zvBJ@*hxRW{yxQb1%o|`_YMX+FQ7cYel<qW-AuP8h_#{m4qns}D{9u}m5VTT*f;3${(A~b=FLLVe^B4)13F>-oC#`dL5Q6p5*gPsL*0<YJRqCA&l2uSNgtWU_@C@$jTCQdBzjsQ&Bs<DN7pZ)X#jKoSYMPg{vS;WR2m@L(tjA`VhWQT)Yu6l@ky_4=-azHRxO@~q_BU#zOx2)#-VldkrV8Ulj$L7C}P~Ehi@n*$`jGRINnf}pt#<g|Jv&mr3)7e;&6%FzC<5>g?b^r{~Gy!jqjCG@xO@qKw+F}LzFR3?uL7?dd_4F_@p<!pa#eov1Izqf4AOq2EPxh{TpzCb-j76MB}dT{>Eu-tbY^t3!8a<9rsV=KUsU@3ORS<nD%~kjbl3Q^Sx(_74HSoUT~{C870(Y@m=%zOb(Lvi&>NZ6z8JdELay=AD~Kuj3fS?_Wz$RRH88472d`>v3J#6O!$5<dUx%Fal^l(FSbH@gf9{UU6n4${WDrS1N-I=SA8p@p`uUXr^Js)zl7+~*iSpbZ0~v`FdoTT7m4((alTH7koB@8znA`g;lGUavo>lP3)IMepC<krsQ53rxTW#{s~MLjQ7ii@{&#=Bv(kW=K*mD(twTMjF*Zs+rTs@V|H#sD*gp9SEc)(4%pUtaHZEI$gIm|Z?7$9kdY_u><D|y=AJlSu!vku+hP}=B-%JA}_8aZfxZgB(Sx@<Y@iE@Ek^>xs-j0pR0}P=D=+Wgpe9*Ns7E=TMo%$~R{v#}&JRAo%ELC%g0(g%Syvb>qhZ#!WPcCPP77&?1&PbIt!^r&v4+|uESR#qfe45GuoOB+*6$Q*0N@Z?PVjzwZ6$yTd|3%CRlD(|pWwH;uR?f%hZw4#=fBtb#tXeV;u|ZzHTi<2FKE~FCli_V~K>3hkDZ#j0oS@d+xvC)Rhb7}`d9sf0X{vv(k^A+<GR8L!7jmM>e?cSvV>RwGCr~=TbFZoS0L(3@5!HzSi05-}vU44#j{aWpfAHs@VEu~4hzs`q-FmMW(HDm{EK<+u(L8<@IsQQ6z2xsQ*7qFwB<q3*$o&U3jd4FmLGfS4_zU-i>$1L=_=iUBU#if!f4ZQF`=TFrse3ilU_aJr_2L;A)V~jAP8o*~=Er}8bJM@<srr6Yo1ggpi@IFp_xOMLD9YkI5o&iBraM<-;p9>HjK6*FuJ2*S#IeYU52cQVAcgl(*8TA@<MY37jml5NpX{4ILe)uhNyb}_`?xhW&mRvDlkMcY3F7^1sb{vl|F&vvydZKvK;{FBS7z*$xd3V$ppO5(jUIq`zl;SI9dKtnbtKnJ$=R)|7Gvr-*74)s==$Efc>le3FrD7o<j6j_SsOFQuv2RlN<FQ2Eyvac6R~8{FiaiwJr+!#i1mD)B{w(Ow;k3;c4^$!(_+1?-aaknll#i|bHA-#GYz2s*VYNr+Ww?JP<Zd5<|`SfJVw?ylJ6kCMcOELuV0Rt6Go%|Cm*4Emrm%`nYulB0!;s9eOF-n!bw;@WjN+e7>2d;r<4E7)Eq;9TYbdwTxkQy2ys>G24)gz<bImJgNpx&<Z!&3ITGB>H4UH-$na&(q4NMG%=HD83I6=eHSUWZltg*ai#j2O^{_j*73&sG#}8ljR~qo?hs-}`PlF}((UbX7wD*(x+OWPm7f;5T8KamxPJl7}gTIX)GU-Q-rH0}PbNm|>>9|&ruFC7ecWK{~wRy_ZbVybC{w4BUcz+=`n!Ju8G#!vQFB%}F@Lt-@G*C0>70zWvqKsM?;%J0@>z87@<N?eP2YvQ2mMxeK8<QjOvNu=jE;QA9_X_M;HVvERO~8&7^VE8e$;_*23#l(M_g{RowAD#mKS@2P%~8DnsX9~PK}{we9?wx|YVWVt#eU)cwPxIx*e~mgood!r$o%1_+Dx3wk3s@-fCJlC;rpTeF{p1(ESxd%4{^@d`~Vtwj}_-7?u%|oKZT4{{;l;v>uVMN<H%JvgYDS4d@e?RJrKRSy{9x_3FB*iQlzSrc&R2G)!#}lwG;-cr>b@e(W5#2+hfI)@1ehb9vt?shQH-5M7o*rnXy8+tF-eQBS_W-mGQpx%iiRTtPv*s_mR0lw%U53YE2+r{x9$(|3w32zOUh)%_@dZ88aOH`hJY}JH5lW+65o??uns;`eXK_u~;!@3KmZri%G-2#J7X{Vc1uL)%i<jPsP@i3vr0?-*n$LSRL90>%+Uq@jbAmzfk-)c%lKejoP5|0S;;o&;gYz$hsu5PNDP_Nc~jXpKgsaAKiFn88c*lV7vOv>ElN7x$J{()U00Jx}fhzz3}y*0hl*sELP2(ilsBgVao8K7|#5A{IIXFfcv*Dn+wD3Yt_8rU^^qVZeU7)qvE{qKVA4wJxKDgW9+x(Z)mO%?`}pPV2&g&X$P=VV}$aUClp0{qm10IO!U|K|Cs+5b6;t!kMhwO)Q~Xd8PXrUcEL1^()oY^eLupN^f05o8;Yf~CQ%<|VCC!ym^u0zO#FTb=1drYtxM;?_P}ODGLI;x#+;IMLyD8t8a&r4(r}fyUYVvmz_p4L#oudX8u!KXE6(RV;l9Ln<pJ^<bN+bd6Y+dcvJUhOq5<>-XEVZ8ZX@fg?5AEz|FH0X=og=nSA#HV^bc4?E#1ufDPx=#OrpL{9Leu76UKYDAehf)UaUXPvOc#?7U2Hb5<EIph({;#a97q8A~f!2==e__AJ5fzE%SF3|Eo3rU(=zny-)IqGmZSu*2W5|G0yqanp~8m1;gF)02WUji9sKC!=OIhv3A+~|A=!YJ6577)(>?>iMS_ilTxSiy1B)_$Ny_5=>`4NdXgbFhhV;YEml&?r9V|Ppcge@CgW>_k2A7j0#%!eXi{T7C7LATEY^+tLfzNxD`vxB%VOB?Uj=WY%?PsE!?<sZ5PF#)dqaK?;k*75{tIt(Ie_qATOZ6+%?0q4^*yNtivO}UApL?3d8oH@6E-o&n>ld|zWwqG)yLL_I-v0GJ33A1Opnlu{2zb`BfqDvPQu2e^RRd0O6c#{05kI6f_}pK@a`rJ(0suDCJnGRX!HX6n{i)HabLy;sdY$YuA*+dppR}mvyXbMX@c|*h}LXdwFq-3jlmB?2Qt6zp>#mJKzAKJ?9mki`OJ>`_Djr}G#VS1%*LV3E7Y0--j;_E?qrG>5Bhsw2Wo&5Qq(%Zj;c*i+6Cen?_*uf5lb(uX@G@VKUCHSl63;4ov<*<TWLT^EaQHh4~qF(_^;*<Mz|xN8j!Bj0GZRgow>m52_y0K7oSoGdZ2Hw?)ao<H}v`7ef0b212tA?#^~>`e*RRL?OcyQ{<hK<QJox$^W<y2#P`Zf#eG>f^lFL5eaZ8sU;hd{zpNQ7P@E^<r97|r&-kBD{^!RLv1kzf7dPjBD)}$#8K<IwdHb2{Naby!$YqQDTd<mZAN}1B=8B)Fw)H`uY510Po-*n?tl@F}UF+5Fl@Z~ETIL-MRT*l|(SsBDxL=z~kDiV@gp@y4Y4}OSejWE4Io8O3wQi4&`x5)5pHLV38^;K%F#_j`@ZeH8?wl{e+44jr`<TLb<2+0rHjs7fjX?uG!Qut85asXso1EJ`WeE0dSVC@@s{Y_}<p13h`MNcVO0~R1)+zWc{@*@@+LRzQmO9DP9Nx?U4Yn+29xz(<sfq{a-K{ewjvkIVQzxo+xFAOp)xT<YU<20A8IQ>$zQOddKdA9zdp0a2uQtL(e<M5$Ho{YHBYaGDDgE-dleK`g^>SrhU~db3l><l{fS+!y@aFx1YL0-c^GjXu&}o1Rxo6E-ZLn(-cC1;d^6ROL?ZdzRl6v1ywGDhZfVw|qAjUF|iuYbRXR6YRy&G3SZ~J<fQ1dMqmn{$ORQwm8Zl~7y(XBg5uiHEiQ2qYm_hsC#g@GC)Aa#;CgQ8Jh&G;{U0IKc6TKF&PiR-ENn^SA{u3y3Dat7-(3S$^czx#3kx%D|Usy{}5I|S3l4pValH!quqBU@L)_V5lht|*XvjNtu<WgLiOtWTr{B>OwDv_pcI4PlKK7YoF=nyK+*GM*=1+5zbQ)5(7sCs;tP7eosGeVR0&NcgYdi9#7C$n)}oWh{XMqIn$;v;8n)UcH&IeIfnSRPtsV<Grl4By*aUPG=6iYz|DeuZEw+L8P!g6$t@q4bUs(<yG?LI&rlujov+3*A9?`%f)eO?B7Kh*IN*;xL&8@yl6mOb_{)egjzrB0&%4vPU%2HaRT|S@&CHCA5^5NIm41eNIOFv&#7VFkrCvg)|{3#7q-xQEu$Y?Fl`(b&zy*Li)ZmUSk7l?C!aABWQKX7Hai-Zd5t?YIci;T#e3@2E%7KN$!cBEM(+RA82cs8p4V~zVp-F=9=~WW6#mP3(r&1@pRdgcuCKzQhDzMOT*~hejhbjXL^|%r!R^a1ar`h08u%&k8RpEQFI+SiW{gk4PG*X84hJ`4+mdN&O@IybGuC?a*!-PK5(06FdMNyVaJE!iTZsIBbmO<j|H=5qWAq^TA@<aMK1WVQ@G#s4{f$epZ2A}sA2LA81*icZcJHDf+u{p)5(E15!l<tXVjlg}mZh^`ux%yzvmM^%d*N%k18#>m!1eHYcp2>^@Aon8?}Hbg6;HE++&;p%uMdBFBf=QLGXIC)Q)vLp0K&&k^U{)EtMRyITED#bakKr~aA?~)Y+thkYZlJJl3DUQPf+tX7tfrGmE_u{<qMdDub}5&3qxv#yvO3eHu8R_f{mUQ`yGVivIddKL9K63&F#_F1a~&oxG(d7RNsWOQ`np8{E3MgtDy1}S<g_~11zOY;0X8WYik_xwF`M{!_SjvQsxGU7Zoj7L2X(-dotG2i*8%F00%d(RO<%V8|>meI;8UHFj+fT_xBfKJ**H%z9;%<f1e>v))QxrCGGc7E+z`mt|n?<imwgQ0_@dV0eKM~YMm#6%=a!}yq7*fZGM1<Y6C6|*VYk~@j^0wQ2G<X9GKtmce9rDU-;YZU%v=@R<oWgO?Iq?<Kb-xvN^)fvSuFXjnkPCYQ5mA6~g~CTqalLZwFj0lRiAn>(?>wy;u;1b2;H^T)@S=C#WON%lck|>~J+d_yW(7{(lw!rTvc@a9!5(uh7O1NL#w-fQ+fT#?L)RzfhGFq;w#h_spAlfs658`nSFOT@I^wnN0swk`#bb<bDI+bE`5{<>Yr{9bvh)cnp4?%su|`sBk|+LDmLVZF!ArZ_B*@OXW}gHh^r)y5E9xT3^5FA5?L_P+Ldfs_?&@J|hM9%7U5O_@N@h7m@re3=DQ+KE26c=B<MU=`iqf4a{c*<3@gm70khoP@82vy-fbLC#aj(m}~u{{vLwH|3`Jjzt8_4>u{X>FATF+>qw{Zd5W~xQ~d!lA9Tx-nV2(vI7Tur9n!y#8XqQ*b?UzVvL7Z6`wEMujKSt5vv7od)%M^Pcr$JU+A`i-lj9~k;dW>PoDQs|AJ_p;)4ghbv!}Vne?MCT^>+bePLRw6kiQEcIw11{g#W(wTAQH6Q&|&8o0E40&bq%3AUV3_p`DEBTj}dJ;vji`VCxzjrY`91SPvum6%+bz^Znb^xLm74!uj0_w))z-|H6Ce18|acfSWYnfVTdyYUeww<^f5J7ry(+x+Fwnexm+vfm+8@<GqKB2Vng1v6XfW16?klubw>_FKkcEu{yK^W_vfoWcNnq2V3Y5wyE|-c}6{Xo~)Z5$oI>-;h~PEivJPhcnp1gjA#H6!x$gU=UX@*>1>QBCu6l8OCKQdKgq`$837K+BA<o-vTm60U&aj-@U^TPAajPB{-#Kn^b<IuNW1{|NhxFO()?|aB!3Hrd0Y^G*Fft->Tg0sI2mxirCN8VjQNC&@xLHzc9KgP|5ND$QgB)3@W>oq83(}p{=7oC;<uEI8c-)={Bt7J-wHWHoJtSI*~}0%Pq4nAk^jlKRh8B>X7HA*^(Xv2D(nAebDymJnXTpz$lohCOJ84|62kiVq9WcORmnj(o*Aw*qF(wQ#M?_=FWeU`p+6B^FVWT=Y~=sXH5r<ZZye(*5TE@o`QLmUpdxMkFnP^*|M;q$C;f;icyu%z4~|9Q-o;$h*O#NFCKpLbp>SgS+qG*W7A%;F3G|A?>0?I>{{|Dr4#)i2Q?QA-q!FJPU&hU}P<PbOE7kM+K8gKh8f3k~+oyhq|L1T#J%qot1G4!$r+b?t#a-3`)kgqz-|@&cHNIl|@;S_zreZNY-+bl<3&{CpvnFE0!s*z}cwoF^E$k0&g}c#i_%gnS*y|IA;cK}c?)ux|a&Rr2_7l_sNBu2uGTf>9wPhV`SMp5O)b}E9WIYfM)wXs-xAy)4)z{WI_RjhUxvPW3Z5bORZFg4mNHXunf}Y-V-&W@LTUFj|vS%|)>GzFxZ-z0opb?V)3oH-rP@2#jvd@h9fz?5|-VW7HD0zUBtOX)#mdTm`=GxyQkue00YMjts)`NATuavq8m(}<nBW-O%nIq(ExR>93x8lDYVJ~Y2@qF1YYYKbF-y2bXQ$Wi7YVA-r<9%u!VSxv8Gan0j0_tXf{7n@Zb4;!EHah?xGalo%pZNj!Q(FTq4<nTMRupwX@`6NfYjTyj0(B#iab5b%5>;D)h0=;R(F}o`h7@m0q<YC3<JL&yds7vx)Oec&9#3>PM>5|ho>BZlG4-S>!H>CQ08VCx;uN(&)($;E4j)eqMomf}suRdRdV~rYgD8--h@(AG86)cxdh6CH^hTMQtL&=QDJr1`Rq^=o<UpKC3svp?7Z~d<<WWm<qSW@e4DvZULWf8-U+{8%tZF|zmr1SRK4~MA_sbfA7jk2$H?e%Lv^DZv$vJW&a5kOwW?fGt1>$6KFwXJyInlJNNOjK5Qpp8URX)&&ACC$$l!ge@{wm2Aw4C8*L3KLr^H|f`0LK&YxFQsfYohV!Y7rj%bPD(GoyWCnCvp08DGCcy5f$YJS63^T@>w`|U@P|S+<=37=wJ1B!j(C6xR)(*n9Ci{mE47&DeIM<%-7m2b^KRa^yAsz=Ks%iYMuAOPzU7rYimHJd7AOJH|O(Uj7TR#1l#JVwE<k{No^S4&Gu}75&3Sib1f|Q(W}w}x*F|L=LA?vY|vAlP}UI&VChHC?`gP2^$D16S%AGOW?<vOsaP-THPFXwT0EOM=4_Q`YM6@++_!Pb9IRV3Q{`;pl~+>_*K+^bMYCAW!kR@I)-9gXw7q809ITo@Q-@htId2A*&zX+pb2Y5cVddNz%nxQ_HS-1yGZkc?yk}K2tX(vlpD_=cmd(fJ6$@2PxrODX<@2#&$y}^otouw}SK6Ic&zq*|Ds6Zhd2A!Uk9?O6Qs)KJv3CA6)wj2P!3?ZlB<Ib-CSoJKmfVLeOXYps&wI0e;WVDZI`Ens_<94kHxiqO4GX669PXzsuU{~Q$ET|Mx@iHih`PLDj+)QCf9(PsTDKSnH!P-qUx0mUH0)h99|x%2dsffK-qrK4Z`EAv*PXwA&3qhQzYs_G9-}QwHEdl5gH21IziFw0;g)60{g=aH=W5vOS;yRT6Z3(saMj;NueY6f-!|&~CO90}2%CNDVYz1wtoE!`=UeRLHX-{gcdvmp-*3HZ4Q#k=xob7db}$dxzLMv!gZ;h@yv8QR5&B2t-S9NpL+pmPiB`JNSGwqJS91Y8m}h#L$g$mUH{3<=e8w72Zp(YUjrY)#?&a~lYRwa0>a3@+{LPk~Y8?@mBdj+|Sr0+hLi6N#qNyQP%p3SzhdW4Hj8PM!oHa<BspLvw<Vu*GhH&a{q`*m^nyQC19}^slvc;`5H}XG>xj-)Yc><3fUBrV24Y+^*GVa{DfQAOqfpSz-q$7_Sm&)8eA<`QOVeUwa^g<zD*JMSbu9QAS+AC^vHSV7+)c7xBnCr^5KFJ3ce>eZHP4-1WkTtS>ERp4FfedQDtpA!Oe_ul8lLSitvW<!xaUPb4aWhw5G?;lp2)}0t%U~PX)~8l5?$fsh)At7QUI;=Q8LOxPPKVZE&ytClFsL{Bc5REU?OUMB+eDid=+>?!y0>e^eXY=~eQV-vbZ_4V-8!^Iw~j13wng`j?a)K;PJ8rtw>>`S)Dhh~bwH2zI;w3!Pigyq@13T7J>)nc=Y1gi1fAbOPaR~taeoiKue+3;<ofcy#%p#|*X-5#UG!4rJLt`A^_g-W>m+qkb(K25BXw<~gVeb#dZ=Tq`Tbhs{kARfew&u)DrGyZ?AG=zbd`N?zr|xMxXrTlo9Nv74ZPR#b#zjAjoZXqGGCo&`6}M!_B$<J!MnseEnep9S9ttobY|J5rG~Dp2%g)u)vHbWy0mzS=e)#oi8o(V==26lLiTs%@vbdjMmKJEYbk5aXy_(rDf@U$9_!xv74#rJX!9CAZ2N|W4sYP2b{cxMeI32tehnW8<UDo0tiQ+W5%NAkPoCGa@jRl(+p_Ns^lbMgdbewVkBPn=TcPiJZ{ri9?|W_V86jogPTcoyYxH@y4L<24``Y5uP8!tqd+pSFKGALWB|d+@qe8!~ECpTK<FhUun)ZL*?H%+dzUa{jgNQGCcGl4QeGK_f!_beqVkln=zWfyi_wK@y;5py)>59>VKEU$PpTmB`bQ}vcqW@2#H?JW-Pvh~!OVofX3Yreo(+||)&duYvez6h_%rojw6`}rE9vZ5d+Z@kfyw6vCefK5b=YHv{RA}V>g$k`d^6{15;s0q=#d{$yP`Gb}EFTMGQU_#B$m{@HmD|Z2Qt2C#F{F)a&SeHUsqrSV?xL(85#eBja7ROg+fxJW4b<NX40kj^DEEgrP!H^nC_Ub>Xd>@v8?<@`;D7vuc#3#hxBXWg{zm+r2ma0j|HA{%2zcNh0v>#p2cP3X1)iY56P|zOZ^U2m+&|>?U(~kX1-?n)S;61`3D5J`^Srn~dtc*w|E68{e;W76XFZMQ`MFYufBfw!RUcKazi6e@Q|c@A=BesCDDeCI?J3=N(|yOk{tAE5-3tM9_vXL<p8R|2U)+<Yn%%QTG=HX^{?+sKmuAn{zk>WW@@)PO@eduIZH}jYg{S`)@YwUjEBvrdF9Ji}eHk0Z_d|i7nYKpI&nL8XL4H1~G~n^$dJPXQX%O#z_l)lE3h2i3i>GgFe>hp7wM7ySrQh$Qu7BYd{J;9!{J(h$)$txE2(&`3A35)<{XGcj56mO?3&NaHK+Y@vhfC?A)(KCSvAyC4+)Pz|8R1~4ypiUQOw`{9jUfN!Zw7{`zY%HxPxC$4yl4vJ!&`WT_l36vuMuxPt)T_)(_8!jS`w{z^F<?CKC6|jpViRjIpTT2Q)u(TQ)v6b(`fh7U(oJF;-$Z$?MqtO?&ZIz?Y3$k@$%mk+P(bu{}t*S-T7_!-u5ipzxX$Fcv<jQbl|=YugHG+jHeZ(4sX8zyv;hbeqL9<#yU3D^-29(>AsKpZv2jK{ayE+|E_)S#@8)~H~FBx@${4X^X5|;-gv6nHZ;7hyO*!)Ap72Ih8B2oZX;f6hS!^2qxm^4esztOI=s~k^14+swAP{d{+7SOo4<O0^Xs;3cAd9%Xvgp0f#0`1(Sh3?d2ibPMcc1R-Rt&zhB~lpEBpScy>9z=ZMz-Ijso8McZhe1P9*oc{7v2yJf|yl+nt}+@IKL%=>FnAqbq;=ZaTdGD>Ux!@sfs*-g+7{zUhNx4^xf*^7le-pU`+Ne{WszqYm<Sg>UNC8ojJrb3pnV8~IMI3+GkYxGjB*q5;Bx8IP>m{%-y@|F0iMMT{GHZ>IR48(@RnAUks233(xos!ynZoLB9`(jFZ0Oa4n+QH-nfIT@?I=ScZGw^BOG-w$P;Ky3(jF+(U{yED()z})nGdOOk7R{V`V<%95jhreR@dw<8M&d*?E=YL>CmuE2I{bw<}^K%&9<#~l+U0z`MB1Uu*yo8b6Ur`v<{Z*E)v3w1~xjmfah;FZAgnG^G?yqa;@j8YRBR<eD>Vr2G8eb3Z@dU%VYZ%dk*X6M>J>J0hUT<PtuQxRGd>x~CY-EpDdF@v)yz46%*7ao!@AfiZvu@p9!RYQUV>DlnVO>W{eY?I$yuj~9@ZlKA?=+&za~RQChc3^m?>@2%!TT`mz5l_mcmD@Jyz@7F-|=twp`(<4)ouS3!`|UNd*?40Mlv*FxS-?H7}@bDjCuzc``*(S+v#bH?@YY+6vlTF0Hb+qbVp!h2ckW<JLtgeG2B0nnEWm<wKFjNePAXryX#XLxIL?jwofoijuCSNU4eN$2r0V(^Sf!~9NqERUA1!>-`Duwx!rz=dEK7E{O(U<;RjD+@dtmwvfh8giVy#eHGTdEYd?7gYx_RK?Psv+<7cq^qi3*;So)!cWgq<mOZa+WuV=8J=QCK;>mOLu`&lghP{Wdso>N%f=Q*tS^m(lP>_x2o{3V5T#Cjbz_J38w0OE^Ru=R`Auzk=I>=>+J`#=rb2EIbPiY;Hf%95{n&K6?xfY&jv_jBk+Ve3dO7&)Lf;@nN~b4{kUcCh+8AEz`3=N0cyX%ODaI3kJlYW_ii8hapkAULKgb=&Iim7LU#|B<yn#XCH{``i4#aS9dDZjArBzyB8EfPBXO!Z1e^M@k=%3koA7?#mo9S!>P-nd)y($$S$l#ebCpkmu4D746JzH%s+517*Bgw7WH;+$|C2VhVSYeOR|(3OX_Wds@f;A1LIDK6(MGK6?c#KYtC&KP5hU9m`lQ{!H)&7JvSx4lS^#Ukfbl*HXiPR#-NmHA}(USla(>EM&QeSlGWc=J#uj1^wPuSTvxG!jb`P{|Sx97WUWh<h(XmKCmrT4sMH8L)u~Wm+gsmST(pERt#*TK642_b8){`SlquAw^<)z1?#i&3u2&@t+0~yT;BgJ9(#-ReiKXNJAKCT^Ea@J-)kw$rJui!<^A}*`JI=2_9~Y09xVFgW#Sbq?nB6SpO>-t<Cn0M+si(AiD)bZFJf7r7qOh>%DyjP&8K{(`@Mt>{a?anmYe&_GtJkZzlaU;%=dkccpj_z{sXJ~KC65EEH-@lEVlN00lNmgfZYRM#QrZ|#NnYY<M5D|afGEFacJm^IP~R9&>Qj+j_|miY!muJbQr4P$PgaWK|7Dv`tk){>jhQHH4gE7Jzh`F*CPzR)?oV0t1$oWRag&u6V}7ugu|$};4r2o+z98f#F$pFAKel*!&|^=*jum|(E{efh#y+O;)l0j{Cx`;f7b$r-@OSV*(S_}wNx-4@iuHnwT0c7HgFu<7Ea^a!DW00xQ=fRj|uJJGNB#I4)AQm#CPB`<sJAEfzvuE1WniAH}xF_Ip#OzU2eatw*9ARaO3;8e(^f`FwVU~Pxtj_A0Wcn7(YsXqHtTr3SB;`jT=;J_UpzEH|M@^`eYvN*JSDXA5zu2=s#9xHsOKHN01yq)+LiU0RJxk8){KW{uc+^qJXhq_+KP{4?ax$ThfvP<TBTn@gUhjj_U6NWd+)+u_cQC?&gaBu~K?i5*CP+@hRT+i1)TdyqB$x|7NQHO8Eb7%a>K&CVp}R<JIy{U&O|NZzyaa))Q+6yot2~TVT}}Z<0e&wjiHc60NXiaBHmjvNhIy`8Lr8YXw8vV)bB+t1E?{gWG5ze^<!9LG9H3=2+1jgETZA6RxivLVVR88@}#9wAb)eN30*(0qcgg=V!NNUF7rlIfLHjb{ni4(uT*|Vgs>$D7WSHkk+i@+gLTI6;|^*tWkIiYroL8*A6BI5!|<q-+Ap9Z}I!{JNJ77tEdet`n|^O*RYze*9aI>))MRbzlODpQEOSQ>!)GE0LC0XZ%wi4{}6W;;8mXK)_%s?sTa4n5kX3!xP=fBGz6!(69`0bcM?JZAtZqWlHl$XcM8P{El_u*o!YdW>GaIh{<WU>{r29Ww9Gkk{+_w6b$wgDy^}Jz*CVe*mX$eC<z+53n~xEvrZsz1HMtpl0h-$MVVU0aQJK}^QJE9@s4O7k(Pd>xY&kGgPIB5;ko*o6B)44!@B@L%<?YJjy+>t9Oc~YEwq@|Vj4X>SBROpymDPK5+m+SVm$k!d(DJr!<+d%4_sdHjzBj*P1t|c>#a)cnb*my9x>p5%)nr>QFWJ`HTed>mpY#D^zOogs@x683y<|<dYLJ(#g-W`4gKaM<=nUbys;jrG?&1Ua${H}fzNf!z>FqCD`})iF{@@ha*&jSp1A=AGK%>2bsOnINUJnhaE{BI29UUGfM}~&FbaZ&A9K`$7(Gh$fkB3!<2L#Ei$O=*mfAS9^-bFuAL+4F=%X39_?!b$tu6I0uR~P#?-0Z)8LB4(Ioc!&^N!8cfGyMg4z$=FTAN0Pf=6rDW3g{*OL>=+dKkt(Nzb>yH-5^(Y7s|C=g>vH{&yZdt&msOhHNeH4t8`B(t|vXWy-?0>TLJ!8$f=FFmj4_%vB|K1d~<=E+O`_}uTuWGX6+F20oDK;a^}hSp}i!i;v>pG>&^DaRmUJ+O~T*oMC4eLS~?zJ_#fA@va+xIgNr0GL;lqRywn58e@d&{_-|?Xk96~|d|QqEC0Rdk_#YqbFB78utp|YnXkW1JukSGz(4U8&MGxTbKtECsi1M=Rdt3Ia;rFX5$?&ff%Y7>RU>y1P(0IO%|ITFiUvkq*V7~(LqVnLsyreX(01qJh6^!t2HolqB1aLn=*{^^cv^;Xq3No`*1)0;jg7QD7b$OW;RZeELEGyHHr%pqj>f#@o*8EYKiTCEUDl7A&%1B0=a)_N^yglP;Mak=Ew7h*qa9mz}AuG15EC%~jW*aElT(jE1J1D%L-PULs9_PSMa`3rqT$fpq{oHmH^=A~sRYHs>`&GbkRasAg|BXGpWm7K*>~HDiquPw;n|t|!eZ+7wkLwyfMoeE7Z{mAlywR$zzOouB?&hoUcylj^al3zz>>Lmzdj|$t)<b09;Og?!kPz837$WzB9aXpQf&XDfM^vFM(F0T?!XWc_Cvt>ot;$PH^8ZKhU$2H7Ub|HO_VO8zIfRY}*mVOQ>@(Nr{G)GfoCf!&UF;kF&nW+l{n{6J|GGUt)b9KL@9_V*;r}|~|MQ6ZHz4I-a{$KvBDu7)P{#t!gMIER_4GEg$IR(Xc{&e->&=dH?J4JmAl{$awo3Uws&mx1_Af{Kfg6_1mE<8kC9vYd%73{#@y{HfWku!R@c^=7*f;#Av^4yu*c>2*?5Q07!F)>}<v$VeKe0K~!dFH^iMMHVOFz}vNJq%;!G7{>{8QtjO+Ju{*gmec@d%SIAP;C|L=PBeJz$*8O_CzPKYrh2)C2mv(+il#DDPEEu}^PELhMg!LRFNpP0LGC(~4lf5>!!Ao05C{8zC>6h<~3+`1hHt*9v-_9tpuir$>VK)|F&-R7IKHx+3_m2>$U}E0Y^cZceY@^---`soCIvesnoWizeF@WEt7-R9SL6BEN>1UoQiz+3<j@*z&R%{4U15>8;DkLU=}+RR&(8GT|Xh+m^FlQBL!TrSOzYctchjz79`mPhY7dEAewy#UsZ@PVdC>^|)^AQO)x2gII605sx?Ex~7MZa<A%U;(K8{W516Sko_){ukv4nxWBGf06Zi>wjeLx&is8~i0mf!V0s^P0NRgO&c{z7#_vJQ-!UM>+^6@3$o|2h@PJV8Z*+utfQQ^3V8py)8vZ}10sbHUlZpTPi<jsb#d;>Ni+>O9ZJ&?qfA{)D`G#j2-^TynUoz}#&ad;4$^Ofh{kKgI@rRH9jr#u$dHvKTxv_7xTqpmi^(lJ573TUoR?3BKE9IH3E5QB=)cxRp`%2^h;D2+T<^b#ip3u4I`MO61a{%@L4z0~au9KtlB6byKNO9&288fh(1XM8mmnHxHf|~U~@Nd|s4@^@2kptlG+~Gf^rR4&T$J!cz{HH{^`A>FYaZAI0Vhh860_%BrKms(%Lk|0;_$T{O{$L!^{NG<az|lD5{i#vDrXS$y37A}fuR9))qJDtisU_LR?}uwj3op|bXl}fKVh%tL81Io6B;&txY*Toh#((4i&G;H>0^~pwSR=p(r{Ld(njDEMn4hYugjiEarnjmnGo!$M>&h}Ox{}Om0}sG;7Citt!3^Z1(^`~Q{#h$bYhh}IdB_pcVk*dDaJdBaa!$u8lHaL{tca@$_KgS76POodwXJ|00r%rG^P^1OU~-3Ys`O~1#rVFgm<pOtEXMs*W{i<~OuNd;e?eRot>=roRzsYwrg=PLy9)fT1M}+{?|T~F*Puo(0`pqGcQf&Qbyq*+``)rr<31k8lYKw1?=Ne61<K~$K^phBfyrG14gY(=GMOjS`}J{{9Dw-R-a(<@zdGsyXh10Vr|1n~a(KAW(Gd}HY$P-yTy=b;k>&s+!_;S{;orPQ8Sszz-v<2eLoDPRLFJ#)9>7a({<&W0-K#e5zo_#F|K^%|h@Ag5k9z+@8}nH+@Vq~=&$^%dYyAJ}*ZKeEU3u&DHst*!@*?W}7kD--bN{_X+7BfEIv;48<$t@a0TBPW=EL0s$WagAI@CkR^|=P^KuNaF1K7DDUAE<=OL4|j88e`Z_?3S^`DYiPgP%xpgGa!9S^Zn24@_)Xp%nj455UDgV<h~5{HH4aUO(es<39LLXy$cC{uAyL`#ssW8i#s+oXz)5&JRT~Kk)DX4<Aqupg%;Knji_xCqwKPj@2tYz^jx8I7-o8Az0^sV>82l5`6%V=?(OVNs(1#GHRm9t*XcrRb`n9=2cM?*pIFZRRQ~zWo{dI02PDB@PL^v52z^9m?yMgPEb+i;`Mpa6=YG{N|LGUgUh&Ts{Bq>;Q`fTS%)fG4=h0run6oL_Td}QTzCLA51QYq4Dx|;D#r)XTbGkX(dFR-6;zq{Jo(SXcjm!k3gWAR-)gR0zNCk@tkw9==O)${^&rnarj`fqjO~o^`dU}R`-;xK;NKTM;D`78@t!|E=Pzq|2FfPX;$)v9`+EkF`7p2_4(8q3PvP~wgTrL!K*Vse?`RP7gK)4<-fO6iC)ALWkoo|0JR!n(z;N;(qTcNA-@1Mc*^Bu9Urzr|^|IRoSie);Gl=#4n^)vtuU*vf0;lh<wLWwGk1Y2*V}$uW^$(l(yZFD$_&@G>^XYB!!l5-f4#*tfxjkTiSAksIzEbA|aW2q##QO8w@|FLmwFX!rXOI(|-i*56@_!V0KIfZr{BJ+F-@P(JcI2D=*tg~`kX7m6zke4!Z_TOyJNTF8->Ct}|G4IieHFA8PI6)*xoBoRK=S}M|Au{!_}|>ydO&IZ6Fg$S!+q(vZ=~!)ju*INz43q){fv3cdVsls@c{aR4`RQ!<=$JykogwgD)j&>7yDWpc-ip+ctCUW-<aIo^a&>--<d>3RR!bKWLj%4nc5mxJfGg$Xa<;{(YlJvZc|0(#6WE<|1nkJ0aau^UgPtbs0)}M%mMea;RV#3HkH7BWyxq;MV54^D$6<|=Er$UUS}`Wa`2yn*RqiZWWXO5#Z;7q(eRC^3Q#$j6=~JV_`!VC8Vg!iP^DWfQuZrK20ptOK9k*^wFQ4pRax1&8n~_|MP0p(z&ztS*=9^Hz_k!s5pUvqerIop&%t{a@(wS62do71E4%oq3cC5r>K*~I7VK|8?BCEQShn^LLF`9v54QI^0?!AB*N{WQYs!IP5waiJH#7qGM99uT;qU;X-PGW4eeck4vR)IMLn93T)N!k$Bf?eN`&j<Vn);voA6SF<PyUtp7fs!-wf{>-oV!oGYdzrYXI<m|u6VEK5Wi_-y&X6F=PkoL<#@r}=Kucqv3z)OpS*H(qr7xvgZ2Q}2fPYh_Q?G&*xdga?la4ErM3sa{U(p0{y(;k^MJDD;OZqhPkQ&tMY1z5P0wWB2KJTzMTURBau2%df91daLpsK)H9&LI%VK{k1+{N7dtaz~$26{_<7Z=77n6-<hX0gEQv+z7P2O6%uVY&n4;XFhe^<@l!uMw}KZSfN|BUyg<?yffMY(-|ylY<2+SCQgf0P|V(0-z=LBKvW*7`w`OJ3?<<08FW9>D*mWIJw{tUZFNV85zNK&+n}<t5XitI70eFPYxPOJ+bbqpjLhlUdPKWmcQ2V7Z#ij^VY6YA(2+-?kcj0M}T0fboM_$Oq=IK0t0jZ&(EO$^TL?xx5pYw~Q_C<Sp49y<{nTf!?qf_oTx|7BIhvs;Iri8Li60Q`lpypn1dscmtIlL$9!kHOh!(4pBwFFGu<J0@L26Pfzx{v0f+7i05FR%=7oiM{LiH^O0OU&Wktf=aYYOZ&ldM-+F+*6e90m4Xx=NEF1cg{pu#42k(1T5%ScqnnojP%E1w}pqjEDUO*4nJ}3<Lhq?IQ>F|&D4ky%<Bg%g*;{njggqki7I0An-g!{JO=dv!V@d)^5{ZIap|Nj?ay6(-PvS*4hmOJw72{>|MJ<lQiyXF4-TSgB1Wd85(%1>Y0GYd2a_!a(t`b<8)c1T`3wo&H+T(|sR*|}2rzql<=Yk*5!N4|4asc}H|0M2YN;{fFUU~!i0TeVpBkbTDdocXdPXCC;+b@_ZLS~N`(2XvKy^7p&@f6V`%c+m1+RwgtpE2)qf?<%iz0Y<?CI1gZyj+IxGM8<#S0_+P!nz1$0V|Uj8uCc!s=9+YSkKZ%iYmC1W_x5V@0mlO<ryd~lrZ3=c`S+8gR;C9zM!m*(0KI_BC)+wDrKPuuV}_0gus@)A03N5d<k&_v9Y>gqSkHJry^W8|i1Cq`G2Y<a%LvbB+2^y`SRa7qSj~+A_puQ8pO5Q;SYE5*T17?f7q+b`3-NeS`)abdeO1W<>)D-*mUXNK=Br5-Ue83Xo{4&WVQgh^ZF&oHkxQ_*ID<V0<PoeRs0Da$A##ev_+Bcrtx*<!R#rRY5*?UNRD;K$zKA!meHF*;*vIQ;V?A>F6`jeuuMzT*+&Ew4BM{l|LVtiybPvF10>FNNtOWn7a8=>;;vT`W{>fn3(hs%#z%bc`n7$j@1NO=P{$aJ`so_@O{{Y#i;14^%|90@dwO<%&2I~QX$bU^a4DJsn)RJRh{&<4p0l3l^kPjSW4uKkC6XHMfS@!=PV*kGZ_+PtBzUO&C<e1_-KSz!ixb&LQ*Y^BzC(i4<{!dMQ|HG$7%D-iw<Nw@ifc=19<^L<)ll|?}Tz|Pit{*6o=l86ZE6DvXgYV1mf(zTp|0=n>r&!kmkpBx1a{$%=<o|HVQhADNfx!MQ)cf1HCN;;dv*NxVxoNUy@pMTU(o_5^KIrQIbqrwqH~dd%T1LmSlbhJNKxDsBSxL~jK=6QuoHJwc0P;W9js-aUr#fs=t$bZ$0L~boGq#s}8~YCL6QToEp8QiOQSO=`)uTT!zD<C;FQ|2ZKiD^82xOmghbYH)^mo(VptsfsX3WxC`-J@6)dM(>poJON91r#<w)U2((cUtxjW6Q8FIe}JS+Tw{JC={F7dUF;jn}+Xv*87^@!H&22+tR^HCout{n+>bME;?4@K0r^4|qY<z`PfP=N-tu@ri{o#y4h1Ro31C#~j#Cpa;y2uBd&7g~$&U#gcWCGdRlb=+?4M@BqYq#&YKGtJuf0VlGd%ncExQ{UitZh{k>ua(>kI%<+qR1fs?dlH#60U_J;rzj<8TGg#L34v}?Wa5G{*<N2;ZjNvsjw|B>Xa1ZvM8d^i2??wH;6Z|Xt{ljESe{<DZfbk!nW4&+~%o`7Ii|il5d;9Pm`|$JD<L8e@9UJb!|B+3(W-k!3$}<9Z)`-gH?6*r_w>ka~9^5-p{y#R~_jk)a=KymLurFV~3;*Bf8O(24{yF|f{;%#XkSn{u|IR`k2e`azm6;0!UErP)yNZzioBE&Y#vT55gZ&-geOt}~XuhsR-=4ofdjK1==g7FBz4WXiSN<Ph;{U`ZoUisMxPKHme_2UvSXM?iEF%dGsIngXBmScXV1JDKkF#@u9R5|Ty;Z(?r3Z}R96md)M|raEaPQ*ZDium0|9A2L+aFL5fSlf7QY(}Hk7?;=J;00)I=|02+bbAPMH%T>L8RjWX6%scPipNWQxNl~g7fLnj97n}8B6{h84u7aG{eaY)C1rJWWTKs;=j@Sw%%YJYDXXN#&a)yJU7<n2r<S7(%QMbARUj>=naV3>Jwm^_ss_5bD~gRw5BTS_yR@t=?%;yGRe8t5k|}6jFyAnY+Q5LUx+ucynxJiXU^^ihFQD&Nj~#`Ti@r!``df{kuSJo{OX=YMZLg#Z^L|XuVB0e_MZ%K@xQSj`8WCd9%=|Y0KD%V238#nHDYeSd$%CwZ|EC}&yxRe%>j1dJ>JVa;o$I^#si=uWS`fO^pG0rExYj@Yv4uXKcWoy=lnn9|3^6gpFMu&>CEB3{lG1rH|ntNsoP_IDgHmUHNV6DO*4+b^GTTl{3`$d(D+aOUp}%?<Nr1A@ALq!?kSSX;9utgS^jyxr1HNF{BsTvYyXlZvLCU3_sTTc!FAQk=Nta>)3g@Yw>nEU<;;}{Bc7CiiVu~l|Hst5U+1MN|BW8jxxk5Fe^kRqmH&|r|F#A&{8!RG`8d?ZF8*72@E>VnY;vUWfH5}yJN&2o0{`RN1eIbxDeC9?nGe|BL6V&hFs7v={H#dh38@@61oIQ3Av~VId4)Qc$j>!*z>(ttoI^Mz8vKLp=`p@Cv#r0(px^_u)C+u#V$5}h#(y7^7l8daU|-{ZTVLe=P&;@4w4klI&X4ud_dV-{IWeYYm<!HTV0j*{%oXOx81|jM!W?=4`Hwc5%P|MspV5x_0%AM42eY~47b4HOsO1^QmxBLnyr0XSJ=kAK{(FFB=JI+)Y=`pU1I+0));j|C1&H~ppyFO3sP7@LUW7Wo7+3Ponx9(xB-sy@ZOr8n^U3dSTLbKd2T*$$^P$~%kDjm-`N6gUsNMTV$ojrv@BqkqfX08^b6|wg!QpQ0XD$IxAp3g<n|fhw&)fKKP}}g&aes~f_Ke|weS|vxmvqampKPwL?0@cNpWnqZ3wTBu*?-5JQ}olPFW!~^PnrLp*{1yK{2%53+y}}T2i(2NwH}Z?z;oLR^n59vzkC|G%u%q<y$<$~f3UtC{O<t&?EkS3xThdP_7^SH{Xr%t^p(I$?)<-<uSg>JPi;)@A1cLvLW4(T6yrZ?fKiQ14UmZZpL6LQ{yBc0>hu62-8}$@f2Rf*<B|VUNl~TwAO8#NkBRg*YGvejK(ZseMkOidev-(zkH=%2xx_ji;IDHAC$ur*JYw|$c))nuGfbsNL~`zs^?)cdhj=pi$Nke0?`O32GdTb}U>X>o7GvrPr!Szgj~ySF1McYq%6?m;x%fwCUNAe>)(g>IG6QwSjA+UWY<rpB0W{BQKJKIV+U#h=f9*3?HSD)BJw^5wGTK(tx*<D`ock!>`CxVh#Ckm!xqgnue{a?Dc;@+j$~wh7o?6XZzK8LGLU6tUzOWK1=pLxCeKi?>(!}`neT)aJ#kCl(mEdtn@9KCT>Qi0W-8PW?*N`0)nBNKQ7!+YxA5_D>7opmQxV^DoxM3fk-KYu&|K|O@Lu<Nn1?B?#E&Dodfa@OQ{(Jd7_*@D8J5rmL=AZNAb#KtW+x-7ux2!&L*YbSqc!FmR;Pm;({jacZ)(KrO;|q7=|Ayh8J-{0W*O(pv#{u^jne{-HfA#>lPxv|A8!AsuZCEZR*XPKwHQAc`@8iC3jQ9BqWmn!pTo=i{Lij>amOQm~nV#7_WpqCYviU#9|M6!iqaVLdk{dn<?jMw-hV%gD0j37vIv}$q#>|7`e3&sh7FZFiSJ56hYXHu#W9>}-6I)aRe_p2cZ)tjfp0)TG%YTaY>^ypYWWRNwjBOPliC}&#l-SZ}tbOkI05#TA;6Aa1RZHUqswl(%1g;x^CZbkQABZ*{Fb+y-P5w=;z<gyK{D3|%K8kAyd}T7~etN)^XftNPbD!GR@sYYAK2kf#M_SjkbpkZa%E=36#rRqefZ7``m>J7@!b@3aUvWxQHJPg8iF{s7`v^1P1#{Rxfac*|NAsCiz#rzd@p5^B=_h!jMu0kaOJ*k@Sq852*oSAVhE_oNc%F;bmv#1&rKsPjZ1(ly$$o&wb8=loF^{*LuSRZP*pmvDRXxpfvakFjuP68G`c{{cCqt!3^8ok&_%H4qDkXhFaSw9&f#4DGcsrQiKDZ_rkI>gP4hWY`6drHEYg;LBzOi2fzLOrn8o>C$cHBena6Dk|Z62^^h^Z}DW9-0plK-Oa<iEUxga1eF#Q(SUoMEp2_ms09SnL0HOm6SY?NPmJ>-~4l`NU+OdB3uM$(~U_{%`&p_5Trh9qiu(`^rD#|Nas)55O4*K-}m4pcl9nV0)pi2RO6U^#7UvALe>raK5*|^#Ar0EYjHjR5AAj$<m(ZcDz1ybYJl=Z}=}``5#&5&yv*O0kHpo<^N%*jEvPafn{YZdx4w>+YJ0SGyLnCfEJZ?oIC|N0Qn!&+;Fe_+qpKQ?f73pb2A5kV}F{PJ2ildf5v<_`=h~p0$3i|+$cf0wqD>cZ*v338%EprlgPjI0RD{eWWP<I%LAPG#2ha$>k6G7f#U&w#sjoJVAeBqukS0(BYdPmsE<4rWVrVf=Nd6rm<(<wxAAuQ0P6<R8!)0LOvn3-_tT&$9A5zclaW77jWTlzW?1&;#QLaT%wf&Y9ytUUck+k@?c7?3_tM&Xt1>!)Yw)~`b-Lw#Ww$^)4wSsE0g{7QpGEdN!2{y_!N0#O@50z_7+=-P@LPn~%`2ai_X6YutN6b4fD+dBsLdTQ&XfO=C&To!<e$&iAcn68yBomtmH`pU{U%(=`}+Ro^^MRbYCsLu7HFe=ou8%n+V+7pTsZ{uhu!J{HC-OC)9Eb?rdNb(->?|J_qb+e{tx+&sb5Ri$!KqmTs!=K{}=i3yN^Bg{_q&f({X~g-1?h6V~ppNbDfXV@B8qki+w$#=#BH{T*6=F|7-c=@<Dn1#3sYP_WxIz9^lRb#D2~LSf%{4_IJ*ayL@1yTt2u(E+5(=&+OeG=XUcPnnF3cK3@)&EH!(ff%~VB_jA4D;q|%7|CAAZ#J_?&{v&*V|9d5w{5N_)QX4%YDUBb6I1T_cEoaug*m*!H%6=92f$0H^X;vA+bFQ%k|B21550HJk7MSyY%(`H2o%^GGIj-@s{HI0*DErAa-%qsc^K3{XA9#RoX%C1j9siSBxqX1*^ORPmFEGw}fFpO$!1fFy{oHF3TN!^E1<&dJgs(IT`#I|_1^c=lcdh3mlfd(2)(oxP`M?ygPEA$86W|e3@cyJIZ^%m~f%{33Ug`tXbi6-Xdx~V;SJFBdE$V26$LSsXaD_Vh;XOY|?`R%p#ThLl(_Ibo<aPye_B?1g_+N_m%zgf{1mg2N+_#cBe=p|l!M2vKu6h2d-iG&;P(jZScmzD5XNcDMYx;!ATFdwPetgV$PyR!txR;6jCG-Zow;n8S0K?R}{=7c|pEp|D_s$-$Reb=@p>50`Y@Oig0Xv37DEGT;Z^7|^?TG(dkeg`yZ&qH%{~o!=^1pe7ITwgIy^i7Efqjk#{OvtGpXaB~!T%R;na6+S-T?57ukrp@@0l}7dB!*Q`+4^{Ge?l~gI_zZx;y`0D*rDZU90?G2m9B-KKXx^{Il=ByI3ymE|SZ8*XbD&&mY|>H%{%97tS7#7tS4)n@=B>8>bJ*l_R_4{Qk{yYI~9HQGaAZz8qe=Tn=)7u(jFXKTReK?<M|a?^FJ{3IOG#@}Jb;ez5;A^pLJwAK$c$YklkZ=4QQXGS|N;^T+`>CrE37$}+lX6{xDq1JnoXo&YZXxi>&l!+&B+Usr5a_AUP!`#snn*32#A1IPv79iv+Mm+A|6dO(uL^A!8tPJfU;i+<zGAz++$_-D<~zmc!R)%BIg8oqao@s4UVYbrshQza}e-t}6;P#;~7Ji%%r>V`>CrhiB|zCe$dh&+Fy@(;DP^9xurP+BkeA+D2qf3WT^8F2x!xU-``S=`Cr2>fO`LfluTJDZrE1OD@D+|LL5Iq?Dd`eNQkuju5j_h;d?+^#16uj+-k{iM-q@W0CPp5Hwf{$Z5g1EL3Ld`Iq&+?--8rxz51|5eNZ=mWj*ysuFS?kRy!tWibaz6kh4xN1$G8t?<lI+)))s3!6Qc)%dz0n|1;-^yMA`5$8Y2!jp(tOv+F#|-sq=M2&V$UoO<MwG?hJNa+;7}&_S{F^;Le*DJH)6=m45B9&r`=4^}FYx~v`2U>y18{$kTUNODtM|=&oIm*f4f)}%D|$u=&neTh2DvBs-T1$1_<!*b;{V>&W-kD6PxiSdu=f7;t&wX7H_P+KcFE1t`{mUO$K~}aXXMRm=j6@jpOH78yCAPU`?TCVcT}z(-v=MqD5rLm=sxv_*24qVEz^DfCk*Q;K4t!_{6FN1|My4=;(toR2W5Pt2bKQ`mj6`F320u<@ZZdX|0YJGbw3bK4`A#!dB7<0-?W<YKhnnkQMd8$Fv{4^ynj>+e~tCSn)$di6e9ng?DO74k6yr-D5J3)2hiSvGj8bkOMt5<kR0i6gwG9X;wQ0>`D$KYFXWe*uT?8dnXh)}id267W2yM&e@WGoH>Fl+fWH4&u=(%u97Euo$cd;SC}%D5L@UF-@dl19PDidktF1p`zP~IW_Z`7{ToBj}LOvfP+3~?(H4u*j!8kl1jy}M9jIuf#Er~bx<a7-J|3=HZ1tazc$+9j1>KTT8+>6gI!Dn*e6)U^5=4aeT>_+@9>{%W0-e`Guen*hxb_>DxK|MmGfbku1owfbCei0ha*CBqd#q;7m;l=~Nc5!bjJT9^EzUava_(!;^sCT%&M*i1>|E+`A8>l7Qslm0>3)Ba!wy}4BTtI7qf$qK`x!=J#LtHa3$jlk!c;OoOl_&qvjcUu0O?hUYj<4Sa`|p+>^ZVN7^_<(Q^MYTKpZ>z$9vJ@&{C^4Nzy1jN*yt<xz?blfzpw^)S-yY!dHMd$D|%+>Pj6p_XT1Dx@&C$^HS*HowaP#D0w({LIR^+ezw*C#t)3ru<M?iQ`P>nC{qiY!`}zfW@1?8q{wvSR2e00c_i%md=CktZv*+ac*~4=A@J>0ibB&%^acIMG*^)b7#tn7z|DcWkBOd#cq&B=?*&pBdL9qX*EB-70&B|#GrE3A2+SuRN%ms4uZ{vS6vTyPL^6%sTBW(@9eSOA6I{dr&B#WN>54GG6p$9Pkx9mIIkG10fdTo6NSszHUu|CPx1^kX7P5q=Dn1B3Nc(3_b4XIhbt^|*rAk_|El?w0uAQj&K>1V20_c7OJ;_I5eVJSt~^~kMkeh|exz*i<m`)WQgy^XI-i}jVc?fhh3hXAl2AdA3uX6GQu0`p5c8!hc(l--rjgUtJQo&JykKga;f6g`6eunhO+bhQHi*<FJ4y+z=D5xs@?;^!>I`?=jiWF>fJOlRI-*gFi*t0UHj=x6yI+5FjELf`>>9r*!se8luMeGUKAS};s8wv+!=Pr4abFX$VtudPPBFX(OFFUI3_WPd<SuwF~~W`A%8ubv(-z%v(!kgeKJG#)_yIbVSM>zrXLdcd0ALAp*``Dg#HA;<r6<p=QpBj@LS^`?8jFZS`iddKVm^3lsiVETs-Uy&a^dR>0_gyZ|n{ZS+Q?GxxzBR>D)ed7`Q%=;Vz;Q3_mn7_Sw*ZTj{Ye(hv;~O;oGyi{f*GhSIJNE<6lZ)H(<>K}N<p0G|k~vFOESx6&;@U`cpNdlD(LYP5ZzUPfBTm*7<jU?X>*d2YUV;x?m)D=WC^wJ`Ts*j4PHtO`93WRV<;;<BLwbmJnR{LFpZq7(zDH8I2Nml7aqxf%P0Zd^<C>JQ``eV&xxmRy%Y*y!mVftrVAcSMi2q}h`>K-IqN?TqTysPIo!(zUbF&_p^|jXCuK9fdu3rC0TkEstA8zGh-*TVA@j(wIw|4u0lLu%nVC#V4&Hbf&eK23gSM&5=jPW(Yq<a4$5-@Lxc<nqT72o*B9hvu3aPs6*H9)FegXkJVu$tJ~Pc;de3ic^-&-ny%5cA3YJjQ+SycpbPlKuGVV6Hk?hPqbAbK?P7$OjgK|BN_tAE>!Pn$==F&%%34p=Dh|RP+pro{`ZpP{sS0L-6~ScV~`YT~_oARUgO$-}&9mXLG=Oc6^Ak&*#hmj8}x2cy5_r*WYl==S4RDuj*s?UritAg{ysBfVzMlL-yD9h49&p0}bmthSZiFLmxwqUkh=cxj=2%48}JPtf@J|hW@5bV84L;ZyR976j=N38XBQ%3plr6`(Wb%CA|#)y8e&-zecq*9)AD!HTm(2*X2jl?mvF<h7q~vT0r*lKDdc{o|ChPpL<JkC81Beg!@%i=ED7}$jJUZWY6Zca{BN-F#Z`l0DkZVa)VFd5r09>aO*|0j_@Dv+_nF&{GZw&HxHD^bGr)V64-xwLynwYmn|nsGUd$1Y(1Z$ZM|TrQSD(Jzw;?~zy2<ss$VNaHkB00t+!s0cVE6CuU&gao<DO)&hOiZnx{av<jptyKYCB8_@7YwPcpvYeKM}$1IPo|12i?jIGqc~F@Q3<9)SEOa!mkzph*Qhwsk;rvnG&hXSnCySgxPpIDqy8z2F5l@8?<^t`BhL0NFe}xs`jY&x!vdZSRllJ25}0b)clS2`VMG2L$T-V{H#Hp`~HIX}HPj>jeK&56@4+I(HGDHG8De^-rYYtAD#I^PXaEP`kRj21sb(W7Z&A>0BddvK>F9rp9nSftg1zr+uKzZXY0Xp#@~KGua0FV16mpHB=vGfj^4gun6y`cM4M8nJ+BtXuN}0_6jmsH>iIE8L4Lk>-TE>*IXe;e-__cj+ma?-NbdW&wL=KTZqQ|C7O4b--Y)r$9;vpY;F&BOZwH+*uA!YP2&Z;BF-0*f84hkoEP*W^AYeD<P|DB*POuQ3~My+ucg{f{vq<ue1QGJP593BeNB(R?Ez+-kaGpL>bzi%2}bDJVbc#Z>j%~%{&PPI9skFled`9bv>yJ~o0q}<EAsuPugZ5Hz99eg{&o4=>(9!UFP)dKp})U)R`=A5Yh2@3nQQb|s2td_*?7QL$OHcRq0IyAK4RazbJy|zFRmY#x1Zjs`v6`6|7SNWlOwAa$?n{FvL|nzWTXy~R$&#TNl-ay5Kva?`j(MeUXMs<m50Q?`~yGxomONomJg5zy!FCWdGXR2dG^?DISn7%#{S=kUgBr@f5?OXaSi_r-ETagvEiTd03804z<*K`&H<>P?2l0|sED|4_619J?F;0k`+{*@5ZBiwLSw84=$-(U|D;yV`d$B@@z1>hMl^HS_XGR5kG;Y+fig}Nq;mKt_hZ2Q@D>5`WFvoRS;P02V|+x2)O_M`3G32Ld{=Lmif@1S-{SplH9)7jK4v~ql$l#N3EWSL_Q%y<Cdc?A<_D-|whNG1?Z|zgYJNxX3?>)Ffl~^+W^@jA<pSh4J&xQ5N*dy}vfq)PMeYFan8zzTAPsCUVxJJ)Qki6*kK+yR>ENHD4`i@kfV^NCYlQBhlG`Ix`OfZE-Q*dpFFM;?1pMc~8yNcw*|+O!SYL0owx8j@7<IsEa9s%ISAg?ed~be_Fv;s4uFA)2<bQQ<ct_s|Dd89&V*BR7R@MhLBOh2lpoV@o`-ATOLAdn+Ge2<az;NvY?$Nn}5oQj+5W_!xV14gk!~Y{@{tx-*c-TK)zaT$;^n!f%!43K5&1dCvF#rA~#NpF><h4_~W&Pr5|1IV=6|In;{{ES%1^)Vh=@qhn@Q=Hk|Mk;%^7V_SmH!vOKlk}~YSkjygr8qDe}c>%J3u<usVW_6Rgu^ll_fg7l0@P6iVUqF%|gmcegCrRPu^uqd(-;WE99M*ugj~~F39z>hveM;&9bLxiA)^XTYNbX>cRhL)c<B*=lit}pz*))LmK~+RAp5h2OLWeXyV=zfO7y+npe^}0J;{4YiL@Ty#YBNh-+@gs1JC%_5|S=8|TqD>wpaVewKe<!#}UwBY<Ob%oCy=_T8c%IP4EYj^7~km$?sb*It6gO%vaho2ByA5C3E4OVt3QQ%jEn*zv`Q(MFRH@24Q%PvzWUFh8Shpv-C?q+<M^+aXBif&Yb_f)T@+*9WT>Bk!gkP<(v>G{1xKi&>}{X1C)UWPj}&&T4CN2-Xn`Srf1}h&TShT7mp8!d1P4?^B&kzTtSpQp9{JtE=&w%(xJ+kLS$)nfrr(t=rkN>u=)rI>dgeq<<|b0+)qge<j|}>k$qw36q?zVX~}SDDsI=;{kX*AKp;di%iom@Ev5`!vi*gf7TQ0Z7oscQ9p2eaa~_KhhRX2&MD@+;k|Y(fiwTdtQ88A^*w`iFAH7&gFpMIdbnqGrhN6%8TtO*=j5B$FUlv+odoy$<O=fSv#6C<FP!o}V(!3>O{Na`@)l|V&Ix?e^cMdO{@*&gMXv5EmczwavT?~w$(}w|CJ%p7hDC)-pN2lttG>7Nc*0A%*K@0D-D=Y5u`2leD#{c7WlH%{{TjitZQW{l{rQV>^ZYTnaA=3_0X#XOkDj&R@E=?C=lrMG8o<m2Fmr*(e@e4*x+ehl!Axyl5!_dF@2AUoz{<Z_50qm0C;O%N=X@aD)B8^R`zrsVoPEJKkC*d%Tbr8SSr_E0`7QfnS_Me_6aGIRr)yYW0_J8()nm^~<!iU3!aLvnuX69mYyX8(H2`xM!+rqbet=Ag36yEzlbX>kP(5HK8HVP7{kiagc^!h01B9s3I^ha0P<1rkFt3BD6UaT+9&kRQTmD*4&;#b-`{@&l*hjEA0NH0>fxeK2_tL3OM!YX0-blToi;4G{nt!0)=xpl{_WgPy_V;FdC;Q~SmR^ey`&T0$V7;G@oPRmu{ZjTT;3X9EiEQK*%eq%rPsm3NP{8kDT-O?+hJF`CA6SdHzlPt}J6vlA9V_T%RP<z+nJ>Wp{=gcV_j4YQvzBN#a(?yzIR8iM|DFLd4u7*@mjB3lc)ehed<N$K`Nk#r6xUaHRyp$HJ*$>T*0f|96;tbf#2jmZ(+79EbAZ2cY~jOyEB}Av^d>pKeT8hznJd{dl4SCTzLMCdvn0kuNJ3PI=C(tc`N^QBr8S_jul}CxYgN`f#q;+mSe7BLA^zWd=D1uwx?2uxTp=?O2f6rX{U1|BWK``xfd6}Cyv_gF15CB+fyjSqlQNP5aW0VVf!oZH;XlRhpP6ds0=wdWB<KH`9)QyWboftfY4-rpzQ19g8m(71`$-%lwDCU0Dp~9NKzuF$UJ)Q8kvBZq(9hM+=lFb;3vc~@Vf`0tfI2l@zSFs`pYBmUQFDIg`{bY82O;k_ngQl#IynG5V0H(30doP956tff_TdTig;~6xpXc{LQ&2}tfxob)pnV187t`=_X2jZhq62FL(<h+#n6<+~dVuB-_WL_BufY4A%zYVFnP8sEim$FcNV1#@rZsMZ*&^`A++S;b#Oc+@%URFog8dxuy|haxa)|2i6A0ds$^J#xQ1BnB@8|Uh(|!PZfvfRd^p4_wMkW2}2NCKE+9&K~JfN_bGgrWjHFB=7t{)ge_G`ie%vvIj{cRr@rgML|*AUk<a{g~9_y734NXt5*vOO;i{GX6-UVTR1Ie$<t>{%x}R%A%doQX0$ao~T%+@6iaCjS5RJ^AUgH&Fk-f7kxs7dMZ~%SYDBN!0y?3nqfmfik#Tdl~m+Tba=}N~ZQ|ArrbZk}<J0^mk&e>xjSKz$U)Z8^2FXjY{&E&!euqq)&W1dHM3was%=I^07T~a8rRyOB^6RHvf0?|9h~{`u{%lfC<hRKx4Be*xBPgrK#ByME3=1Rzdf}^yHs=>Za&E+h#AFv34GS*#ppeKr<h+ACTo;W%(!jqixMU#u>Y79q_YWf1=J2Hr$VB&i=mn|HYZB7ZKS~DnIwpZ_0nj@I)8?oJ%mC>zWYzCxh3iV0U`kAQe5p(+8+o?SqZLB{j#2`2q8UX}az(K&Ilm=qHog1n3$jXK&=m@R=!ijUF(wEpr9Nde#mQVt5*^3oPrJXF$w3oV<fdL*9{&`xx_?^JkJ>To)tnUy8U*w#omhJ~cFMujyAydjQP$3!vqQ>C2GcXLSja4Dg?h?@7n!GxR$`B~$r_NAzT0faCwBPhj{DR}Uz{_m&{HU~hoTvsXwDSP2#M<Q#$sT@S!HLfZzLbp$(x)HLJ&oCgH<w+;x?y#}`Qp+ck}-e1Nbc7;A<&ID{xJ4Ciw{=a<Tw7h&`ha6m&C;1De$)tpVGIdbr|A@Jf{ko&}f8UG)kblk}zT5oYs|V%z{YA33AWfD`9V4R<_d7*3m5i}ZO6J(!l9td_W)F;&ah>bSh{&K)zl-zx(39dHuPRN0k?T}>NUA=1pN<jUJbzrr04^QfW%wW6U%a^y0{-`a!+$Nq|M&*rzXADwK*l#V=Q5?z2bz|_)y@ZQVru}ze#3udQv=v_0OX(NY>aJQ6`Y&%fyn*{o(0s@jQ=}1z-Y@q_W~O2@XzPY{5*~gc<^6}{T6Qahc=^prBzLL?(e<j$ZyL3?KJ?$A0{GKA+yx9cEK{;Y8sfIrk-HL*gmsepxY1HnY_SxuC+rf>xclYD|GF$@^3sq`wPekrgBUH-@{yD9&-En&;rDLKGx@m{d29a%+?-)xz5x6L$KBz^b2K{9?*q3JYsf_P{S?QWc**%hxvX@DZrJl<$_hl^-OS|2H#M>!FMg7|M0u;I=?5o8)AR22+jQq`CVEwL}>n?xd7RRR`sgk8Y5u8f#O)f8h8bJf?IUn59bG%wZgilXpq?_WFy$$2=>=<-+|u2x>w2QhL1`Jg57<;6KM+mIS=M9*N(~OH%`jcLz`t&Zko(a9Vr94cap_Pef}fnI7X=Ke}dZoFRz$+M0eT$>(-Tha(UMZ*|c<~%pN;P`gUq9&7Y_xtLG(4;p{PzJ7t(G8r@wccW;Wkr25bNh+_O>t<oBG$>Y9{>K-5beV*gI;NyGb!r>iqV8hBg@}F4ykEQu{Y5>j$w)=u4>lk2}Qv9df#y{5s>%N(~M{rd=A1t9M&j72c=Yf&`1dac<*8qNIzYaT}H~9|yJ7fG#&VM`mRHw(?{l9=Yi+@x8Z?6G5)g_}ri2p(0k_;P7wK+UrpJFw&t&vkFIJv^~w#FxzKk&V&F@Z8EI>6Oe9N*gPeKbkqKYhmZ7U$T$g7y|%{R8$IgYjCh$t$e4%!VKF^|^R`L0omj?do6_0{a<S!-q*W^7R}rPIes+SP88F$9Z@?8|;&DevZt~)Bc4K^8n`lOS^_4N5JQMu|Ej0W`Ouzg?*fSAl&2x{29H>Ypnfs?y$}=uA%FOw(1-qvwoOYt}Wi!KNPuvvlcm6*Zmat43y<@J~FCd83}&SoFm+<R*3F}_c8eY<oOeFe$N^yNS`6eL;FaF*3JJ{nEUZBugQ-b|NGFM1NN)@e=6^t-zAr}=g9huDKc$DAL$a^OzK92NJ+*t*_=IF)-IZ)zYqOrY};BgqGiw>bCTW-yrpf;%F;Nnoc1Sc2UM0z;Qr#F9diDu&9Z-Oo=hFpPrM)gy^H@gmBD|lKgzfU_qsfQ{ExT%r!;y<djJmq90yEQ_A7XJfH@n0d*yO25c%gGft(YV(9G~Zya~?(G;07H{+;ta#yD$mbX?Hx^Xbgr_255Idj+Lq|M2F%F7@{40fct%B^BTJ_W!U3@Kv^z|F*#@<^be;a!jCVQX3CxkI?NCPOjkc1=js~Z-6-iE!tyzfqjNHf!b%JXE^zT@c_d%b9{2{)E_h28qKo&lU2rTDjgiBcQGE2<?zq3Jn);}yQV4+F**-<`Vx)%;m8}p)Iaz=b30PiWg-0}J`^<p@`E1m3VcUiZ{`sZDtZ9L8iHc~P;-I4ZgDO##}+vcfOCM%{9t$d=X&971HyEVk#$_V&@)8$Fe#$C`GbGdob|!x1Lj=uW;H`()A9ux|H=Q^oyC$nZ;A}*6)!Cs*3~`o{uAcv23M2s-hV;9|KJ7r;e#7yztFqv|NGv#9dcpoa@mkMRVEMbEgf1ml*hx7yG<M|``0X!odt_zZN_BDN`6u%b#Ef0+l0G*A4fx*`D@RzW9=%^vU&xn=T}Asc8Zeo`!>tddpFAI-Rope@iLh-@=5V}=y!ME{~oY!dH^~P0Q{5vr1}ra*oF`5THvvb%3A)(zDEwAXXaFP@ju$m2TV}ztH|)imjA}pbR7`a19<YE<XVGg_;<|<^x$7(e=C2}?{o2QxaYbcit~CKhnXCpX2beY<>^;{+Ztfk=~6Yom{wqt?6)%_|MY}OF+sW>d7{-M<Oz-sOua2%n2yg+X=`4ejL+$3qU~DdHfBEq%6LeS<_L}l%%+^U&;9~_KKldOf5i8h$Lu@6A1tq$zgwjvrY}a^UJAySf!}4|pB|9MEBIdyo|zwHqUKMJ3q{Tk{yUi5pZ$ll&Y}8Vc6Z$0gP$SytQ%^mTt1*Z1ojq<Tw?<LjW{N-iSvV@t%GZ6{NF|&;2NQU;j*r8nAwY<x7o9R`;M~jw+hPXh?=v(!xDTy>VN#bH$nct5&Sd$fBf7Dxv*!gET20?`giLn%^TE}MN^aiBj&~p=px_0^Su1<-gUFb*jIPS|G)cK-Z{5jF73#ZElZ}!j8T21Q>#W&CoD+%b&Hb|J4)o}*447NXsMK>O_20aT{S03YFAr-C*~%jqQkUC;l31I9<L^CA}VQ3k~Je)=l`GHy+)32FOpq_nKA+XQi}gdCjTe@daiRro=1D1jIDpaBsF+Y#?*g65*s{(8tP%gf8(-l{y7$){8u*dpKF7fSJw3ahW{#HzBK=?8o=_;J^7q{I&?3eD35(VZT_FE^LhhhG-7`ObN?1j?Dw;40{vWT1EOoYYXG11`+xfypmv1o=f~CalgZ%M(G>7D3EWKpqYnR!`^E>1FHDIIR=IKp)(9~swok@A%6;oV(_d`u-qXbK0Qw6(gRE;$5q16S_C_<?hajh5{14W9W<WFTeab)hU5NZYt+S~E(zSmd=JJ4S>j61Xb`P-MJzV+DfYQMKJbZpG_@4*Ok29a=`^)jU704Cxdpq3M1oM23e8S}c;VQ;_Gmp@WF>D-Q#Q8vs|BU-vY!0w_K)B}p+<S!k59z){%=>%52W$?&eM%Ei{|7x_^8e;FgJg5we7W`P5&7c9vs(W%{~z45vox>&xD4yp^FLzl!0Jr-*Bh7Q`?szb{=d9S{{PQgI`(%F@qc&jY+0B*SbDW<sr+~A7%RKiu8?yFw#ms|>(rN+mn<FEU*-?%C^P!D);=ZuDY?VrnvZZ?f_~JkadpX?H&M?5JHEA0&jRK8|8YZmh?iOZuYPFa|L<j7eZzlpy?bTs6Zc6XG`b$xuXn$Ue&RtH4G$gN;1M$qNMnC_Jqs_jxyk#@`5=`L`^|cw1Za5U%E~`A6dq8Tf4e7GEBAiD$#%`|zr+6st`W9kuWhuQBiy}#pNqes$#Z^l{;LlfUaAI|#9H6-PxdFq1nF4j_%>#rv+-8+fk`@6V7ONw;OA@&VPb!f*(<HJ*&}(p-7krv7fgx{)H+~l4C6d`HzMD&+nLwsCyu7$XHYY>24L>a93T`PV8r-MwikC{oDbJHzN|+Ls|Z=zEyD00A1-N~!t}HAJ24M1pIwaCmvjwRuh709m?!t@0jwc<hr6_*cewTeIDfE&V~d<uME(al>i}wL?!VQ^`w{!OFW7qI{l$#?+^YoKujc;4-2-*pe@(9d$&dGv(e+LI|Fh=*LCF8-$|u*3%U@qSC)bZ|lMOiwC2>GcX;#0E^y}G0_O37ZA2HWAvZkEdx<bC=-XHHgYuEqWW&ZDXAL-s7mv`jKp~7^@o0TLZdv=gUwZo)NC~|-v@p9?-KDl&kubkPpNe*pTAzPQvlcM>lTAwgKnLn(P{!a9zevN#jTfJ&BH+iUzX&zdeBS$vm%E5IxvQ^jrbuZ0-6*vE>P*UA{!Tg^kvF?2`^6~p5;qm(=p&t0J|B&UsjPjqVGChD&{6ixe84YU${u>$ohv?PtKf1Z$KdF_+p1@JgejNcW5BM4XtqlL<-QnJ0f3&Uv4v@jk16})ag>~sJm0tP!H|5`Z>#@>vflvcXV%%rl5B?@t{>OQ+OCQjDfH5BTO=;`y8904HWuKoT`}SNUXI(SDgI+K(+T;*ZV_D~iXuMZ-Fju}djo%w<>JUDj*4Df}zoX%Q0r_{-nLR!5jrgAh{+ELLY?m1SYsg~c|BGz=rw1&s9+Dns`U1<kact1UdiMPaY#u;y+(6aa%n9T;BIo;YKG9l_HG!KD_qPl*ai42|IsV7}M%MK)dk}EnQSMh*#68Cu|9S2}Pwq?ZFDnuM$DocS|E&L;fPe0T`SI1G^5x4<>s;Rh>vCmw@(5|uyrDF$_m~Xp|K$IOxs6#f<)e#Ff&cS*cA)Mt`psST|Nj^GKe<Vs+YQgzm?N7qr^%eL1Eq88CYl301|<ycC(oWbsK3+M{hQ_RrUI=`*5lrlGe=8SN+0c0(2oYA22L5!S@PyikPS=c$)1A6^3<9ovVZMT*_<<1QipUy{_oNM1OKCy|9cJp_3lC3zgI@qG5n7N|08ebzp07+&YnP;_cy5|qjW!jiZW992m1{x%TQjMIQ&=Db-?6*jIIMP{HNMII)91(BoF>g4&Z-B_EW&fn5aN$8ew|?_3KL2lP~<{IRN(xaE$}h^OuRShW`n;j*Btt7gE4#s?~U#4~*9wAV@vH(Ii*DAV?V>r+#6_MOp{AdL`qv1~9(C{C-AzqdDMu7Wkjlu{8g#F$dlcZ(#n<oZTgx1EhB`d}qP~mVkSTKA^cjJb~W88bNag>l<lcpT5DoVQF{v{=hr?g{=R3o8AFyh~*Cdz0BO9l{zOdTx);k{wDsL+F$nt8p!>FB6Lqs*8JROO!p-05p2&L;C`k4W<PT9zowVJtmuS16Mwfs;GeTzn$!qH{GTr$BLDyL<#Y1hr9*OI?>Z?=n=V6o#z~WUwN=RpL;hRLWsDmt7q%D5Tc>x**Edhe4{u%s|HuKpzDxf9uMc$Z&+AXEl1tn3<!JF@Sv7aOj2qZpVw%>II^n_U0|R<@lLK4V$b}=j<n*2m>Pg(w-|15>9MM_EcWW-gkvq&BJ4BYx9w+OvX3Opsi)4SX*#mUrve~8h*ZDul|3}sQouq*KlqdcqW5NFDx`zJ*s;=RGl#Bne%D=7$cKFx5fDHc$jVsFVh84knB^gSA|Dmh{8dcSCfRVQMr)Ts;`MTE0x%TIA#YMYk#~t`r<|9k9pUgP|x-KYKVjgqX06rzVfBPDs??Bhjua6pFVk~oi#{OWtb}3N#PjT@N#Tb9^>=%&#iLs`y=j@G~(%S5e?CgKQf1A{3Qwy-KFge!r8K*P%SMIC3>Hr;khz-^`Nz@d}|E%_=Uq2VIdtO{=A4rQg^*@!x`90k|JOKPJ>T2r$^v=cum_JaO|93I@Kl#r=4#3{Qa@#wgmT`Rm*9r7=$A9hlv;WthJ%6*daN_{OJ;(pn^)>qpac{!HUcpk>oqHSw$qMda(ltN|yZXZq{G=GUe+fKbMQ3kGtpA7v{Mqo|C?Wv-&yx?q{g*GDlUv9GUOc%=o+@4@X%iErYui>X4;V49uN>OG_E(vk+$&OMj2a-Dmdru!zf#^fxkJ9Zeq4Td>w^4r>#F?p?OpQ!e|;!#pWi7r_pO%a_7%%{)DQbm2jtF5meGAXOLXIU@_0mu)QJd|p79-IQ19-tWX=@Xjo)|A>MU8cV1i5-+)a9SjF!HgV<oYFSIzx%!T+Z0`LYZA?_HfCdsZ!0{wECXUMl`8|FsPN$qxUI->Z9qst444Kt@80|6>}KvFm`$egI?bnn3Oe%zXeH{)hb{|7K4xuK!PtGG_$o8NbdxfHs$P_)oeU|E)^LezHXVdo&Di@fSRP`ftv^&xQk~=K{3~QV$pp{!^Xbll-?fzb{4p=@FAy``hsXdcZ^{{@c1i_ePI4V<OHz>6-sL{eL^&FwOE$O>gh6C3p|(4D}WE8`_!pKL_{C;n-a#w-0C!Al~r3xSNUhtOK$%_M5sOy^E<4(scaK*8i4&zMj?9e1`pkoE~PpkU4>R0LK;R0X@y!!j)WGr27Mzb;X(k^fPM#xMvvm66N|I?o+lBF+Z<skSvc6gaRa|GtVdDc_sdO<~YwXD(->jab6m`0`4{ZHw+Ju4cT+#eenM^`2Y0!33>P8A-S@Dy=+;YCNstkl`b(YwGL<s4;YBJ%ltcM;SAYRlB3M6UN}*vj_57Jdv})P!M$b4)UmQXXP(CYcM$(Szjj3a>!q{u<GYu1{O`vffBaSc-+N|{yvn`553Q4H`%2{8mOR;=zff{!CQCB%fVkF8jR!CnfF84TLG92$sRcEyTT9}in#!QA?PSWxev&z5j1;F&m2EjD|5yHt7NZ`RCF8)q_rt%tGyioB|D)>MQ;Ppl^&ZfFLyn`2;lALF9>uksBywNi#^!v$QH?4f@2?=k8<bc6hpNbbB^Un*TKjwHKAYTkH?_4nyOU$tp8Pxcf0D*I|I+-cO0%D&V}x%0*#mI)^5PzSm2Q6ae_8`{tn06RKQf-$Ca{zTXg)x;W5{|4*e@-Y2gC+h-tC!4(MC@GFv<1{mH+nSANhWV>M~vV55eo^yC%n)KBU$e_<2+DGiD?9&j#~z;@nyQb}1+SUyM3l)jdMJV6n#ja2?-Ix4A$22Xi|b58!k1pQ*hA(?2jWV+l*t1HxS5{6g|y_@tQ^%)LRlwt&|Xt`F#A_7p78eTjl4m;0G@_SZd(m&W-?b|>&3=c8vD>Dj|weDxgD<sH2w;fV*8|3BdGy+N3ttY11u-np<}zIfrRd~)NYe0=q|ynbf4oZnFl4_GL3Q$|YvF72e*6Lr)F*cWW}#ABrt7u{S&fdARZwTsiI$l>Cpa`WJN`QZE>`Rny#;Qx&Lh#cU@&tCj>{=fSOxyXKb{qz=iz!rJw$VPeY06bv(O4(buNLHYZpFX<34C&rpIz=^>$Od(!@#8h5ah(WhUca`qZQek7w2hSEJvz$d5q%_M(kSEvlVwZxT-lYMCi_-p!m}1@{NI!_`>y;a*1e|`|E&L&|0fLpJQHXX@+rMGDudT}Hb{9H+0e3I{|@{&GGl<;4~+W&aIftY%l`y>exL5k8y(=<57g=VdGgQN-YB4yH23$&rxRNRN;BI7s8#ndsdnVr|9K5y;<twf*!b=2X~6t|nyj3<`JaTx6Kw4;9^RH}^9Cx_);kkp4Es}T?x6WYd+P)I9(>0nTZ8ak=kXMslT=-6gjr-4%+3YN^AO9ah3xB*e{jBtwLbXg;|wSbx&1=$P0dHW&(E^gpmhX1fLdTZKx>DtCKq7du%ug<^?^|J0?sYsnm`=`=pEr&3t$wXYyP;O5ZTY=o@DXd-^lJ?2=143@|7hWy=6%UZ{<I?vpJ`n%4t_cM%2Ae{QqeA$Nv{=m&}s4pV=>;Jby|)eg33;^z2dj;L>3*xJxeYDv`YfnUV|cCXVbc1G{yQ_?SqEZP{4bMK+Nxv5_(W-jF<~rz{*lQi{^1$WyB_^lXkdPi>Qr!Sa_kj?1?%pEmq|dbj;QKgZ8MEN`9OF0Y-~Ca;{>iacPwT-#qFXSe372b3(DELq@sT0%b=(=T3z!V3n*$LboP(S1<&5A6jX8Z1kuB&ru}Tsm9X--FNYN4~=NzaRDg=A1b)VOS6Ge&~0<nE#J{{7-k}KS6r{4@&|x>Iv}Avw=_#jI3W)MuPj{Rzt!6P<X)*@IR=5;{m1l=a_$rNBnor0!;Df0p8BP@qjz?&$WPD6X<vV<=F$^n*aan8el|A)At+i>{(>@FQ6va{N5QS@QnSFVnbA(9^md9@U;+)<G1?;r7=I&$n+pWT;jcCe@1)6`%d7Oz5C8?k?XX0#P_abJ_5|+%6WcJdY1_850d+N5Oasw;GJThah9F`H(%=jTR+$yBG&@Z3o_LMLQxl>Hqg0+rUu};K$8P-53vZ{CrtMv)O|?JKBnBWG)wnD_LKDXzL2+M@!XU4JS*8tay#+dVlVLTB}>~?mf_&v_Z|`c)z=U4lA^^^<&Cqu<S);kfR4*YSB}Z8D@Wx03kT)3Q+wq4!Hsfi^Gex~pDrsGOp!&C$H>g2VKQ~}Aen{hqDhIea^6JQvTUv#F3Ob4I|}8M!|UZ1^7zlM9+Iza9+!W-bXxxF)zfAS@Gks+a`mY4|LVzY@+$a$@$d#c`|HB?0y(u|nH&PYTeD}$nzTu>VooaRjnRnn39@M7NXeQ!O7c+a(}y-JnW<;}lKs5}8S>QXC9)5{-+rDChWv!%e}@0xms<bt@IUtPJM%yCal?Ot2md4LKML**|HJE-lVLXYJN%RVLG|eYm1J;3&IPEVYXMyGU&sA@&EDL$|L4pBNb=yn^uC@y=YO>B2^i?|07u;#xc37HP8|1}^Ur;SYSjoWRRgg8cl7|Q4~%!%=NKPza@PH1-|}zrf72hJcQ{@#QDyoCliIrT29=HZChmu*Z%mAF@`UQD>ENFA!7MO4&vHzj7siLXlxA5s3Rm&<`C9AaGw=-N4zqQPp}Orinx4ZPJfElYi8w#Ncz}v)0lJyFLrc5cc}4I6?irBBF@T;Cy53LM{OOng@&ad1vMxcA9T%v37iV_x*Rv4P+S#*D+S;?z^;~mr$?xPP`JJoD(zb{{kKHT2e>D6*5mZf9Et(>)qE5ec?I^fEB)1U%KfH8AdxF&4=l08Mr+3Tqhd0aRy=&ym)&e=aHb--4>M1aFYE!OU+O=A)?_VpgAKfhPp4}mzAohQ8<$!#N`u`uOEB^WNDf#K$%Xi)X_fvS#TjzJmYvBKtqg&*KBOBz(?jpIkeTAIcoF^yOERln&(q;cjaJ_PY?9Q7nJC-BA&*K??3uMm<#QFlhm!{|c>@P&TFI=o=ejX@U2KJZe{@>((68HbM>woUV|0q}fe{U)NN7Q{#Mm+H_u28)@@n5bq|Mh>3e{bypq(qthGM({%XCCm_R{o{<Pu4xYe$Kzs1JE^rtpZB<K>sEI?s)(mI_chizqJR1_UhxV0jm4j_#f;s?q~eK#jpqS>H{_(aB73`G462!NA9_WChvD@{K+=gcRuUn{H8Z(dINK`&ljpZJ6dR!7VqQ$rUo$F8_l!q&*=#MJA|70p@Z84X7jZUrWRlgklxwp1%#V%MB58kqJ4lc-Fu9Ci{<vPbwJNhRbJ09-D{lt4s!p}Wz73^-*Z1%O#a)Ge{Wf+=c@DE^s1TzWOt}4xp9d9;D1Rha!~v&`<jp^bptC)VfsXQ<>Yqx;Nn5~@G^wl|JLQhczjqsx@`R5qi2u68;<Gg@0>p%@1Ea>>ppqo^bUCwb;3KyAKpE)OFnpdx3d57rG4@_*#83TlmBmSo<I!%{y(^S7yiGJPp%)6x6bWC?B6CY9or<=53JEMzn<B;Le3!mA77Ix$4j#0D17I{x+QXSO_m&Aw^WX;St>_VOLX6$gXA1}KIb4FEY6mL@T`M87i@iwo&mBWf1yks`K0(h?B>5M_y4OUsQv#aiRAzBdt}68_sG!N_sFov{w%}b0mJItFT>ygLm#&u;P7AH@IQjFAN-sAzk>2V@CkZACFOr;LzDk|^3QVtlk{wGAJ>_^iS`_hJMo|F+UGCO&AuH6;F=(n^#I<(e7kA5?E%!Osr&Z-cCr6odPabMdal$C@psh#!&<oaF;Mnx?LH|c*p-vJ<@Nz`J*n-_S-yR&z})Bb_DsD%dHMr;fsD~}I)y6x%D<H>_xF_bfrZ)^2vz>IPtY+``KM-D-e<OV`@kITWz@;eA&BQZ!f+iAbm{=7FUVd1_XXkpK;)l!0QVuxvwIOP;~qj?xKC*yYW_govpj=mATjSJ`w-7PO>bLO7DG$gGyYeVJmmj{@pvACzscHv7H@}t|H@LZaH3p4vPs^1<|+B$0%hVpx&Qd;F~s9j^4ay1MtJ;%K0YCzUOgh8J$F<-yK+!IefFSya&f<Wg3s&q;y(HG@_zaJ8uI_^N5TFX`RA)=5dW{-h5x@R|8FAyci88-9~ZW*kh2?e<iy$~%KkAhe-g|e$Mcixv%&u|IkA439L4p>+H5(D9RCpbKTw1#V*cTE%jM|$Jl*T`@CKg$v0Qc_KbVrh{r}wif0_M1ekVzf{}FNj4;uT2*Z!08&wsa}kKG5|FGK2p|GIAeS^x8FaQ6Sm|A>a=OYuLrK?QgJuOZh0RQnnKJRg*Mf{kin)&q>Pd;d86yXODh#=pZp_x~8F^MHQNKh?&g2jG{t{{JuYuYV)ej$YGVL*SIT656M~)T~#h)P7{qwf(ez@9bad<m9fJJl3O@w~RZn+|97NzMlrprfHAAx=d~F^b6f{jU6ADtYd<V`DQ+V_USxwekb;m{X6-9zRt1z&ek(<bv$5hCr=+Ry+ppo_gr%bE%Q1SXnTQ+)dS4FAY31ut^1C0kAM(mo_mt&zF}QL>>kE}vV>=ypzcp^kGj8|FW5)iM*vF$_vy$1vf6uj#Qv&M(1rPb1;nv?UHm_e_`h<&czFSN_Pgiz%RA5Pmk*ftBhGUi;FIT$$rm?H$>%pu$`?1!fd3PkpMUn;apnKxE4c6CQ$`o|f&YEV|EG-qR}K5$ym(5!eZ}(sZ^ZwPt{q40->iMVt9wi2io^e=<#Gm$pWcwI?4MBf@qFWQIkh22&;C8Jk(}qKj=&2J>sf!x<rvuKm3y5Z-jFMYH|EOrf^?ZWvX5*3--kWs|BS6;_}AE9^G`Ck7F7FQ8SLW!L09}wc*6Am6FmC=JR6+jf5X5%`~O24RMh!@TmvwIy+1t%s5JkHx-Yo5;lKGW^Pgh*H|z(v*mti1a(e*pNwRx^d-ecA5|aMcnXi21eeo&UDM1rwN@$PX64A1yj>rDe9;C5#{IoVnZR0uTC)mZlXMb)|%x(O~daNUA2i_s}@22zGzPj>F-<TNd&M7=U$3EXo+oNNzkK8ZNSk8PNd3~Id--o&Kf%(dB>Av6s4<BF-G1qFI$JKnc^!%YrI}gCw3y6CJE!Opgp|1VGxX0j9-7hdi_Z@eX$+)lQn%KTyM|c41e#HJX#QsHXnfH4k_E(kVoxHW~UxC=a5+1Mw{0~7q^ENGSsS{9H^5>0{o2c2}yM(;{!U6g43hIBv{!gAeq2mFc-#DXtgM5J;o@euZ3O+x67TjMsBp+SaFSo$^$B6g0!2U<nGl>0H56U+$oK)_=eepEde_FnK{oKFR|GS0ne(^Ba-&bN{KkEKxwyo5;K&=0t-js`6V7Z*#oU81g-kdL|!1D>{IM4s)`M>MHJ@SAfi1o)d<jb*5`EnHOAKkQ4A0NWKyH;ly{vWxw6#pa0Kl1<4b^av7YyCln)ck`Cta*<NtmWo^Xx#^tefItm>U;3tu&j>%lmAhTD<BW3AR`-BlHrK^Bb!u_QB7F)SGDH=cu7)AZ`ZltF8;~B<$tX1_iN|1dhGk<^#4cOeZR>4ux5UGwukcH0=_^GXk~f;<bOcZ0I65q?*AJT^Plhg``(XI?eMb_IBmX!$Hq#H2KA&?t#DV4|7+qH;J{`9uJH-&<;A*ldZ#}>(P~0W>9IO9UT1oGWPO_Df134zDYn<|+3RyWg!gOSk34;LCy%%vXZrs10LJgRb{yd6d4chP`5t2fu3P~3sSm_CGUJE~I=f^4;;!MUOdJ2n{vzF9fa3$fs>OD{u}pi$L6+US+?D&s2kBXbnW+7Fe&J%|{~6$a5xCE2$Jmd$zoUu$<Uc>Is;r2sELpMT5zGD{UIu@u<zEr;f1=zxzE$48ctAct{r@rf*4Tf_jtL_EBM0~te(;wo><Ju@_s{Q@55V~c=fV9me7sj~o!=v$;63WA8^`7EFPxHZUO6Y<y?H^thm`-jo&WdUXY%g3eRBQKT6uO4&-z>~mv<G)#hnG31DxBMFXy)8%hT|Kv+#pwb{5LH9fkV*)Rq-;a`Or~0gvFB#z)uZ>e=QeHXG)TZz+(YJO^|W{h&a0tpfiEeZl`drPlusukky?{@=?;=KVDg_Ywcy{G$fY-2VY^|B#OTb3J9E&I2k3nfX5njSc^!kpqn4T;Qgap(?Jkz?6F^+0Og_8UK1VfS*|t?7@GsNB@tpe<T<m*393v=g%+mA0!Ep?zI55YDb9IzVrV!=M~=lUVHeJFTW>#%hzgr_dM_Sm-Ltw_aSLf!&kaK!L>@ZH)+Qx%{+i$7ypx-xE<@f7UHViC)z9TndFS|(F59>9zOY>V)@taY~z;G|C?_6eaz1d?{3EDXdlny1@wkF9`f{o1@WbQV4<rf2y>4gus3Mu4y470sup!M{Btc3*9vCY+CQU<*=LCCFVua>gLU7r4BdCw>`9(!&o|Sv&e-$UGm8WC4AaaGK8X3=@PTTQ*^xcJYKZ;SBp2-G#UcK8ssfS!ax$pKAEcV)zqVfm$(zggzXj|cmJi4`YW`2i`;F78Pp+LrPJR^JAC~tK@85fRue^VDue|g0PI>3d4xQuk!MQ#1(WR%L{qiO1_pfe}{j>7VH=dDip?|)9UcP(l8Tsj>=kL1y|80E#^9R?s*uTv4es>kY16Ij1JCG0T#N%D7<>KBVxv;xPo`KHc_0!5eI49rW|Kuk6L4h2{>s;^4Gk-MxZ#6!!d(|S{|HsMy$$vEX9~J@rAN#!w)7bxe83+#;SnD3m|A*ARPkVpE>pi4nf1LYA{>L^h2i6V$<{Z!phJPrLdjN4ykd{@E>zi{xlEHo|$NeJB_}}gMzjG#NlCuYZogd@$09o_9+4qydO?_3vSOYY({707NpYogs;J2*gm+SX8zSS{;%9r2OHGDzirb~?`jqb`k`+A&f$ay8~H+Fp7Px>|v(7iv#Le5pkC8F(|9Xk#<!Or=cXxaAk0Ed4^lRSC@Q$1q-6g%F>&*?luXFjnrwqWNJPP5~EGj0Aq%l7VPsBA6KzPhW1cRaw!2NpQvejZ~5PL1H{DGR{-0%zR7W85#3a|pVGx%LYn|J-9_p|dZsJ^Mt@KIj}|&p`^(Gf>T$rU5$7k7t)>#v%51B>P^<e-`=gSVfj~Wd2`O^1yyx$I6o1p^{`qm&M=n?_K=Y^r;}Zb5i7mV_T8`A2hxGYp3KR<o%ov@X2-L0N0Lb|DXN-x6ka<IlXV3*dlKtXL#rI4teM7c6h=r<PUr06VwD>K7UmH{_<)0_O+)~-@ScVzJL1?_(%M|b(j4AAGhR9eAo4ZYqj>jyr)>MJ%wCg?;5$Zr$nyqUnkG*TPv4P8(hKlnLR~vekb`~EoZkC${FSXTUW|S<^qWKjQuBcy}w~z`N!iUc)YW4kxU-Z+r|HbmjA)wr~$zKaMb)m@NZ>m|9edANB+;Tzl8b^%9w_aSoX^*|6>|^@ZZ>+2S$zMdf*mSbstck1K^y0N&Ztg_b1Zy{hc!b#(3-l>g)mHtZf?`<!{&in{)ge_6Ifbaq;hp|B(SM_LHqj^#DeUDaCxH7e1G22QTZ{U)(Q{X9Cp<558-R=eQ#CidMD!qyu>8nM1>y2deiZwhB__J>~QyZ|8raN4`%v9x%a<^*QtV9G`GJV2X|TldW%*UR&fSxYV5DX*OSQ#{HG~_Rc&bk9h-HD;OVedIgRL%(L9jQ|8S&0p|XmUNKk46GL5Nhv~Y0*y;U;YwoZ61IL@aMiz7m*1d_jS1HdZNQ*OP5-d_b2sCFJ^Q_`{UK#(9`*%d_2meO!0L1>K9jdxKAg4oRs1j6O7DbhjzM;R9$}axPN$$K<jsNc<zQ1?*kbH>zpZwptdP4UFVPEj=b9?_Ecjp0C<(cjKe=<qzy*C6zu=h?A5o<Jw>CLY6-VuB6T~R@*2!f&rV(&fHSW--xl*!DTnYlA_rkr-?<gT^e_uc#38!&Uu+<Rwo@;vML00DQ9-|7Wlo<D?7PVUFYr}xWko+qaY@#J(NetND1Uy)Au#Ro_6>pNBW&Ak)&J;nZi`bhIncK=^KxqyFta)J22^K}0I^ff*uzv6CXE=j(yf3LDY-o9N)>>n+lXTIG3zIh~9bi!5g4K9)WzgWWj@4|V*KiT^;MH#a1;|wv+_Wwj-hTJ<oUa%b{87ph%zc&!l?|HHRoco*A$DaG2+rPf_Pe%;pJ0MLEMG~p`7yo}mE8*Y9zE5jnU)=(XBikPz*pb+`-@j%4?e<@>A8BmA?*B3To1K5czOSoh-&NQ*{2TwDeSc=n%0A09pDwl>K+m8cbS%4w&bx~xNAEQ=7(M-be~@>U;oUn+J>iwX?s#WJ7tGSU^WPaBU=huGRG<g>`$}Shy2@*b4YG4-_&4lX<9^{^&j`fnJYkY0H)!MEqBERli_}bkhyCxEpkju36YcpzlPgRZWBd%ekHMd_NNRjaj2>9>a(7ifn5_H+o-g3K!h%UUzo_$o%VdV2oAAF%dH~g|z-s>36ul>OlG#h#)pq|(ZWFzv-sT&ne9v$_-z=B==5AvD*AV|~``3`|UrXGtozNcZC$u9S(3YefHcf0p(i)p5w8V;$jfsDtwdTKP$L82d_Wxd0A-=p?fv>Jtk{@tf=77Jxehgm{`=1iupOUSAe1dOs=Hme|`QT_a9#-Yyqmu>r;vD%7mk;6BWcz=8`viXX;TiJ%FW?W4&yx;dxrjeK*8G2S>j(J%hV1_livRO)|9Bz2u6@M+KHNRV{1@O>1+ia|C$|3v@qfK68<!4dD)vi=eaQptBK9*xP8S;Af49W^$Lam5eSH6W8xC;&FXSD#TJHY_wITk=|L5FaNMEk~y?{C7_jB!E_5G>YKe78({=L;LU<dwv+91lejquNVz!TW^TliPDzf(2-qXX>xM_c;;B4yv7t72blzLIGp9Qil4KU#W!n6s`n2@9~#0&t#=eR}Jj-r9ukg$DHQjF%~XpXx(#$4C!E1Tv4R|5j>@ntipC#NDO?#O9Ay_FmWz>MCCsCqB8SNYZ#OTRd;o0SVfUJMIUt=L(aZ>I9-UocaolSo4eK@38h0#|L$Fn44fx{Q@e+F!chX15}Q{?mMJggzF0;S{LyAps5w`EFkNEl`os!LcAZ$Q|~5L`^Wia3Ewv38$~N7yUQIzxoIx<Ox=iiiuoriSxvG3>K8j=^+eV4vt~kDtR3Huo^41Ew8n<<ElFBX?B5K_{2JjMFFXG|IyNKzqlvG5_~LRoKEGOyCluR1xp)|#oI8k*C@y%&3?3zWOg{I$^1b+w<N?V;ve8dY?Z@ZL|JB3f`yaz^?w`gV9$moiADzb^9-hM=J|cO<&$IMAiSNJq0sjB_TYOG`_dVkM{)zqcEWjPM|HlgD$^74<*K$+%&%t%F|CdXZ{l7pu;2hh3viV~7_wAI}|D?|MoygyTV|m+T2T&zR(Kglp<G{bf|Ia$~{RQ{0i!k2(GpIfynf;->`;TM<>i~5RkZ1qGW$v#9Lg{Ue{jK~f`_I0=;@|H3YyJbO^Uwajxd~+0pTl`Rj`f9mBZ_~{|M4y`w*Ab0tY+WDz_HryT5|wT6K{&&UmWO$_q=(}t2;uN<v<VNICe}|^$b!rJ;I57!@v0aItGcN&kd)yr~7unlp*dI*WV2@{5%x@<Jf=ax_vhhw&%(8e!PW$=UPFcQ>`G`;xojXxd1EwCVyc2I#1<$NDrd=*+d5_G5!hvS+edk;M{=e>o>xTbFYE)9O;^b)ELIAoWy+51**@0`+t|)b^>_m{Q&Gcc&gpRe1l|yhq_rb!Ch{g@lDg^lhsY*6@1h9MOUnzM8eM(Deix<6IM>>h*f0&SBz_i<>T7Yvn`g7ZH?t)$ltd~bF2<(h9!Ot@s=mh((vD*F*c@!OJDD!(+BW~`|Zz^;_*p(o+!eH#39e%-Z_$mTV<KJb#M>v9NLY$hcof`SgynX+^hfH^$Ps*?g{+n!_zYF^QTWP;$J?#OmP6QPqDynKRiLt6Zqd>U3)tJzxxuOoG%srZ<Vv1&lB!%SL6xzH^}ej*#8E7{A$@=TsFRc>28Vj+1^XezmVUiSYPM<kCQIoeFasy#DCrv=>;s>z5<DJ-hx}pdiMLD?IAV4$N_5aM;MD{Kg@yuCN}<UzQ4}>hw0qET?bJ7FLyxr9&jhQ1<v_D=03(&`>XE%OaEVho74fro!Fm2%p3k~vA?gY*nMV>e}CgF>>K`>!<udbnmB$m#qe(qbCce@5beK9954BL4{gKkBJ2mM&k3{e&u_ELrMDIDZW1qW+`zs>KmB)dUEo#H1%`hk><=We{nv~de<4oi@e=hMU{W=4<X^;zf1?j1FA$`3WVDV^q5~aufb=AeQ$51z(#zLPgna?d32@xNqH+#uFA<CBOXQiO`4iR7fkj#eEYtD8QsKU<x_icV5cwV=?;z%z1;qd232t)Fm}U7yck%~_|B0@o13F_l$?AzNy5F}0mJ$2Q#<azf(QT9jw!*T}EwL=HIhIq*zap>+R*r6hh2Hh?R#!X!e7|>1N-!>x-+Z6!{D)+3@3X&sl(-|_ZXe!@o1BL~xSM2`$Q@$;F6o1hj~C!G(ih)eJ1TR2zqxms>^|9k^7~oX?*IPb8RGs7enYz9clVC}ApigQo6qp@%mHGZIOn`S=l99KSGImHy`6<?N3wD4a1QZLwx8ZVcYx!4?duc!Cn>%^A+^5@Tfa|L{&v}G#&*9VdlM>hHlcX?awN@t8*VKW|4sD$-(ZUUBZ>RSLA>*mZ2!P|2peqi{dw+xSQD)SRNaqr|6KbIHMRfYD)%2IaRA={XfHY-%AaolceK_2SQi-ft^70l>h`C)0UGS5*q`O&qSznqqS;q@|JiaM+)ZkN#`YWjIgYjR&vn2_16?H#FsipJ@yB_@F7k0B5f<*B#=ha7S)b$A1v7m4=D0gv9^@wdeaw5mAMi5*=rzQQ@f3S+<Dc|_yl&!^eGQiAAP*b=s$OWX6&Oj<dVzfkqc5EGfK7rF<7Q4G%2u23u<1`zYc%|uT;Y6EYt(*6!WjGh0`X7As67R#GFNEdL9|HE0xprc08e2*U3MC~tDDID9OEjt3;B-mq6w~~2RdW%cotVIq37}mE?7pgoO^vqmJs_($F#%3z_yql&<YDikp#5B0{>=M9MBw#M>fUMkxj5Ppb-{~sE;>1fEI@THVv^nem2hU--Zv4kbNhvd5-5M+0~mob3?ZECJFc9+#>dGk?p@zwi}PD3M41+71#c*9l<Yd9mB8hof3P`xj%Ul|G&9^62HCAeZpK9sKUQKx%gE6|KnFrq!;NrF@J@8`76aF6xUxS#xIk9&$&L8?<bpoV2}9vXSKh7k~lw+w-v{8Ri9r~KHGZDehzcL8RfFGWCM<p4%okS84~BdBlikz{lCnAAL5_5kK~=dgX@ajXW_gb*Zf0?|A=9hy1&Hz7T<qZE5$$8{(ag={XatP0IAx4)JV$>;F0z_;D&$G`x{2ghwx5+Ul$T)T>1Hie-r<!y1%RNA8BHKZM&R(|7h*2hXuGh<mp03KAnAM{`q~LDPZ2;9bxBv1jP(o1AJ<1(X})B@V*v{55RE%GoP&4H+g|L!#=-u9e2czvG8xH6<GOKdc!d9=s(11|G?x#qXHf46QVzZR9ukkTrbf4bBr&&hx+eNu;v;?ls}N7cbD^Cv2?w2oMoZeJwCpRu+Mi)_-<iJkQ-7*yNV7-AIEyYmGl6yKdv(tjddX%Kr+q+OX<0Md`B!E+a60OXk9qEEz(D}LYjYT%=2%Fbid}9=i8K?O_1)}7>oQGW2t{bqz$i&*SY}BHUC}dcYj&*44Iv|N<Q?p(p|E*hx_fW)AKqpc!QoF5cjvp2e@0FrSg6h_kVW&0KOsizr9|GU)(u?f4XxVzkP59zki_kXPYl_kNbox@T;4LDJD2iod0{R{QvpehvMgRU*FjR?(N%-i$y!7$L~^chA_{yz6&K3?;qGLx&G7iIn4e^`uyX}d(KuI%T{*&80msadY{>^ppTVjZNlNK4Wa|~Z(EMUV6y+s)dJwA%zsD2zq0+|1J&Md+syyqMtbhovF|UG^gt-{&wc=N&-=e*2bk*pVg60;5BmV&eropLng4Lp`%gAM#8<N~zCHW+cK%s7_s=x|)%T}%{#$*26Z>1)kJ7$+xO1M~%6%>T^X^{WCG<Ai_rZ#H&Jhgg`op;dp8fANVi>wldI_GhLZqL-`sW-QB(lvPXXiiO@K3KlO22-?zr^iM^}<Bs`;YBvv-j4#L7a~BRjjWh+G_u`n4Tn4E0F$Tr&@uD^HX$w!t5=!<_#0anmt6k!@$Fq3s@lc20VoSG`VBIH;i17MBFC@b*85ak^?)F4j>6~K{`EI783sp$9BTPacuKDkq+pHMQrzp{{?|<kV@Pq`?WxdUo(=XNG9&*`7r-v_k9~;F-hvMI+)_Be%FQn7Im>Oaw;mb))HsT*bcF^SBa}@tOH0N@a*la!&x%hD>=Wj2k_-3p7|>y{>v5r+z))GigW<U?JE504zqt8zofX}mmhFn@L~Ld`2XF*)5Q6|)yn@rfB7M<k&QoFz<s<t*Rus@$)7(@G5m$%ol@h!!nJ<#0ayo|Bmdwmee850%Ptl3Q=ET1Z@a|!6(mQAd%j&zwr4%cGD&u?6&+BpX^GtbaM$ra|4f1;{(r&7zMA(X8Q2hW2U>gnSqG?{AJ)F#Fx~fS(*eYPsPNxj@$awrXWsv6{(T+!=lFl7*nL;&{}J0iQqBHGTkOB#-kE)|{iA+>{ZT6Z=N{cRhr7w%-oQStQs*D+^}~ICeY?8KzTxhZiTAJ=xG&#~PWz6b{h1GC|5%&HzY|?xy$!&70gZ2O*f;e8Ywa+8teyWvEr$Puu}(h!Xh$6oYvEsNk~T53(q?a(o+Z|`MKfQ(dLY#@TaavW3VIh=oXj)XXBkuV-qLv!)n37L?kCW9M&{`|hI|)YZkY1!x6z%Ep!rV@bP@hjMt8zIdM+5l(vfsPN8x`7Jr^?nZ2JS+5c|ZxZwq=hLo#un<lPvlB=fu*VZL`mq>reF#KAA%WfwdDZq1&@ywG=XbkAyBU|Y-m^<-<WmJs`7@9$8o&AI;v$M@l*lf`(#^S+ml;HQ_%@zu>^`1XS;e9Jup9~{Hai2I+BF8JBaqxkLyF;7qK2mJZfL-_fXgCf6sc#1gxbFKXU=JPwaME=I<{7pEOOZ?|=CfSOM#5uEn^$^AUB00EJmW#_r@^R(JK9WLQIa(xgsk{*959Q%($zGf!y}<X3%d<8U_v_^bK-r#kIJj#K3N|lA{H(X&+N!?2|DWxD-#W7IKbU8IiGAt!@83Yr{4^pxV4wMun16V4@%^oR|IWTYv(NXx4gV_kxAJd~{cGai`2QvcsJM4zKWemP?~jFhEBiI^Z~r*?^FHo)W2l?-@o>G5{rSOOE<YH<%YN~J{iJ88YfLg+H)f*ak;`az@nf{T_e<#=cJ3+Wz5{FD;7fztlzrBGTlu$&Ef1*kg9%P~!k8d?zeB85v<^@|6UBezC?!#roS(@RihZA8=`Xa2eTR^#H7fsK-!8EA4wzX(wR_y`9X2<H=Igu1e8ZgY7bg<`NyL2uai27b`~Z3;6Z^@dJ0dNx6VmB9e@rKe13F>xm=5IocfbO%{p|a<Ci~w?_)j9gpV?0u(GV%aSqC)0d>`h&E)s_*{u^8Q4|!X5>|Q8jUwb<)kRQOk_S|=S_h>#Ia?j7%67u)U@Wqv*`1*sB_~y<T{Os;|eD~o+e0T30zPrQyLZ@VZFz*of_B#0m#QoP7DMq+-0N-9N!8aH7<5#y1%Zy)b{QvsNO<bgSpKbpc^5=P`=lp*1`zhYPO3Ys_%f^+%xwv+8AFfpv;rg)>+&F#^w@#Pi_L-x&bM6>!pR2^}vz54h{4g$;7vW4vHja@`P);#G*`5tJMC>0VA0ThTV#R;UhBp35y<#@m|Ih*4>r+p9erENrkLjcXX7bE`|3;WIuo3wHYTlpif7tNm_MU%j_a*m7{PW#U6Z<p&9Q#M<++Wm4Q~T@W$Ulpp;y=Vs<^JXf^PQ#kKUdHBgp71|*!dl0+4rM<&#gNGEc1fAAH<>$IDVf<ymNm(=j%8x*#EEa>3OFo?-TWiOM~mSTy&_sfp*uvkUqlJU;Nu&HCtf)e#7YRp6TDkZokKPIP?rS^B=Ec1jD|`5i;Lgr!?ZM2jtIntk3<45t@JY3CteBgt7KMfmB;BpqJi1WS>Wn*@Ceu=1&-7-#yBE2V^d3qGk86xj!<&Q*IFO-9e56IOk`N0Xj=>Na85PeiBJ4`2*?nOs8MY1r!4)+us38*!GWUi}``AF`r`pdBlGT#r;V>WdFU%{*P#k<Pi;!GNL}QU!Qz{x=0xGJSKIr^WVAIb4U+;2j#m~l8?PrW^1mMWJ=!s*3mqCc)SoFohc!^e;A)%I!ZR5*k|4!T*l8xe*Wk>e)0G^y?>Q-0e$S=d3-~9;Hw+QC`O>KIbVz~&J^Owsa$+c^7CsYa^t&p{%_&Rp*_U@X2pI{1}>FmNv{8Dc|NWmWm{i>>lH<~QB{HuP9DY`V*T!gDm=J)29IuBz~h^j@aTh!czFE+?p{2J8}vEn%l6?o`2t6?x8cwp<p<<$ScI5aZ@{f}LtFlTwD4cY#{O)2&gjehEBilZKx5*+vF!U$`F@M-x77V2nSCGC_pjLJ-v5pcb3ZYr_m69U0Z#n;+w*_HmYcqz{%#_&_gnA&H9LQ0FMve{Sh=^dZ{?rs`rHrngS>mX$^5?Xp1cU1vr6Tb2>bG_Kly{=o#%x9i!+2CiSr$1gh%#vb@1O!-mfM-V03|bJ6hN8BLl1RZ_o895#1MHkx|Mgh|@hlW>&~LXOyga4$Qn#f}Sff+}n1MX}izdr<;5q?;7A9Vt!k0jCzU>z&e2Q{;8b*3vwf0-<7!UOmCAA_i5z!&!gu&vitLi|3$?9;;|jY_AegWPTT)BNFUV_^ZhyhNA{n5f95}VL?a{(Yk;)j^~Lv3A6^&ngPz6YPAb>U|G741|Nm|B|5uUkze8qhIro0^aE|oe@La%0XAj}ii-+;#as|Hn;56BOvilFN2>U<(=qAYr_{Ar8=y{7ib_3tuzl3kdN8p{JpPxU7Pfr)(<6~L)<k)Wf?9zU@@m;I`|JiM+>!0I%-+?{2bZ{@}gKS(Y&m)^(K+gi<|H{!qvj4@zf0^+A;pNkKeB%;6y?X<nJ-Cg}AKt-d4{zhs`#16M#zlNUpL2m?gX4vnII?#W+5dIeyLJKM=DZ2F78d^LXEUQW@jpPZuWWw<OxOPZEb{+D$o7Zvt`Cm;iT_C5^T#(oBINc*YjyXV>;ByPGqQtXU-Pf({!aXlvhV$g3~*KaYYFvNH$7SGd;Z;RyMLlbSLZ(}usZ+M>_5rtL)~hO-8nxm@%xn5Wxls_W{KPnW<Q?y`Lz1!fBd(2cb4|2?#Z42GgriV=-m-rY`sO%));1V7cCyuCD6lW%T4cr;osT=9T{lvRZ^noqyp5>M{)x?f0u0f12y~BeTBlj?hlIBd&uNwfz}u7^QY@u1GYUNy6(rhf3E#4l-r|SNQnKhqyt#U2S^Q4dB0R*J~gP5%KeSwyg&K=<5k?R_-~KJ#Q*$JtttK|`ybFk^8P74P08nPf>g5qDdhX7d)G(Wh&uGFi`W6r;H8ch```FE%nyGT$M&w1dmT~(IJB4e&z7E>56Sm`eD)wdIj8vl;>HPlb?Xc<ei`3BxQ6c@-6Zz!;-5ZyfPZ@O0Ka_lAwBPjZr~V!cZEISo}#L3d{nWUe1Tki|DWI0%K!iR@*ZxK=SmLXD(N7;MRbGg{#ANjCBOd)Juj5*#re`~>{_!38y6?btq-qGt<bJ%U3j^)#`~{LLgx0(DA|{buReK*FCN_^f8a81ov*_ABl}TVz_Y&_k-MJk{|vJKtr|Pn|CxQ2@6U67yys_@?D}eq*#nt<9rttHPkMe0`>nOjZ=-JiS!RA@=8xFtUY{6e{>{#>QJt&Vf02={B4JvLy|?f6u+IZKvu{{;)&bVKJ?HmKKCe%|Ug-Avo9HrsC0z2#$(R3J=6?U5?g#t&FK}D6)wUbh%ogyj0xSQKfsXS-5f<NFG4HOWi|uJ*d;9nBP<aoX>#%=~;y>E-{EhXr&m!8yv8OoR@IO}FFthd{s(PX61@0<6z&s<sJ3!^G$RuU^7fBAF3(_aJA#I#H<`MrXV>JI`$mWx;KVNeGF2esJ)&UgzFQM3f(HLT1a{pxe1IYg$MYexrbIkK=hE%fu^F$gUP1vu8dBf_^^93Xj|C2ge?f)}a81Wvea@Wfo{S}_EJ(wkPH6I+w!CmeLI#Gzn6a#!jdf>pWwa6lFrcD`#Zp0ch*R69KOn+lC@-nudyeJpH`r?u3gP%Pl9dP>`#SP{7_|!f;JiHTMoXx}cf3E!o;O~BVUv3azqrdy=VY2xomxy_u-{Bs<W4YUKJbxQD&X2>`fnCw7Lt}W5|L)SFzPuKz4C(EO{HzRo{p2w|e{>g*ZeGTX(-k;#Fc0Oq+mX9zDWYe-3HMe_?EOD3WdHlr#oU1nbiTil`22GRH5T^6hpC*u{nk$_MDvUfGw;h!5<g}8V|34-jeni<v)g{=zmvnxKV$o?{M*>K#rlry+hnx8hGq5j4fiG=V6EYEj(=n?ozL&(C3gP*_g+wzo`Hc5J_zfikda*+*ble%7;A2M*1_6ytVHo`bc4|YhJWc(46uI>lLKWR!rV+VGe$|yJ;m1EK`n`9SFzkE)HBHwl@8#(ALife1ZMxAdw|Rx;&kGF-gtMU5&LPxzFj&C`wPYy+pqXvNHGBC{uTzQ*nh$3He~l(iTzLUZ;mv-row-!cOwz@{Zoh6L&{L%fB5sl|Ktu<{-49*sP|Eov!49zU9v~xQt@tFptxINZ~EBfgS+uTc{Xy_rDMWiuctCMw0Bn&W$(hTzI;qN;6wb3e1xw)IE9aSeyA!7pPtBmD*wO#<{|E%D#gvC`M6P8h>K+Nd3L9aeD<Q9tC7EDITl6Ez^Lx6;qB1^L*1KWVCQD&)3J&4GIB4E^K06%aV@_3^bwv=TyX#Dd0eSFh~q_BDA>A^;{SKx-dgeBjQJ=3e>VC2T<4!FH9xNPHMQ0HdB!(}*pIQ?`V{7Ub+5l)8^u4*`|G~{XvzDjx&LUb1EMwa!vCo1xj(~ynD+Z?;@^RN3;z+duy43GHFTo`CJwOd29HWq{{7$KJ^=3zvg!b?J<RlTcbF>*@mHd5o;!)&eW?16-5qp*(E}znF#LzwKCg?d=ikf-DDkYZr#Q~Wepjb^1zl}-2TbliT}1C3GCe?Yf82xggS*&%@%=5fKYc9mKaO>Pz3$I>f98KdP<t#O+drS;e)j$6k^N`(lZpKlVn2oJeiZw2-Y<n>{?uW0kuszXQieT`xPHK-_ICf@rSY>^ME1XO?^<D<`)p2gziiGX9L?H*gF9E@Xy!VsOAh`oF}G*SI{f;lAK_P@-N(-;R``<QfR9e(;mh-d`2J6|-vIp6&pyV7XAk2#@qdwQ|M9{Ml#;KWxo#0Qldqo|It?%PZ;u!Iw8OYwZ4uO?4Fc(P1$1i#U(c2p;?`XBQ>W&Rx+!;02EKTFACK=`!_BkDaHgyP`**IT`2StFw^7wl?unuwzu5i=>GLIfKST-V`l964XA5=bm+$=&>#_RQH#2YI{zxAZ>Ge@H{|Kr3b<{h*I!UcxvENzl{ze8E_FWYBoafWR{D=D2ZvU;ZzhOV37WN(bU`%f;$y>wSZ9RKECrprCz5m~RfS>+{)DWz9!bAs9Eb#7dcN_bL^*Mfaan=KN{@oq;H@N}T>u6^`e3X5z$u?gw&MpZ$-xqJWHDc@YvGA|%8h5qbElJk<$k_+r9zd@9vo4q~{XbpkWA57atKPrlG4|Mh9<eSuV6657$S|?Y59%QNr*qDaBu#q$xbH{tpX}2Z$;5y1@CL+wJtQ;xWcyQwyny5(&mmUx-_XSWP09Zce+S2NHsD;*Hk``eLcFiV{w+(9y>>o!tw_O=m|6cV<_=_M;8&kNz%L%(l)NGD^!?&uk=*#JmH(fAiu>n|;CkggoGRLb5@J7NRXUa>MIv(go0t{Q9q$fv!CON*;f=u^@%kXUygIN0CiiPcudg)*yESvrOM}TTDbCxAkM7>Uoy(_jv9c7!yEh^s<b8Np_#f-`jO_MO{k`n(E4v>~@qQ%V_+{JgqilaHaUa8Wo^3xfPqBZvNLz&ava~~(Z+nE14hZ+{AU%Ik-1jq5+xq}@_d8;w>H8t(eYI%z4gcXz{9AK>=H`!K-^#z0eZzd>I8R&cjNj*3-XSIjFsMH|9=`N<i2*pb$a@I-dO7AKxj%UFKvx^<H8F3*_y<qtU&RO}P6#)95`sKzeZ^KCkYJe^aK5dka)KuR*VWedC-(^S{qj_OgCK1j*ZV!B=O=BP;y-nqD{=28bwBR?NepyB3itd3bwXNDN2HHYx&L&Q(H$@^upQ=&YJ*gkfY#FUm*l5<ez@+>J%35w4UsgW0g{F){*#IQq(RTq^I1gq0VcKs>Kpq{uJ3}dcTttS4rljmBb&bl`!_Ga&ZUW1og9wDIqzUjV2}S6a~unl@864Gd~^%n-aAh|z!7|X<q*FAx36pE{}11NjQeNHaH%XChxcy5p0x|HBrzNj)8E4EHzpxsY)^y+dSWj98_)7}#S9-u;h7kIofG@C6+gxLdUkEufRBm)d)Ln5%JDKB&fSTW@agE<wwe4aS~36b&&o{?-s=@UOvU@me)#Za#6SD~Eh*ktwm(MOezy1FKCOvyV&A(hB7EA>`%0L9W<N~xAK}D*xWDG#&qbII_I0s|m3^KAjL^3|oNs<wZ+<xY{#N$Qj*VnJTW9nD&++pNj_}sg6Rw-G{@>^Qo}vRh64GnjD>kZkXH4^U<lHJ^)2+J)Mpe%f+H-}Dc|ucXG`mP_eTX{OFYN1>%j{r{v&{R%kFm}dsXJzTk5uUZb%#7<tcU6a*8FqMU-kYe?h|$0KQXYArSFG?xKE?ElSj2j%BXfo321{P|5ix!Ye~|axNkvvpgB^6fAasy2S_B}KWSJ!B$D5sH24K14tfqr#D8ROU_x7<p60(JIRXnp-X{Nl9ZuzKM#=W&*t05CZu!N}eHUTxzVbIQm$!2xe(~{beEZ>Le0A$MzPVXW{C`t>{QvDE+&z5=XG=0sv||m{%!@<RjCb(nq_K#8b1de+7l^dC{E;|iI3maOlGkMQ1^M+&_wJ0h>Gh25)du}sn%ex5cc)Cm<J;G8_sUsZK5-aF@-mPXISXCescDE-&j91xe}a%fWc!CR`^{y(hiCi3M>G@r&-uPsj{E&o?9cvwxVK@y4MIk=MW}Z>+4URZ-QLE(u+RR#Gy7!ox%Vg3U$H;eoBRE1;D40n-`w_)xnTBnHTwb8ZNKRQGX3c4riOLKPR-bGPb}Z#vh4+%6@u2E{PFL?zquXYwqbXTyTdsim@?Q^c8EI9FIdH4=UAY92a$8Vz}o*~<_qF=p5MB6Almd*1e%+}UGyGycbh)2-60)qxkXNYu5OWc5&rq+c*<DS^Orb=Z@s%BAyD=F#E$GFJ7N-fKaA{&>40Q<CI_|`NeF0*1phWj^lyy>KjPn)_?LcP?)htmG-5xE{ePcE6azFs!tgpsAlsiP{6C9?0YEtMKdz01{{}#M$Xlq$T!Z7p|NhO3u`VqNiNWt-+N%=~_s;mgiMi>ozexPw#CIQF!B>1s=uQ>+|Fzfue)H3ZxOJjL_Vevovk>zlXJhKCFCxfyC{{<kgLP4FV`b>8STubc;$I$u-~bPY*J54|>!eo(w1-dEmgwHD5!yGcC$+)**UsbCg_F2cRYv^pLTc1(c(zy5kgbLP=cLYOXTPP`e(?cDw2*yXoac}7W}m-}VxMh4_xXFb)uPxB@o}{Mkt6N4Kb-sk!+(fxCj|Rw_BH<@+W%L6za#%)9N+S-4_i+F@h)@!N}`<DH#_rc<DdWhZA%WIPfstjKYjo2#=nt{2hX9Wzh5;S!1G4jtH?FN$XfawY~s`hrQ#%yYGV3=Os!DmMpeH7&s10?*2oyMkJ;2-&F%o6BX+m*AH+TW?xX|Q_ACB*-j93!;{sg~PyBODi208l$va{?5bMN$0Pl=q?%N@rgxQZLyOZF{?6*RKPYaUf!hf2s`=t{9sXpZUkEn-)VRaBsen0p8#q@th@!tywB1gBb=D!2+KQH)AiuqUJSoQ|&T{jO);({^f?U(Rkpbyskjm&lH+#0`lc$NJB3;5>N37Hf4{x3hTmH%IUb{ChA7N9(P6Sgc#LCoy;@bcJE7}B>VGMC0+_o7H_OPzz&;jbg*bszFeysGIV7WPXxRvArk$$-vH(WP}mc|FYjwKEksUs-}O^8Zt#=fJB&EBRTpc$WSD=P*a?zuM;^v;D*Lejl>`5#DOAXSnS6QuTg``H6eA+rOQSf4=V_wqN)An;yR?i|ywgzhIwEn8V_&o`!$Jew4)b?lwDbWncIoY3D!MnSDzfD}8L-!#crB=47RBR_6qG-v`g&7{=UJZjsvj-&+Up{$Z~fb875K<UPY&yWl#ZbFYw99Q44b+Vy~$0f;g)fu`5MG9w}Lm4Vt1F#FhTx0&rag85f_o7v7Y?_Gp@ne`p3W_}Vh|GWz-W~7VMhnfEv{|>}^d&K**v_qU<J9)-a%o$IyXFS<`7UrLQf1dYCv()}li2p?L{o{u!{$u(-EBr_I1wwle|1E(!hX49N>fAR_NpU~(zk6jWQbMQV?a5={JEY(L73O$O;M==r@HO8T_@MHs{QvyZ+qiNhABVFxV#9)Xgin0~WBrGtS2qu2ubGGan-(E=b+Wu3)<;p}`yhCvM~yzo+e15HeDAgx(WQmtC;Pg$#bvVlmyR98>BEI6$=ZUXh#ADc+J@GW`S0>P<_>PE_j$EYv;E#q>}z`;A^rVrg@2Cujf863AK}D)jO6<@|8nC)u|JpW|14sE)(FLau&=iL#D1jA^=SUJ?PunL{gkl(uXcSpvTwEh@nh_JvrRqQ*nRu{J`b7aHEeZ%<yGnJ{d@B-dx<~(XPGDB`J$Ti0M8BZ4k6xMP@5ib@CQcO`w7fEl(QcYrE8A%{^08T#~A(tmHk)x!sr2|6XMzSb8V3LXWP%QztREj!hfvhKbraHx{$x(KZfhZzU?Hx9H)i(k7NFQTGIPu`?c?%qGNxa_gD76k;MLS<om}CRQyNxhvfd|dI5pt?!I97Cq*zX^bJ%n|2Z46b6FA+g5SqWWBf6&clW=Ux&B?+;p-1h$zIVfWLHS}Q}h2{Ke~>KWx3LyyMBHGLf(5Ff!;&V%gX~x6C-dmXEP4&T8-Q_DOex#4w7H@M);T>w%221wy&GyEGPAChml^bFvztTrcMsTnIrpg>d-z^6=!4ruJuR^orbRMTgfftRzG3>>tODXCgj_<LNxI%Pu<s}c6zqfyth%k{q%1X>8ECVWY$OGeXjel&)-3Oe(C!gW%$?gy*9hw5wqzzoB7v%0OtY>`*z!+`46bhe`J8!`D154-Xhk0Ip)r$sRda3Yu*~>W}5@x7=Y*Z{t@^$^+BE;_L?!*p;yqFALM=k-enXL(4}_%Ek1yAFOk(3u<u22chCc7KZxPqR)5tWiwZP%THP${dr0ihx&Kr%@6WNn@b9Abzx(UH5Q-C{`96%F;y;>u$bFgrwutj3{>krWN$}&me@mo}Y^nNwn0=l5OCC|5eE+(L9ing8_j?B6eU$G%%M%#c%+7zidSw5DUl;zXayDSk>Ul_*`yM8Y^2UH(-LO3QZ(}a>^|APp?*?2xg3r#E;G3(5pPK*s>4U2{b08C?8EdhX{D02<jrAXa-rc(5o!4H%nbK^WEY6g9{_K^BSQYlF_$1L2`pRo!-y|%kyTm9{DIS?JpaaJAY$GwrinJJ1?8`(&VJ6D+Gf=Q?HR6M(!n0#*{DkBG=P3U7sDqH9%_YYd$NHAytyurolJA%P9{KlB{XMLkW2DEIp5(7Z$!?Dh!oHdDm02IneoUa*^Tjbg$N8Ov{h1>=lI>S|!1(<Uy!(^)e+Q}f()j*X_F0VWm)Y|g*pIi?tDW{?IrP@auD&j|Iv@+r^+})qKZ*`uU*MzPqhr|x*<D`So?_lf!uv@=18T?zSn`75rVq&U4O;qv#jg_n-5m0Y(VG8g$ql$$dN7P%;BLFsD1N`h{Px;E*Zz_X|781PN2wby%)k83X#S(fhD391*+=D>nEzOx*3x?t$G*R>+Vx@feWc3FPh-XZ@Op?DQb%lmRKMqh{V-xbv?nm5tK$E8&3{|6|0#2c|2@qAM&xW<gjDj0CkOcv|6Z8-#>;;bb2+P%@Z@X}K0Z@~hsSgA<oteo|Hs<<e}4KQ^Ph!7yVqm)>V-%RpM_V(jY6O9T`^_Sc!}9BS02FGLwTZ?*e}@_KNZX7Ov3#4MoXTO^;67?1H>QUx&-T^DZ{<6Zeb!0Wp76r&j9Y-itJ6x5Iy@ncy`kK(+_1r*SZKB)`H@#wjy!Fb{y~T_S63c^syL!#eS^p^Xx$XhV&fOQQqf$e%ASZ8~dX=OP^na#qXa@asDiC#lF~nUuF9_x6gJ&<(1t<r00JW|6W>be}J3v`*kil)~PQw)*><b&t*QA{r%DQnAo}tAh3^}|K39e$xQD*68|<mK(PtmDCyn5Ppx`@=NfsJsq_622mUSWo3}%BU8SbF0{i%)12q3JwtI~Bo~&5$`*rQliT^ase?m}a;a`3~*iPtskZeQ5hm`s;*&3c9rC5{gk?`-sv41Pc`zMM2-yF%l&7|(f{3i~tPkw)0L=Suc5zKzyXAwf|hfv%f+MW3KQ2ak<_<w=<e~tKGE&2Zf^7R+T&cPcKN1=aDPrUZhg#R{kZ~An{flc%A@Msq9muKR`!@Kb5={$V@`!8zc|I7PVahmuq-%GD$%Sx<DjluNSU&7$Ny+{Xm;=MOs#f{VDxN+(TE|eF_PT%6KOOdrKPT~{xPnOM@EU$_6()+_*@Y<jbNQ;<-y_;5`c;`A4@7{o7^8I(OS%}D)Z^6^0t^7RN5dSZFHbCTv)<_`klQ{nxp!gRZK<vkkWL;oqKYo<j=`H*Rbd>x}l%>ZvR_1#&`x5teR=Ixh`&}gFXW<+G+x|#h=Vu!dC-o<554@UHV;vAGcY#%0z_liaJ2%$;z95s&?pkeL&RP#MaWM13dw^~Iz!C9i{plb7A$0)fC)-{B5?#Y$C0?oR9vS-sywim9gwFg|+gZ*(hmpF*qGtoE@gHdJ8Cb>C3cIMBKj-+ys+fNs?+@nwAMyRWATh{I_>a^4^Zn--VxM=zu}JTjcU#FZg^g&V_$T&bH2VquEvxa*Grx($8zO#aeMAqcqxkRp976e)jp3i{|19EvR5kuz#gUAaIGL~N{~PAVV9r~UF|2=2^zH6J%uV@kF}FE26qojGz`e5V_~5`6+%4UP$Ca7*{(n8LmH#j9U%}auY*gm#K<TcH*uH!|5<_NUVxS*-6aOqBv!>zJxnuI0E*{;F<Au8<KFMD<4;c%>u|D=)$y2aC;vIRB?@z({Majrmy$JbRSD|qGYU%yYSUn$+Gv0z1@z4HHyB8GyoZDyilgO@f-Zg>wXYMuoF?yGOoZ02i{0DZD`i*_Buf4}VBEanSvikil%ID|3{Zj8|zu%Gl+W0qjy{!AH;>K8aeLH?_v=j4=+&ju_KX>~)5c>dw2mJMSLjPX<0$=<aIu)Hjw|A!2m>V$u0M`t8*NJm~fU}Jc8>!+gqX&!*h_c)+j54!Smbr=;(Zh;=e#%VHI2H4&yq|^t(VG7Nb^l#vL%2^|Y=2wX8)f*9B#F`di|;ReKZ<|e^^xe^M9J_5h#p*z*snt||MLjx^Q`bMI-rM&|7TPDA3*#+tNCY}mppq44)0u!Qw6*qb1gDgr($0CG)xNgM&E88B%~u}y!&5bZb8VKIIwvEPVQNbtE2;N7O<?r-Lh@O|3|g^{|~R@d|584@^|1!&NhkBSI<wt-1pyr|M0=Wf1mDNc<-&(QCXaWYbOrj{NV!8Nu@hi+VYeuf?r0$Ykrvh)+<PjoQqZI@z}X~0kStPL*C|P$lbUU+m@#xV&>cE*14VHpJJg&UX3KT$F<i)uDP=RPqBTh-0*2Hc3(tne|7d**pIW-`t@CpFkkNPb5Z>JD(0E}xw_XcVx+q9Vf=nO|0Yhi*Y~ByKiayhu4}a&x6ZS(e+~Oacqf2$ABgk=J^9l=v<~211Z^Jw7A|`aq5I2IYSjbWOFXW>tIRz|1lAlE*n6xj!as8n`|Z+b)8`Z9*!RaG{Oh>@>Gv6DYX0o|tG*w({Wj9hKi7+xf4&pV+=uBJGy9{_;`_H!eLo}||BL<C{Ku2+j~ZGJQG@CtV!#Us>H8BB&3{i7|3`BEkK+G;CYJc0Y+mB*SA_o)c^hR8sEBO)`gsuudFN$}9MTVc=x5p6(~bOqS5R4${Z!_X-tb54jMuPrK@^HM%){|L%Wx@Y6>b!)!;QRExPNHNQ~Ce$(G6TUqIUHhFW7^!tgYCyZYdTgL}EJq{eHs+DL<ea#U?LL!qm6l!1~3hD5H4caOQgKS&@W<nQ!2&m&W1US0^EC`a4LA4#pbNA-l-m&sx6(dp9g5J+Op)i+RNVTj=K6Uh<*s>XQBM)>!6vr2aZm+kHL9$Fuxgn~C;UvwZ9W@IB9Hz0Y6bH-9zb6UqC$MASWR!+tRL`uM3HpSc#hZ*u*y+LuqT^_6zjdn?>+B056%n%VMvV;twx9M~USJNGr|1@?p4XSQ<Vl3n(X&A&A_aQ~msvGg2zj2|z%OlrGJ&NmSFK8e|dV)BDE@^5;q^v~b$A8VZlu=M>h|D6Bh-k-!E6$>c8-wknO`(q{dr}&TbQ@=yZf0)#dxreNcx*x-RX5Om)#JRtCzm}o{l9+#==7{%cia26FW@rOM@xJ;2br8}|u^-Y$<^3Z_BK0#^+kdP7pE#5F&sZV;|B2j<II?H8)cuym&c^g9W8pWXukhcSewMFI8i%QGz9Rl~S(afg5y`XOz|<+@G5774Fh61{wl9cA$>wyN&RU5J*(-20Z#AyvtiYr4o%qk+ep)O4Up>BwOXOdiJdlYh-q)3%fdhLsitS#U7=hXEyo#|Sy=}38@dwn~Ug*=q3qC^!Vp7m3On-9<5<_QUSxPLnESrZtYZnXuyuW~-Tj^sFGsynCb+GY2v0DR#4sT6t+w=Rfx8J{=ZKhA+G=GbXG`Sl+-y`)sX5NW?AH_b~d$Zdw+}};^d-3m##PNE6q>1H~EjN4o+--aQZ1=sa_dT8ObLbes+_Q<a%)(pm+}P@5f%X|$j+X~m@&G-?jYInjAN^=_fb1sX-6CsuqL;6?>@TZH515*v(E$-ob5vF_zE`x<9Dw0JM#lu|-%{xS{`XAK`9QJz?DGfee6ZYoRQyLu-p`T$h!L$N_s{*N?ElC4Y5PxhpC!S<f8>z*h#=b^+F#lJU}67RMD&FcVn3REf#7Zw|2MVsFaH0GDL9m|T=wi9&s~qI?DaUXZ5ehiPsEbw>6r7zM2zztj6PZih<Lh+kJYc6JNozNiooIh@z%sqh?({()+WwH-l`NF+r12@vQ~)wznY`?zkg)M5Ac5rmn({Kx+EJXiuR&ve<sRvGf=c^BX+M{f)%N;h?z4LZ@)sXDZm%L<PQuT&=<o7_M^84V_cv=UY|T3v)_FKN#S#_JT(TJmZoC&nnlRmumsubmnj`UZ*OJ)pZM?2{6C{=p%c3|Lhx`s$IJD7k@n)3MT&o>>U-wjEYh!|t!@)eymMcV5<B~fcNfLJk78eH{3G3@#%F$4)cz>@+<Ag+4{jGd->ZK2ocmEtJX)JL)WV%^T$y@6bdcko9M%EWejtehW{1h%?jMZ~F#Z6&X5LxI_Xld*Z|K)k?HY*CyHjf8-~7x?-><6AIL-(}>ApYJ|4TZ6pQGI*-j5xna(+>I@0<9F{JX=wVm@~M!zJ&}zlVCq1oQ8!Y(Mw?#54Qe%zhI@4{eAD3;$sh`-k;?j+lQ25&eKD;-BY!W3~McwABBY|HK)u;Ly&c694lI(8;__IGVWz#aoD(RmoTtKO1q=U&FgE24KAJAovdKg<*ZWVMPBP2pHNIlSd85w3o*qVaDrNofwS0<PRL#z7VJPuEK@9HMm+p?B}k*&3$X}_*e$M|HG5o`+q*Vg)0>YaQ0v}P94~b<3;2L?B9bUIXh6iYXkZ2%dwIC{l$q<NC=&YkZEsY#@nxA8rk|8Z@(saia7fCg7^rmU66upD;AR8V7tE*dnxwM+Q70D*&FQfe|NWzsPin)p6vhlZjCT!SQ}xUg=;aY9@D{gyMtM`&GY%HnLc6P-_E`~d7jtL!oHt{eSb&x&8)bQB-=h~%|7=4={mDj2RPrZsK$Pf<NnJSCx5}3GcbFz&7K_YuebID_vzIQ?n~DF<L3fuk#<);gGX#C`t<2hb8euki|j!T39!#lS%0?n9xF@VuWkOvzHeFdw7%Ic!vD^r`Ift@?0ksQHy@)+u1MRGaG&<Jxly)1vP+KVOnp`C&-}*<`-=bA5seW&tP$D%28b9~pV%k<`@KN&Ji_}vBYi*I_ZP$LlMaaE{@-qn{I?SRU&g^5ivQF3TW~UW6OLuArI=tjO1CXR-r7`bqj-L4)C`IhUPIVhFJjhfV=?p9ahUtY1knu(Bi_fx)G%bPO2nZp3veQHInL#*!DX`j*Y>Ru{y$*;D|g}hKYm_2|7`y&58zB`4)LFZlf|S1_Gh6Yf0xYb9L(O1{TUmPyJZbB$RF6UVgbefX;?Ksi6jy07Nue{eQf8NrO4X20@<5ZlB^KP*|ePEeiG6F{Py-0>BRp#=<cfcXZt^n_@6nnjg9*Vt(RFpi+}H@{=LjRU#N|LNA@H1{vP39@9`C%-?zHmH#6eqmSmFcjztaZTkL+M)4h)9Kx-Y^vh$}F_G{$dq663mm@?SS;l59w-ravxxqzB#h8>RIhS$vCr`|oD>TRE)wCz;aJyxc+VkBm?{cSU=!TfW+P~C5LQ}^68|B;4&UGq0}KjmY#lb-TO(`#mW(0JzDNAVxy-Hh08hNxkU75jr5$h`{YpKsQOQT)$!f95}C0P)Z4_g7Eu0SIhr=f5Qd0`b#cM(Oq?I6-#*M9v1X|C>>@cb({fiajfFWal!JZdE<NdltuH+x!S@Ob(WvK<i1i&I?1%iUbsGOvlj-`aZjt<4o3aT*_T7w*Lz2fPyu+y?;H~|IDZE|N83DEsFn(ai)a%&lWjRl!dCoJvdsh3*~t`ag_AH5sC>|4(04bN!B)$W^ETK&fJRPJ)2RydlL#X)??p}waDAL8o8TSB4_goCB%N-mQ~ofdLg3bypJC4E~xtxvj6pof3L=xe-)2OFQ0|`Na@k;WMe<fx08s~_g6E$O4!z0+2{A80^GzdB-FsZ_3nMF=`Yp4Po~GzzPr9={;hj{49~GT2WY*8VVw;yZ(H?+xsNf%DGp%!*{S%{k5>n9&G557!zJq=dJP?H+db@j3wZ+h0(|2zCdlcwp9BB)J7a1N(9MB={<p4)f36KhS!{oVEw`k4%FPZ*wPT9sO<OAA8Pws;L?VVZLO8J>KB&I%A3lI{|Igd_kL0<Ze#-Y}{^K?Oqnq06f5QLtmr=59F^=bLRQ!|u=Ntg*fXd8Os3aXwnYohKUV(~?WjMTZ3CgxFLK%I{;q8l2PI8QxKb5r-r}r$wxxFiKe(!Qzr03<_mAIC_2G_~<-znXKC#SOU{lC>d|MTU;8^nJRPEq`SlG$hbFZ^eU9NV`C$LM*SK7N99#ButZlO?$#$LM26>Cbtt|IprTIIwFIig#{6!S;11*tSL_Z|fTD+rE}$4R%pn5Ibk8;-7vF?HjQDZ$kXH74D<8uOI2}c(X&@>+WP@pY?#1{b-)?;~Tys)eYZJzs?RZKmR_c{xC=O4gZQqJNqj3)VpuBIM=LVw5^H@a;zPgTR_%4zm<R69Si51*_L}5+&AOnR1fIo>w^weAN;6w0Q&+Q%Saz#zg{)g2zU?a``+$OwH6Ou_v>Q0E!@S{_s=@O%0K6K4gXPQ=bQ2U{Y~z#z08ovu1Mc@vR5vKdrkdR?T>d)g!8S5Va;VHjqIjk{s$@k!w1$?_v)Gd{>=V!h#v5a@E_A(>j2_Eg!6w*ocN#i5=w|+X8w52df5eDwU^$eCref4YE)&d!tu;CIJI{*PLeJ-MS9_M)(V`>CfU0}Gf(k;_DWo!w=d?b#3g$BO5Q45FIbD4g&T4I$Toa=dLO?3%P(s0|9Nmjc66W8>?{72RFNDj%tGb9OnLh_#RojcbD|`V*w4nXqHL7sXUblm(#$Qy|3(xM|NAqP6z*7${X6)%4*Pd)!0z=+5F0WLy}EQpz30>}6y`s8M0>?P_wog(eZ8?-)V}`CHXWe+e>?X!;kjL&+vgo#erAt<XYmK6Ka}f3V?7+$=NnVX_UqoAK>Iv@)F?+WGb}Z-Ki0!G^J{KlT62F-<KKEW*LFXXK7W>P7n!qj&;evSxz6^Z)&b@Y!I=-`c3EwG1H2!F?*k>6{winwbxuHR{b>F-c5~pLe-{k@5f=W#jh*3{KdCRbSH0!3TTbtk_GvA5CU^%$*id@%?U^Bs5jMD?x=lS$-KyoA)e+qHH-OoHmIR2>{7VebTgU&z|1*aFXPN)WDBiMAxUV4AD>K*1+wA{W?pa08)uadLxoesD_9yqOApYr@$$q|_|8w*?=QCFb|L5srSMpX$?0<d#2HY;$f=87Z`00gW;{Ts&<^Qw$*Kn?^K;r%?iu*;FdwQ}|lI>?XMjxvrso;+n=aAjcM0tT?pL_gEGARZi{_X73lXU_61QZ_>kz{RLj<}HN=;hHF^<Q`fof<xa3Ei7wF7r>^bMJ0~7T(do!ui`M%gruxx66Tl{<qfb3;+6#2lw_y>pH*c598gCo?5)*&J^#Biyvd3D~+`DobWR?&@z8&>p!!zZ@5ny?`hfZ<2VavcHAe8t1jm5c1=3qJ+gy?JRP~{HEakv6;}O^&;eX0;JpX6#RR;2XqI1>>i)mPOzt-RV*-^Apt!g5uk3;91COxq&vqu(@Gm-`o!TwMbEe*{WDcG0O@t0<CO2p0{tVxsCjKS%A6y^NL+T-V&<ltni5~bIqKN;f{)+$Teu{sVU@s^8Pl_OR>Le6xUWlW+SIZs{=AZWgaqO>r|5f7mGyj#uzvuw+0Zx-J|L1bo;#?N<PwZ37f1d3A#oSfG|8=taw@NnSe%VfZRJ9x5T;}^<zp0h~Pw!sAse@$C_wOb<Prm+s;+@3Genr6^k)vA5^LI;b(9ZrANA`Cr_KS8i`&)#4emiT^a<c!^(c8ln^`C!+>_73}qZxv|d7j5b?l2~d=DUop^wjfOf!xcl=J|zvf4#Tg-_CvXNG06UXV~YNzDWJMz`rL6dRJ^J&xGllvdNl%$@^K^A7$S;Wo6!ovEzne-tCiY+3#a+ft&kQoEI>A0Ig!>KSA#XH9LaM@A+H9YS0113&+xb?ajY`X!+`Y%g&K*Z@pV%&j8;>d~K+@zZ7G+xnkvCcps(vIhlL?jL^4TMF+^;R}))m{{0>K=RS14CmlMh1xa(cH_10B4F8cs8X;<E17g2EqK4EJ_M--}X#TbB=lWl?ssDE+{u^2BKfMt%Z4&lvNRu1D%s;b#G=rYI6#qwweU^${OC0z=lf4FKa#rIk3FrDR<*&u%y!E)0w+@#J*5k^)b<+2L@9+*hJhB^~oZ2gQz@NtdN4GBu|BC%AM<E+uNjAPBpSag@I46T-hj4FYzsSPA`2E8FW*pd~^Mhjlmx=x7eujE4JPVgb&tf9+AL2v&2X?k`&v#i>&!+0za=6vyZ@=5Y@xHP7&g`2Vo^1c+My!p0PaFH(FP<>Q^1C$JvTw?^Z_dtqO}jmkYvSK>_dZT`0VwvZyTEuSkhyo2I97@C*P7ee?|XN_kZz9lv3LL8a9^=Sc6Izu@CD>HVe%rWdDc`XRJlQS%U&_{Zx%Vq!awh8X5Ub=FXC@@KXsDbFe(S2dP-&IhxD4Zwe6M*8QKzahcqW?Dtl>oH)SZ>eqx_%e=);U?_cy#vj2nYsM()E&ny4m*nbQEb3K3nC;p?~d6D=}M>*Sn^7oJIT!C_uBiomue8*B8-9h~CT!IRcD)Ir2b6?Ni)i|9^@qYdWTrAjtO9dNoW&b8zDcpps`#0ix(I(tHun~95c2L~E>#yej@vTcZS&~Ei?{;LrP_r*Kf4AiROZRRQIlx?#l<<@N`wZrv?ElV<67TP~`Tp$pEB^O$Khe6Shzq9ppZKrO{5SpyCiQB82w!9O-Q*@K?`dQHnSIIQn0@`a#&34^vzS;vtcKfN%zuoY*HbrRy6RhTp1S7mA#-E4*mAV<u1OCE?yc-6k2AOGyliYI>m9$$ypcqG&o0H`p0$^4FPQQ{EMI5gpW|N^_9LcwJMIgUx<I!s@SGhg_Va&=4(L>RR`n0okP{f)r?bpUsa^q9_p|aJ9-w|VA_6+w_z!pDKf;-RKkhT-*+0G^-<r5@;lMxNoev$_7!gCc_TL0C!y6)&_>Yy|f6f0u#XqypeZRc(H>#K2{s%O)^Us^Fqu!Yy_Mdxz%62Xj{ts_oLi{h4XZa3o|B3%f;{PQ1`lqre)+alEA%8ut7H!70;w`vwV5<ZFcMfeM+rJwh6911Xv;Hdn`ED=sUqSp!oNr=%_WQ~HOKzX+d@1?yhIf&SO~SlzZ$$h3;{WseyEaSw&+PBpv5xp(f*9hzC-YzDCvppMa<7((|DevYpD9WAY$r(nmYewZhW#+ZyRThpyW7VSX?DVK?oZ#AR(Zd!4*c8VOU~)(86PuWYPe6Z+I@4Yx(1Hpbq`_uQ`k?ne2ta=cuQR{cC?*;BkTu^aLNgC+}v}dzlsMw`NRJR9l&=_Ynv^UoPe)=4|}BSXmu5_-f=TIVta2`C$$q+bU+7vC#s#)|9QVu81Wx6td-(_a5Kyy_UDlAA39jwrHB&SZ|9%q{+NG`0V4Z7hsgfV(9_O;WKSitx&Z#>_=k=E@yK13EImMF#QLFaON9GFTNmSquJLhQzk;MPqjvtU*T6s9{|_zpU*i9#<o_NM|EEgxwaw>Tf0pF+E9l9wKKK6|*rRfLoX3}3zV`9O@7E%+zlD8%?$hrVY*#%2+gHv<<m~r}e>XI!`y4tqA^YFE1;UAcGpmslq-M65eeU0i6s{Hbp*+*4#ik4VRNXH^^KbS2)!c}?<mr-Z{CCx2YX6Go1k0_NSc@3;&3$^S7=Dw-+w*x=_El~`-LX%zh)oBKcl;Vv4>Y+0=NzKa0W*F1eqi+(8}1S48kvB$w}1BI*8yMur~H3-#HY($Wak<o_YS}3O*)>~4|8JQ+;R!^Q!^nUzPdk5`omS<ua$qk8831Fi0b_F&G``W{iC?&cX(68693Wz$TR;u_e=ao4ETxk{)Y8=2BBQ<@1=MD==~qezh47K{=?oHgPb+VDBZG<*k7!k#D3|<`8c?FJ`QhNBsG5V|0(Ww<bN$Lk`B0-zY&-BZFKbii#JmIzu6Z5+wy;ZscrxFf0F-C_P;bw<@z}1SDY>Tx;VdoICmGt_gjhYP1v_>t%$1YuO+V6%g2?FpD+HvCQA-*BXPe$gnI|GH?PE2?*E(hE_!&nYW~Uo_iiaMJ#%l^H?}{9b-bzRTi6fPzCbwd?DVgNeVs3g(>Z$f&(q{4oR`RaodZl^PBs5=&is$@aJWrj+v`{DR;`xts*gx&0b=)w{fXT~=4naO!hdEy-?k&L2JUMT-m_)BA!xlL-hJ{*|6^)|d>66H!Zp=;1y~0R?dF0RzMB7$uDbWLv;C$E?{@Q5!oM@&PW(qz<G;1=KZkvP;y-vuGlUIMH)*28?l+~lpL~F!4TXQ6{o~wUSbsJ96T-eeaUV?F&m{>n`+wX3A7}pG9D}SC@x=Wik_9+G+?S9D|C^MUKA%eR0gmrc{GZ;t0;jXN4zP?w)dA0EufU~zlmA;Q{G0s$qsm>n{#P6Sf59V)|4$#>hpHmZ_h(66zf$(~?Z&~qJB06oZR?P~buGynd2)Xb+keA8=lCVhzk7?e{lxwbVxK-Im%fI30QYWOuK9ltJw3JkZ}vQ1>Z|xqwD2Ei^17VQ;k{dWuE&UB-^zXDDEmCW$qiZe@H6|&{QMW~{PUCfPtkii<ATgR3H$x2xG~l{Q+D>PcWaWYet;H}XH2s&ZzOes${8eB@`W*GZ-`}9$azP;)feEL0N)WD;8{IR=Doe#cmMagDfE9^_(pO~`-Zr0_`MP8-`|0KJ@-A=yCdfMbWr?r@2Kt}xAGsQ`~I2#u;Hx`JhTPb{N@z%H$&*KrrP#5BmNr;|2+E>HI#b*>LOU!e-^>LpOKwE%zbco^_<JkZopLH-@Cq@f4+P-_w~`pTpoi1n-^mLhIuGln<`SYHVwt=(uDs*n->uOi%_wHVgZuM9pnpSEW+`O#W=ZZsr3Dw*trmAx$i$~InHzMU*1YwE!=?Xg{uC4|HyXT|5F?P|C{3fOE`J3Kx%#!#Q#xdzhE~GQ`}#YwUvDM^_u@R$lJ0S`DFW9xSq%P{o>3mDB+x7mXiIX1NLd(U-;j$R%QZtHejpx|L>tU#dQtqJum#f+_$yV?s#@PN#?iN_b2{&FBjW)=3nmhxTqYT=HAqMj9B-=#{^l~?<#eE!~DV*yJ6v^ZdgE1{#dHy@zm$WS$c8fg6#a;*f;m-jEpsJ+w*|d7~h$B!@cbunCt}iRI}~Q^TAH<8~tGF0y3*ie?RYl^rv{BUk&-P9xqNphl*=Iem~%|KcjQTe)JyHzuNz!Zx0vD_H~t>?_hn$Np?N(ZHEyx^Ut@Tqh!up@y~l^BlP@#DA)dn5&y%Q$sLMF%|G}4M-%^C|L6Xnx&2>2aKE2mPVZ+iyXP|`z-;#Sy%hIzJ%Krz{}ImoPYJ-z#Zkn5D)Lq*B7aR1_N`7vA<Nnn99Wl%LmTIzY;!t}Y?&`owlNilNseqv#nCP4sMwZ{N|IyS({YOQ!s%U0a3Om+uH>&4{<;6}!@q+6hc~(Yw@>Q+obM~&w@3Ilalhh!tpodormsh{Uz)vBq*(ahEW$oO9{CD6^mV)kkZb?UKllH0-nw!9=i%1u1-#O~wXiPyb6*YHZf2VILGo=`&A#mF(zRXb`O<w~(*LP<_A2qP)aO;qZ|pvEzgV-+lBQ$uM16nCd1lAln@iO3zPag<p!0PJTCD7+O;j?EzP}Oku@uW4K*N5lL!RI4x$GkLJ=!`uG}^;f8?gEv{CD#AdV53{>7A`9FW|m>Guqzy?nkZz{sk^Mhil9S@SQ^5gKOj8yOW)NZ|1*~jsGZJ|Bnvf-LV}c{~zV6Z%d8f-k+9ohlX#^MQZ-J_djx|n)?eI)PU@MUCi$D0%rDl7Bh(bnPl&0@UGu(iht1o%s<Ig*XsPweq|)K&ksYvnq=gxibvkcL==c52><)nCXo(EM#=gVlx|3&XEF|LNJi<pBph6ufbz|$DBnVQfS$~M6^Y^hsuTap|F5n8_uuhQ+y4sTUddsu^-0{X^7hO>=kQphuFv^@VxMb)2eUIscA_j-JrD5L5&vxaSy$w2UPbJa|4%W%_Eig{{@=GpS2S)=2ktHEU`jvDKiBAV4_*}Se;KLR50g5Mo9^XNyPxBNRL#!Zfwqg)M_0Cg-b7Df-_Cz`VPE3^@yhm#oww}oh|;@0%#Kf$?{n<mF&vwEzF|C_{R)fN*tgsPl0E^=zTw~M-y3~k{#@C6r7z;f=zbxoKdAUZ{x14|3kmQPU!a-}AZEHxdHqK}LnN`lt|IAuaQ=Vv?`gl|X!vLUKg5T*?_|3LYxs`|;Qqf3vTs6e$s7K;?|--w?){CVxL@6+Z-kJ6^$|Rv9%lEgi<!Nj$Ml{*!8B%{?EW+_&A-+Gb2R_&KZXBXvi&(L<B`2At{VUQ*CgTq`2|I55>QO<mk|5KYvP6fGSUT#|Fl~9zg@ZoA94JD@oD|P|BeSYFX6<2TpRy}{SvO(XOJIHvA*bl?JDkP_KQiF{X;n!!hM;{1#*AD4vGO3|NDslJmH`0|K^p*-MSjP)-6Rs#4Pmh;e{skiT~zy{u5+xLl?6Bihb$l;k>`*-{kWWbRU1R<~!Mzk2iTZJNq1eTlr`17iyVjwf&lXGutCQK-!P6=J1Vuckbh<js1D@F)xen@9EI<W8WR9^+1q4&(BZR5eb@m7Skt`sC5y4PtGYWAf2{&at|ybi5u&Mmj~C97Z^MMUFI!A+k3zGQR@J=)!St*+IjEr<N=*A%Ukb#^3nYB%$Q#%+e{hn|B5v?f3*E~;GgV&$gpO*|F0>6x&LoSV}uN9h&lc1V@97kn9=J6lIJn4=d+kf{7=*TPbc<gvKaom)z1Gm3;%g5laRffbinEa>?7{?tx6EtPk+w*7p;s#(W+RKu1~@t(gSS&E4H%#KOZN__h<j##Q!Sx-%7FnPJB|8iEplyJeB|Z*Uw4L??}Ne9F)Gk9Wu)!?C-F%Z|pwL`LgY2-j&_og+t6g&j2z1ncFn~8_54(Courm{#bXgACMF=2LpO~k^f%@U0T$|tNq&$|L$_bHAd#Q-5uBum%Z?6zAHiZ@H3C5mY>QT=-wM<K1GW&`wK1X^ZRKQ+t0RMv2X7IR{Sac<$Gyg-fGju&)347v&^5=&Gt5n-Tr&pWxSo`_%ZfgKO=Dxzo<X2sm8$kn;2t0`!me{OFcvfu%y4(9q;>iRO=ZO@pKbkz$LF-^6Nj!Sm4t?qRWC+=+n2S!~fe5FBiNzsI%C9$^ZMPc~R~Kke!hMYWF|S|M2d3WB;S{E$LA1{T<#y_5BWQCVju$|1-CLBg_)^>taUlI+#wje;WDz%>Vn{75~$_>-fK$ivQ<mnN}13FO&UGSN!K{|38mxKihxiztHf%M)7|@+y6rp`<Ii==lK8l&V@L$XF1O9S%&jDy8nOQT3hb_Va0CY|L1)FySDzHzx*riT|0}T6#F0Knf}aeV*9zbkMsC!!wa^rm%hGY&h_oyBL02p-tD9-$nNK9;aWk?PSOX8fA0HX{>|6(vygX)kS<7#4#B`)-O;o`UGo3yVhZtZ=JT2TNV&7E_U)^fU-j@AKb(E`dA3}BH_e~!>D6}M%6~dZZS0$S(`MGkde7Iy*~yx<bopAVefRo7sgZrl&VeMSo*#RUp<+K}yy-D?tSuOyBVF^qXtITWda^#?xF9sJE5`Jzc@H1w1-hgzMccbS|IyA3cqF7(`~MnDI^g|bx<^FYex4nbo&PMvf1K?9wfq0v{}al+zr$Lp`aiKhmvq4FflV-LKx544+mP6=kLhIlr}cgT(|c+D$@Wj>8XrAp>AIhhknT#Ry8}K>{LeJ}6ZfnGto#?OCZC`E|Fz`*uS+5JlW~yZf6o6^Y+p$J{zA$B^S+Psxoe60HMqKegX;e|xK;Q5?7^d|Y?=N4<qz<G_sVJM?`4}`vWM;dCI|M}ZY$f*JwTg<Z{c6t{<6H?BK$tvf42QaI{(MtL-YXm2~s>^<-ZB>-=$?e;=irbZ(;-8b^o?qOdUTq$nNtio8HYLcK*fo>wR8nddAP>{1;6!pR0J3S=}IWJ6f}^_kD8Co%!d_v9iBlk`k+pPqX;{qFbD5{i!wd{u!SoX`B+XcTn{hsgD^S$60qI>sVx-eq9T+{bwD(!ur7ecPkw;Wr#b5ckf(dzd(-(6J?%|bL>C5KEcbMs@>yGvxB30sU2|4KHL5%ee0X~m)oD(|L6Q4@BL!-Lx;B_?ptE^;O3Y;s3~R*BpJ{I)5-Qv@7sX*uZO8*``_zj=YM(+7D(=YZZER;^b8>hCJFDMWES!7+rTOR|4IOME{s4v+kav|pP0{Im1wI02=|=-+n6T#KhFOj-8vt~GM3=X-c`7mzYdoQHc0>9jRRY7ll=WVhccx1@8PjbJgVGF>=)q6^9A_T?PE{j|6g$X@+s-@mHj<C)@$z9*>e4?132H$_5G4P+r-W@@8$dU%5L(C{n_$%`Myl?3AhHpJAqh_$Se@|1rht47hwL=n171hnl`LQ_P-upA^Xp_ccbLqo+JCw`sOq5bvHKM%Dn^on)_t^RDOb&?ek3CU*-InH(f^$wCwq{&6TKJVv6R>aPQ2&^8dS4lNy;fGSN{FB#%>KxHs>oj<@#-tEZ>V_Zi!-Vhwpsla&sbZ;4N={ROO7=8RN(g{*f5taAiiQkS6p`N#io_XmH-2VmXRGcd5)|I_P(U2N?0{D|S-`2Rfr&-;HuN3@as({qQl!Ys1=vj#QC%z=vk=>v%U{tYp`Ujw|~+s^;AUMlxDqo=C<&Eb0=ebn8bFuwaq??;&aUk^wAn@RmIQ21xtpHIB!ty26Ku1O`IKNY2$(s77)d+l6|ij1YG+Ora8a@XNv(N<h3-in)tcH)DwJ-EekXcz93?ZUmIS>*HY!N(_a@#)EYV!sGqoX>kY|Ns7Hd~opu#q!&wuUGc=Y+onwI`8et-?mQj`0TqMAodRv_Z3AsIB~ELXO5KOT;(C0KXwRbD-Pmx`2kdw6yQif7Wn}?_1y1PJO6vPA(y^)N^~&s-_6GVOa18QulYA(-TNr@H?jT7?$ySAvPC2}!2IjjT(Pg%OwoBi+4adcx&l3H+?(Cq@j)i;w==d#=hqf$&T8e{k{fuc=w9IDabC9h!DOuu(yV@h)Av`lUF$Thi;P|}?DPMlwZ<5yC7S-fULWdSZH~xxgZ!nJ;kqRUtv~tWkEloRKhSx1arNGy?k;j0H%jl2QTJlh-3YD$@XhE@-u*kg4dxR6v&r_)9PG&d4D$V%|7pbkG_wE3|L6SQtUfPbPM@D(Zr^9r-v543H-9z%%zi`<#lL?I{O?*Eg@Sdd;sX?{PDR10WE5{m$HC2ur1y{K|4(GE#p!}gxOgA~*UPeSyJ{cqohZb^(+BYI+(A4(TZ%`gi}2uN0Un>;kB?8~;S-WC&h8ifzrL{l2l>Bo;W*jtZIb7c9v{s<3-|D`AJ6%|vfSNd?{jhL@Bv&nb{JRBRN=<?lel&14C#<F^nFg^^2rLEDKEvb;(XB+rQ{D3QM^#hH39k_d7A(Jy}Zzb_;;oF-|!z3=<YCk$0B|En*Ee<#_#pCNtzS;NwV9|Q^fGE`hB%Ot{F<w{eCf{O`n~8Z@=v77^CX^b{k8+HO1A7w6Dz{6FY8d{x!*8$-nYPs*CgYF@A&bG3MK1lI}Kt#MBsUyG1Ou1Dg)u-9mn9*LY37fb1O)j+8rpKc?OQuW7TZ)dPb1sC!Rx7tVGsM(vgr_PyI7cvu_E7Td4*Xa9c|`Tr^gXpEU0|M%1JfA6}I|DWBr4ubm<|9tbO-?Os&H=;jr-&f!L;Q7DaY7U6|0G#=sW8uGWeF_TKrJ-cg0vy`12;~_oaXfn+PUUXI`NHkETDBLrjuqnGsS-TAa0H)Rt-@y?oW|$3&fv@2=kVptQ~3P)F+90^1W(Qv<I77W`0^q>FBFsgFTgKvSN;J1RXDg;@vm(D8vR@=J$xMZmt<`hyMCf{AI=>;h^wcog!y|nF5$tgYj}A31|HnLj(gWH;U<0j(uoS3EGxoMiVaG0cHuzwcF_UZq+60A=c0cPPY3=v-zRrm^iF=3q;W<Riz*(r?(MYu`}}#b&)ekw#@l!Nq-*ZeEHgI=W6gZ6o&RWk-;4Pd|H0NX)LpnY@iqI}ww@k~{kC#DZ-TupFz>~ui2fXt4@i9q|JM4V%KPhBMC$=to^ib6J^`x^kX}Rm{~QtEA-jeLdDg50I4{ug$mJhR4>8|O?lpX9wHt)54R+BxB-IXi-ZL5M(_YvQ8QzZUerwDcs`#HJv%f7cXQ;~mbM24w|FbC$nBBhtX7{UyIsNKlZvQ$69Z&~h{nhTj2+{$}Ki~a`a^m0F{&W04=j8z5zlh>{w)v%G^AB!cf}^`v;$-$loXOve3&lHd^-v~m9L>XBX8(LSKE8eeU)(;AuO3{*H;-=OyN_<;yT>>2?Sm`$>JI6G>lOIo@*#Xqdf>A&g`@-a;paDb{`a>}<-c_AR$G0aeSU|yfA<zU|K#_dC!hc7nd7)~<pObk9UtAljZYum!;{Ak@X3RFczpL3?q0tvpL6DDDd~V5@dH=~2>(%YZ2Y^C|NpY&|J@y!xBK#*7U^0|4EG!#@C=V*{?Bfkt@C!tPPX5+w_Er7N#0$@+19)o%R-5>?d&_-Z06U>ymLQ*gG{tb+622;b%E`BP4sfedHx^v&N{x!JInW<x*!FLL)<+CcMC+2kdWXG5n?1Hc!apS8}UGbgb>_aTS{BnQkU`B*}HDDcenSP@Ar9r5}`Bg+%28$ALsSNpwDD@9~WJJUoYq2VWr2vZv9Zz1G=+?ymNfxbTe_cyQltP=E=CI{wJ|#Xz1tvE^iM_n$W#Jc&eeYKYA4D0A_zA0;i0C|D@r9f42YZ|ND~!asU6MfwBi+qOAW1j_-$naeWZrLj3d2Z)Nvqh|9|)FCxsD_kZg1FZ}-=;(y8iER>7>UupN5{gYMcWcPD$zP<pL50&Ej(MsH6{!cdI(fOnJ;QA?i`rs12e)krB`O#bW&1di7_n$w;@4t8-zx(u&&;!4ChjhR_@&&FR#U~eANCz}1{P$-6|Al;bvi}tSH!HEf%D;&HdCsRy#{S31j^`)GBO`t{mM@wQOM{^pG2m5L84kl5KQC037NNDN9-lq=K<I%-_iy9wmGiiG;xJB-A8@#~OvAs64*x!5Mu^(JKKEVOx76_m%=NfeMeY09qm%#JqwyOQ?$y1UBJZd6)N9#iy|7!v4br~GE8Hijwttb<*6#DvGOvn;X}$V{wI)v2h|c%+d1_^E7g7H%{T#oXUx;0#&K4-~fk#(ALdHzG^T~QWhx4>EN2~{Y#+zbX5B<Yp&ro9avww%^oBzg$JD-c##mUM<FAiAZVj$SxD(?M^6#HiVDehPKXZz3jKd%1;O_sa=LMN+xe+OXe`2N^Bt}nKZ<-Nah_h-o1mo@x{_Qrn?`Tzb3|CNc`$o|LTaA5*Es?x<C&@**p`<sh|Txl!A4T}4#Gh<MY5RKJ~U5T|Jf;nrWVc59L3yqb<=xl4kAHI5mzxm<={N^Lp0oU;5os;<Vaw|T)(12guKJrxlABgzBMCtRBHNV<|bX4c2qB1)f2lCRy3{Uc|2+VV_fc>ccFdy0%hHt(u-bGi&J6NKwssf*X{GreR?~)GSJi)o+t!QtmMjiPWf`5B!J^nf7-=%YlN1uBO4fEamea*FdeRTT%)ZIMlUVnAQR%Jg@XU->5WA`mI@qAnt_9g$e8tzn^yhr%yT2^Bh^uW5#yg^s#Ry)wq1sc7vP`-=$9=klO5j4jV>!+KG9v{&Qq`O<dLx0DJ8W-%+_&%0;J;iE2G4~w=%rb}j7?U1)1#CpE{h99<qF810(JP|QQSa6m`2hY?B>#a^yYn9~X((~eG8n<!|377r*#FH>=09XYe}s(htL*)j{PXQEUH;YhUpN1!*8g<t|NWKxUsX~B4vG2yL>#Zo#0lO3Sd)ztRhc-&JHTr)P!JmqHy87#GB?rD60P->_}gzj!QXuTzR&?*zkLy(T|b6TFE!!oo6P_3pUQuGLz&p$$GLv)*R3M%OH$*Jm$(=CN&B!Xa4qK848>IQH!;z45XKq}gyZNpU_GKAOa}Mq_D)5)nfRRi0FDhF-oB3O=Q?rfa3h*4^RXvlE5<1Nn+<s#zGEf-yL;ka!?Ql~a-Wa6Ms{`KUbXY;Ts<?dav!O4pCeM&=XbYMdTI4}lRb5^zh=KuXH$LnSe1Dl8z0vtC*KqIy1su`5$~YWYs9>Vx>{llvA$@WDdsquV49t=sOR&3aIf*)A7R-oU!eLhy14<34$#R9sQtvzN^c?W9$h-Yw0myQ!bsfk`APHzZQlz=Yg4`3B7WmWi#@X{|Lp$<Qv4q<N!I^@rpO&2%)KhQ{0Dd8KbZG^ix`0TAM>If|G~t6h{FH+F7v<rmHc0AYBUa)CJ_7SXfIF4(egALE=fg0ZXD`!Vz57W^`FIDRbjUD0X}|+ZysI87k5tK<I64h<_7Wq$KQS@{|C$DyiRpa3JQ{Ak+v^N-2B_Nc{Ro8BeBqN1l;Y1W1j6W%%%65Z9N3jEeB(Q$snPh3<l}zrUMnF<O95m_usmUyI0SPT)^SFGVF^A74^TT@vr*qJ9}V1O1bsELpMLD?&5Qo{ro!==2aW7`ue&yKhjm5?bT;rwfTul?Bvr+_SDJ#I{Cf$#rk;xT~WDLXN==DyxV+-$e)w?!3N>P^oAM4;zTpdbu#I83qUu=#k}rYpv^nREbP*+uu!diYGWZaFR(*rf2r7GsPq}E>Sdmg`viu)|A%M3FUVnxW0#&F1Hr${{mJ~F%mEVrQ}p@gz5n7lQStvL@E)*!6a(}@kjrZ#{%8J!U0xRXzhJWeTOGCazpni2_WyGJKWLe&*#C8`EE$K(QqWqGg1YQjlqN+ZKQ0ox16F=7<_^}B;<ul?gI~UX8(%&+kB_gk;Y-2)?|RMuy?yh%*yDYuGLL-ybYedSN#yJA2;Kt!)xOv?+eXw;S4=R#vhkyZ9#v)O*il&QJc8bDD8`!%)YD7j?Jdw;SA!4Ud4RWXUll&Uv8F1-lTK0jHy!*MmMZc8E;Wa*!@roz*Rk_rS3mFTbJOzQ8~c%6a{Jr#?#js>AKY88Tk)Tn6P0_GB$8zQn7B_|A|=6#^+9(@(A5zn@j^#f%U*#6+%F(S*Vk9SCcaMoe&jqW1kU0)UUMv&U<OxWeyXi8##tKmdN)rM_6H*7S!(i*@rwS@)k$iOP@@BMdWv_exeQINRO*1W)4J~*Wj}y-@^HTGS=IrTn}fQ|2CSJlTJq1eKShG33{&?1u^t#A{QnRo{^$JPR<Q@PKLRQK52W~C<N%#tLBN=o5X3tGoSv8Bpk;rnop%41;J-`#FJ!qJ8nb!-XDSYn|6iT92l?^a5w~qKLf86W^K9$y#oPo3bF?>B;a89E68{(R>5X=LeXAYc{>Lw$%0Ku2^6kE|j0B|a*^a27P2#riS|3k@y4hjtTuTJZG{xrW#@IMbU)E1C67RXdak%hPbl;~mKL?K=-p4z4uH)9lGw3{2kNvwNMgN~B{&yKJdVb`7ZdJ5-z#WPX*r}rfRJ*Th`!%z_^CkNcI=OvyXSB+{%KlEJ$DZqa`*qk)^^xo+d$S%;`0v7e5(|BuC0TsmRudx#5p%{;y2kl23jf>ZTZx(7)x`Wl^6h6j7>j(>ck!<F>^WH)EB-C-!7&&4z&M?G06qQ}TI=KmwEe`obB%i!be~mR+r!>r2Nx%dK5_fm-XnbQEFA4D_1=4d6XyYm|LOYtv;EiQpZ9;T1d`on_5;Qe|6_^&F~t8Eg@31)5a9d*wm3cqe-h@O`+k{!-Ti+X*#Gasf9Ps=!T*txWE{xehrGB5#D;Ce*40a}d8x+_Vy?C*Q`rBn-nxX(Z*}6EJ12VQ|Ns6Q9^Skl_~-gR+kWoP-9+qrxz9z|viXQzGZ%YT%)}0#$p~||7w_ah!$c$eejBD5VI{qfoBc328xGL;B`dub;Jvr*;NhKXA`f`7tpSO<qqO#4ssDw}G112I9_#~XZ*%MN&po?QI=TLxnt2~f&0gMU#r8)kxqX#=J^s1B&&^V^7bsq_{YhQePgeNu%6+m9_bU4={I&gFlKliFXRrG8yaO=W-AefNfwL{Jak_<w-DeT=J^9&P`}BEU$9~!r*m`&f+s3;-nM2KPZ1NYP=<nC$E;RbcTKFPrZ=n7TVoSZR>CQH*@c{QpuAI`nHel_y0wZpJ{!HtDu6@B%Z49uH^ME@1Z=Is`|AQtE5&3^H`^U9E=6_s2_`CGSmN9*?#rbv71F+fY6>N5VN%a4T{Xh24kpKa9a{r&$0ciac{#Qx<4;Lq)JY^^LM{GjyYHuucpNVja4Soo7>y~<o`2WiX7x4LQ;{WbR;{R8@^Uw1?E!86S&x+fH@Ga}$yT~0gCXGi%*eYa)uSD9`rP#m09Z^drh<#JK@1njB>m+ZNk(g#N7&asNVdQ|<U}-p1*#5U}UMBm0n)q)-BKa3;{-67QnE#O3#+sO3R|o8L*Y47eR@m3}2U+OM`|0oTRrmO*yob-#?)BfU&h@&<-r+c<$0vD7&o+O*ZXKYDpRY2na?kOBCP$#m;IVHVIo}dnW^(VX8Rj?|_c)v1)0{5bFV1D#y1T<7d<Ts4YcTT42cqXy^t|y+ZDsa_b6i_ySx7%d=UwDXkTwtaT{=LG1$6ft@vaf?@!jhJCRydrbZ>yz6Tp6wPOijf>}VbShimztEcbtk`u_wi|69iP#{bLM?C=6MDKh}f{}x;A{?7nq4)|&OH|544cW*GFx2(X5g>x`_qBAkK@&_?zZ8!v9y?t54|DWGFA^HEeUp{62?}FI%TVI%tw7uIAuy#2-=gfeMgB_}qB2c+M48?o=krCpD-G0-^FR|*Tk673*;aJ6$;u0650WcfdSG>>t>lbi`Z2$GMC(zMSi-cW~aN+$QeP7k`uk`<Q-LEa}y}Ey2%YL-({trzrkmP@dy2sa5s{_=Y;_bRK{TkbEg+yj%v5m&2C+V=So5N?OHT{5IQW6&F)(EA)&oO_rn-#XsA=^3G3~tUcmLL1HSe`kyG7-CaICmxNd_X8naw|n29`D1}ozY|NOmeIA?&T~PYpUU2R|l{kqSh?C_7nG(6VU7_Q|bjF6i?02-7Q2uiPc8`XSy$7_@m!-+Zi~^j&}p-@IP6}|LgFtyZ`^G{M+};ziR)3RQ_M@y8qV=b?G~CAY&I&w{JqwibYsBYZAuVS^Pzq`|{p7e0hiafR2&>-`P9=|M%bV=r-B^=1ReT(yndTOuqk|X%k^%VTRhweK?rEANBN}{64IY!aW=jILo|8pJe4k19&(L#}u<SMSjxBbhzOD>Y3vr|KHwJg}5EzV*d}%|C@B-U$>7(aIWm-Qt#oYceVxpdh<ROdb9sB2dMmcSKU7^WtXzkBSy)UY5e;hxYy?HWy~I@D~di)Wv9C}{K?LHQ7q4_u)Sg7nBMuj;(7A#ZI=3p{XD$Cd&HeDM7>?uSDwlH=D)h<x$k}{ZhxqM2iFKf=--NNzC!I=lzqecI-r}~_w2rFK)a{J5^K8T0vslc#mLK#pXnHY<0RHqy88iEP8cQGCkfQ>KjhEj-+}pmNxT2kUc3KWh_3(NW&S_N&lQ!#c}+?b;=|TqqmLWeeka(N8vJ19#+VJq7q?I1v+Ku6j^gXv$MNmI{-SsLPyF-luZH3br0j{r#uYxo_h<eSqeIYHSAe4x8K}?Pjhu*8*yT4BA@glD@1x3==_Vp)xyWe*W?2ovIOBm>yVw;MJK9KEarS7l=mFfjJp^NHEH(Q-yvGcu_}^G_pIfc(sqAa-Jt{N#ZswX>e!4Zoooeks(E;q=?{u@&Ft5kH=<k=a#0d)j9M>l<w(iEhwwGU<yHoq}Si)SnR;JeGjRk{j=f5kD$G*M&^l8F}H%TvoLCaY&e>?2Gzmd81Z~psFyO|fYPtU*go?s^QQnHRNQ~iHE9nfpfkao|2x~ovt0pYF|`u#!Vvl!=9Khu3dyz{W@?r=BG11k2vC;r+0*X2J@$^Qko^wH-3$^LI~d`T1k2P*sj#r+T71E7=tpVn>v?;NrJw=yvT@gZxl&dU|k$Jz`26Qj2NAm)O6=i!rUNATITqxj^?A$)%I5WfBAFMHkp_2}+par>*D_)m|C7XJV2DHCCBZi?mJi*e~lEzY-AqoXDdwP`z$9=ufeB-=cl#5=KX5<JgF#3;Oj)yHKN=GzSuF-a=L6Fe_`n)eGHs+Ije+k#~O|Ld>7g!liA87_AE?sAi}euDpb+S|L^=1Sp@qvol5xFzfR7D(ZnzAT(K*rW8l?s1p=@6zdk7rT7Au%AH8Cn^%J)3d`if3Grcx5rbzH!nu%`Qg4l-mk+paqOQ^r{kF3ZuTr#1V_Rohy42XYZ!6!Q&CU<zHv1B3ieYbY5p7hE}^bgNKx__avzYkKS|qX)Uysy=Zm}Q0Cj$71;sPYKcN_4*yF#2-Mo2v|36H!HpKc#qr?nw&{QP{pv(V6W%u6%p8x6JjsF0i0dgY#on8<#fPoH5{h#muI7t3A`Tw5!|Ga0TBsK&UiQAC8Z38y@x?|RON7$GcV8xOJKZv=S)M$Ks{vbZSco2`z)Z&xN&G_~ozUX!T_q+G5;8aT$no6^go3IDl{MW)`&U9E?m}0S~o7n4j^HduyA8QbLiT#qisC7v8Uxe5-t|Cv#`YB?ei||LdF2VZ9XR;MC<95)WuRsU+7d#Kl^Mt#?{K@`X;Eg^i|HH)Izg?og$4u<_+b+0Q_5mpOJk(tP>i$2KeO>-lUto{I|6UKRs58NP7g%cMX>{3V;aFMJ0vGAm>-Vbccj?vJFvI-m^?3&y*w2^_tJUkpEi0Z+;kp#xxBE*m!~ec5LZkHlyPhtln)-$8N6_yDiq+8p`*ivS_vrXJdOAR7eyCdvK=Jj+OYc3q{D1I&q>rR?OOW-K_gDk5|07t;0n6H7SN??#=*qv?{ZITmOa6nLU()pdggPtxKkW7SpV|}ualxocibQ7g7KE+wg4?ul!v1@CxZ+q{>Gxu8`7~>^7R2DgQ+0UUS%ddFD@hmB;@ki8@ptmiJHATN;<0zzRxDfW2`4*i5d*C7^~Sx6CvfjV2d<rH!I`Eqv=$|yCV9JvPuM?6_FpXCiS^Rj$%gP9Hwt?~x1hE#L);IOd&0{^ZE}|~|I@$E%P<{C{JY5gpF0))#SUP>yor#I+48B`2SB!8y92<2{vBKB%>!%Z|2*{h-{+~%zKHu3?nNDs>v~>l?%rBkzgPQxJ+<7&D*3?m(=B@Bp8a~hUunKGUfiu1b?)K+zx`U@{FkW3>(0G#t!~RKOC=9rtJjyPnLX;N1C+kOeG7H(lqotu$pP}rz=mmN`n`zc0~i+^cxGb&*0Uy=rEqkx?$!?&G)?j!!aKm2|H*#>|1MhogT(x=<UhpuIX(V`55WGvk$(SQf5rc2{_E0qq9`U9d$+E_a?e?CwlRaPnGsek_4;1S<?af^wWeIL|MR`}ay&d-h{vZY@a^Ax*en0<-McF8{<T%*qc$%cnX%Cb*{~X}Gp4}ClHP|TV8dFxb>*~pr|X>uakiyG#3zl}`%oSif}F_JB2U5kXwx)fgsk;JR{U<U@0;(39Ih!8Qdg8F_#eajfBU{7_~-q<5xoCf-2Rd5N6eM?zql_TbdJKmtLzcnqxk*mKCc*^Js`W4`9D<z_a54PA?oeFZtRn!C^{fziOdDCZ=k86sk^n59H5%-i+0ohH)S_xwwOQToc*X%cmGFsYg$C?*3WZ07-NTrwX97lIly?GT863zgbvV|F;?}&KAk&->P#{3CH9|buE#&uD_GAxv-<<T`cD|vcfiioRc}w=6p8^hOdgHU={o%L{U4qIoG9;qsr(0xqc~t}zi#{oD*T6xQDy+0wfn!g2asz3tC|1Ty2t-K|J$6q4+qG<&)OM?u(jU82e3Cch6BZkLB!yn!`!|tzBojG--Q~!0hWXN&DnUXH3#n<E5^6Kf4}$pfA_E9OlvKU5}$n2vn*pj_D6?eo!=5no!|mHD+?iOmiysUYaQ;KJBF*rn}kj}QkJI4Q>F$kL6q+dY+AlV?D*zfLU~q_h%4GE^Mrok8z8%s`G3Jb-~VtKD){HyA5nAV4q)BeUJ-MZ{lB~i(A`Yfd(A$-g_fEdKD%{lfV<pP{w?&l*S!P8?5n*zsVv@FN%P^Sl0#df)Y2B)Dt-M@A0%<@pG2=WJ~r*g+kbKL4(w*l(%*eC(Ok&^DD_IEFCl)Rz8+9#jQ6WLLPrPe_O#OHf0j9>+ZlJ;7sPqXXLBz2zcA|TTXIimmwsUGrwy5|Z~s;P{U;0*_kYFye_j5^$Q=MZ@z3_3a{%1`$9Dj?5dW(Td*FZR92`iA#=-nJH010<b?QzeM{dC;Uk}U}??n8YiW_-rmoNQJ=63kaK=_&^$RXR`khUEi#qqdOlZ@N->9~0y0S}Ml;oILl>h=EjM{nK4g(D3(USCQ&phWZm6{W;tAH@NyeHIBFU~g>+2OBH+dN0QM)hm#bxEJjQ3URzT2UYa$(VLcG`C@mh@>z(0H7i66k-dL6s&bOi$hQN^`BrGIxC1KUf5rbd8}zc^f7={mar-lpdFQ>KbF{*HfyHew#rCtkXYS(`$$P%LJ>{(bPLZS5_63L=-wU+-Yj1#9iCw^K*QLEzm?ueRA@(zT?C5DHzLw-=EjUk5cK>PFw<En_hbcD3nq0W8hv$zr8^irTrb)SaJ+GXb;2B|EKSmYZc|>)`ncBk;ud`o7-76w<fEL&?)2!PrVXJkUp6R>e!=C)3sI%zy0&&08;xVJ)KV^hA|2KJvW(T<c1nmx>p7`e)fQEl%1~^2q|J?t>>~FCIRu}*iUhQ80t0RVwm8KBS$v9M)h_d8p?BBK#8+@b=aI`WL@9XE~i47}#k(;oam@812+ku^%{IGiQJZxC%j_3_O$c)^8`jl-rSrUiyWpTJz8iy;D@p$`C4*u)c@AS(5$M4+6#bZq(2f%xPkB}e0yFZFj;}E+&1e;d*!fp0+`gdgwM_clD?QAr{pR-<=GTsFXTxVge?-E1?Zbb6#NEFkbugy<KOKGlPzm?~U`2Gjk|FOFKI}b(pEF(md{g0fh@ITvFNaP%O3z&0*Y|EMZ*oBh+7=`UUirrWH|J5D<E&G<rpVRWsJwGb{s?8VNFSSReuRXFzqz;hZOBS_3Z9lzW-<$P=9hOfv*VOu$J<jR>X#FntzlQCCh5ET}VwG!@x^oE}!>F@{aSL_k9+e)51f86Lx?4o<C$_}K>E>iR_4&71xLD-bp5>Wf-eJ-;Pcz4UG&W8eDSLpX$Q{7E1Bm$#>aqhYP}BkY>hT{W=Ko&On*rn<!2ZlX-vcuTmUZEO5YPXtn2)BM7<5#m;zX64{cXvMM`=P761Q!_Ccg!6n?9a&fT<8C)?0c{Q_L7^2OrnT<V(0AK5zy31A%B_{tNcuY|%cPFN(qCvKZVyn2rDbUGMh)Q{w;f@fMtGJs|FWcQ#gv8$iuvc@)1VBaQs{$e@i_L-BwoF+Ow3L`)htM#%I@<ArYUSuh_PR`_5$v7fLb4EafWQJtHty%kFKzqKNd^g#;ozXdL~7U=i-OB(*SDg1ArYb<VmvL6shdSE;0gI)8<?(^P%vgdIN`Id*JcryRHJ)}<{@As-Zz$}%Tz4Q^7^;ku3#A*1K_W+U>%XmLkvH4j`CHq2_YWYv${s5hw0Nf|c?5EMsgwMCq%ZKyq`Hz_kGi>bab{B`|14HIm3jZZeXD*3robd|(iOM{acGgkZccR=$W&U@#S&5tDW4rV(TW#=vcKv^~58%+bZv0QNHo%(kBjG=Z_x}zNdqC73U?TpXG*H$6#SXCUGl0y0kPiRc1HkO_{jc>FBxc?D=l;KKD?QO%5Q9^-c_cYv@5ix<bR5i!Ls?=Jk|Q=Fe4P(gE}RYb>EkecoC790S!1%34Q7n9hx@d#SnfU@fqrh-vuP<ZBi5lhJ{WCTQ8<ye8)u96;X-jNE(-p$o)Z6mi_hP^i>oIN3I5L<k~-jIOO?3)-C9+Ex}r>!Wb8-sp6z0HXYi&q!XH_?%p2>M`@nzAGBGo-CoB*t^fQGNJ5-b2;2omOKJOJd#Qaz0qpmQW{D0p6X@S1t{_l&#{}8dCo#TIvP^`aezLY)0bqsMH$9H^4_A5GIpQpmVvj0Qv`{6vln&XRC_}AzF@#n3jDDi(P@z1`#;9p@sTj4*$$5!S8nS14SaDs>#WPFfLKf`;0<~eD5VtD3+=g)u4c-J%`OV8I8|JH4dv3aJ2tYI!xcMV8?fa`<0^GwN#4p4iC)IFzc`&}Ko-xoEDNqM$+1agh!*j3Sc(e>SzxoCHQ3LP*})&RDO9e@K6tjzy2{{g!Ek9kG(00fPB0YTgY;Pkxg0k)CsuQLVKk}MkyOw_gi&jDe~e}0T$|5RNrP9DfYM`b3C6eo(gfXajjk;{+Ux*8E{y%FH&f%RUlSiNXAHqbN3&m9q~7a=yl7dhM4qhenWnv=rOp0fj|3wPsONenJk#N%>#9L4_yPs#s(i!VR8hZ~)3IDe!b=MOgsIeoB3`T|W=Xs?sI{2NPhP(ks0eo727<98u-Z!{8jZbQoM2xP_X5H`OeD-m_%^YgvngXH%gt}GCDL=RWx<4ARZ*ex#de>P<QUwcvT@9jJo;l%&W`E2V=h3s}S#a?$a^7)B<Phy`~k7rq+*l=;v%R+pAk0NTWFHZRORyuWkE0N2OQ~ZW_HU3xX|NL*A<9y~nbE%!+KFgOr_OTU=OWSWN`vk?`r{zC`bp-wF+G*N4Aot@NRki)N@$UP7fJMl5y_&Z=C+s`XOzaw9o>>Q|yNyL2lzf^LZ!Q1q>#+aNwSUh0^BhChy+97+YZw>QKJ$BlMt9zU?R>Xx{ChZz#75r#tMI>dlAHky;Thn`12z0_6|;a{_#g8!0-Sjc@Ok(<JSXk|ZC3bStMYHq1OKZQph@Asli25-Umaxsj}-4mTYfB>v-aRXawIC^LQ%Lk2st~pAT4485<^xaIdlzD!&W0VdLv5q_@gQ&0F4PDIF=cO&Vt=IU9=Y$i2G~RNw`^?EN%dQ`%j-fh5!4wd9n?cjx`D$aE|40J<gC0U^&hHKy$UY3(Ro=-vDL1&c1zpVTO=~Lb*4bXNX&g^)~YLj}Z5KQ|t)o0REWk3*2M8JIr6iG5ub90VV@p#8RiB<ntR*jBiY0B81tG@h~Iq&4sYNSNRvB=m56o><4hJkL`QHB1`gP*_PQz5xG0bxrhZ8Dm4I-1lAK?I{Yt{{AcR0pXzO^<o@IyAD;DLUx4cctS>TrWxinFB3o@AkfjMMgCm}a*%z@3vxoi!?o;Mj#BK8|MQ<a=1W5}2soru9lJ6aG{f~E#t(asc>WMwgEpom=%mDm}{mMU<jJ)=d$j|D&y9?>Expt$lLF@pQdjL5P$U6YScn8Rofe4+f<zM*!ya$+cfQEl%4wz>EHu1h6Yc2ol%w+z5((C&CN32~a_&-ri>{q7A9bly?#C|*u@?2oXE_n|;H5x5^A0#~rjVY0+P27fhvhxke;S}qK;b2-M4yJ_TSWYxf<m|+$!o4_G8jEWOQgN#;4G)^L@WJUyeEYB8^veHNA3ngHGe>ddcrz}wH<1pg6LRh_No%b}51eYM#EJSc96wM@Qi5aE#W=<{L)o?qzKet$<GbK2;$t0Q{)?pl?`&<3ey_d&qc>iFkJAvr|6X^p`5q?3J&P$~J<WvO-@iZ+#qZbX05u+1q;M~6z7_FlMgM+nG&+EDcOn)LJug-|_kii=dG?QVfF}P((v^MD7r0QF^IK%2#0>Pa^nGSO(~n{W65TtSJd^XI#sHe$m%kIbsJFdgGwqFqO<ys^98vDt8%Mi6<^HjC)6B7Otf`n?>}j6Ck>a>#-uGAa;lp3UdiA<){~Ko7j>f8S!{9%8DA{|y|2-7py#H^?n+pGLB6z|8E&pS6dVrnz29V^R+23TN@NX$~zy@<*jS(=p5Aa+U|G$asd<XMik%ptC$-?(<%Ol&Lv6FZw?$dV?`_X92-ifx{T{xW0cR+XJXg1#hj>ZwP_s7IN@Ez#LCHC|8;$p>qVm}498ZvOVB?s>uEx?x-8}aRb{JK~Ezj^Wyx6dBK)eh!g>VS)6|IZWq=Q$tHT!qetQk<wOMMrHhj*?B+6=uJk^gui5g!bAJu}e${{X9#Dc$T1}zC`R8+!G!k?0=tEo)`T4It>;#8Dok2SmIvT`~_yjHjBB2|9GJTEClbY3s@3_%_rtbl8IxH%Ogn<cHKr}`{Ng@w|}i={!bCs6-f&JYTaLDKUrm8^!ivy|9_FxC!G7s^pU<omcsw0nfkkCOj8ScoZ0(P>Vlm6w&>oc?5yYkz7syx&RB5Hc>=Lpl)lD$NjTTn+bk3JIgUDg|0i)DM8rfq3uvt$6HT)jjkV*4BXG(vvHve*s^mX>8t(!eM1FweU*!MBD*TV>iT~#W|Lp&7v?5(#DeHf0`TlobE&p#m4}>!R*)cdy>>n#nMO$HlxC79Vy+_>tWxLO}fLoLs;H@NWneqnsQD&a>!BOI#`RACwGmm2a(s*1UpZ^-!{rjy2cyz1;kB^s%J7C}b$1i&2|Cb*>68#0&I+}2q*(d-1;*kbiXsg9J;=i-8oa}z7u;+)#uV-oFp1w-@xH4Dh0QLzwh<)}CPLiL%(%D#{5$-MMAmKVdOk@DY5dVE&c>%`6|I#so5k1Ecae{qfUWhrdP3$i;6VG_@v4x2F`6=u@NfPldxF;sl74|j!3*Vpj;ZVG<xff`qnfYf)S}cDqd5P2=LcApV2|DbnvjU=C!2d08ZJr=+nS+p6iUFqS^u_Qky=SE+&i2{3w5i*DoF5`~Gp571xc=F$`|0xk?(ap9B=;-o-3zxgz}gAJ#s05QVn1x!P;8qqRPY}<m2UvNi6EUG0DqT0$_%hR|Ni#Rk;weNzm4qu;~L<4;(uBX{5KN+?d2(C`;*X0{I`<rZ%EroB7ObF)a_^{{+m;ya7gHYoj4+V0Jixgc{_1De-}E*=RaE#hf7sSxY3Y_y9e{|@JJEf?I_0w6#IX9<zVmgza+o-_#y6|Xa1YTyusz84P^g`{Z{h(TdIWJZzuMTR8m}Do=cL0R^q>{B3I~wqtyjCL4HAJLpk{hl`>vvQH0|L@*hq%RiH!c7|+4J$Ux%XRM>wL;@{7CFm}x~BIZqn>}Mg{pFonhfY~?G=z#=gpCoB9i)25Y*iBnvt>HgK_yNo{@AQ;0gL*48W`SB8kokP6JLJ!^sCHjvf4@$@yyRc@3G1?-Pm=2=KVLJ|LT{emu;%EqwHv?lj<}O2_TyU^e=qKN-pAT+xtLjIrhZEI!Z{Y|S{IpQWq{@5cn+Wo|KtPk9T0H~X#4<*`Q;rjm49&u%=s0`KhFU=$~(ZE1N2vS0Py^endJX@9sW80*O0Z3_)o>r65^k1{~?9{x|Aq1aIBxS4Ry&8g8%09?K=GLLOa?2<7D$s702RqNenKQC*e{x`Tfl~cpz?pm*U-a;=fbn|LflQ|KuUX|A)!;HxmDixY$PVf9nC9Jy?yC<o9<H|3|CX?&k>h50z$$CkyL@j=ExD<4+Uk=ZJUq8HDbjkI#zup;E{R;=i?m?SG)K|F6ID9NGWpvE1cN?4D<Y1WyxU-jtXp)=38>X!L-&^a<#DtOJr3SqQdMNYa;BN%p;^q_Gb0lI(LXAbz1XM;N2<&!4NYe2T(<s!|6`TO#`bRrb~W-TX{c_|G8QpH2K{6Z82R{_T<LYcF~P_4ebe+x*P!%I3Tv?-ApkJ^QJXr7o~C>2)WO*g@uG2m6WRM6Qp0XznrK+`><ZeZFbTyUV-&Z=7UpfR*D%A#~bsgijwP_z#~tMDVZn00oR20Dt1&e=P3;?W=bOkaK`M11NR?D|<l29dKn2DDyv~pFaPgt2~JRJ%axu%zs`ynzHtyfqeh^l<lZVl<c$AlQg9f{~6K3|36-^7bi&0l*Zv~Sv<~_C*UI4{_FMWdi*~k{vUUi<8z+>eTx16)u->^&gnK>IaZHL<ojP>+kdE<n5_`}w^!v0_L+MjB`jIQelAI#$O*80XPqH(1|%1h=ea}D{&zN(k$+HzmeMSW|NY@i_W$*lng8doYz*^nh{T1a<j<Q4VLc%H0S^<6UywjLAaRj7l8OHmvi;0{y0<FCFxzi0E8!P#UQg5tnR!n+2e4P^9p*a0ekGP?Uq02#R?EJ!14OO$GyD6Mx?hTyoD)#lXPckDoY-GxFC@=T_6_iT4vnoG>kNa|^Uvh1EY61+9ykWmv_e>I43N5j-j{d!@IC>am$C8k7Jai85xZcTn4_6p9{R~Y|D@Pg<A2_#);0d0XvO@GM2NzFIQaq0e+co<JwO2z`)?ukH<Rt(Jf@%EKhQ<i0E5TAD(`^0XnTNk?|}0z5WfF6qknh)>ofP@NHP2VZ2RMg|2=3->%xCx1R50n+t}{s?Lh~zf2uehrzqY(L&EG|tWLxATFL*twnDspg!wPS;}c~H|GnG)Up{#Uw@$W_?XM$l50H@UCSI9+j_Hq)?LWf(yJf_ESr%G}Vb%xCe@9IrPEyQ&rlnfM44gO6<`3#b{2+1w_2eVem(bsljeXk!;Am-r*IojQ>G!hSnfW(B5;317?7xY^zhqzJ1W6CDFOWpOe=_R;FVX>CmV*5>(gUeWq(6|%>?^gxIKKbOJprn)51{P(ROkLv6{gdc*mhxG+e46~!#?Nyas>PKg8c%8f0g|ll7Ly3db>clH%{#DdIoiX*dzMIe~3N4qs~0Q$P14!{Qfs0rXTw0zX;BGu8(twypxE3UfctI#%p~&+JAa?!G8}q&=q9=!>4Qc51qnwz&8;%VIa0p?7w-OmVf`TvIl@~0fvs}JAkhuRNMl538Bt>2k1F@2gpH6SN;bx|EoPwo4ylmMJoTXXe9m{(srOeEt;6$j@smHg8wEF|8pH+2io)Z;6w>?zaM8Sl5nAt{Qd)}xO^ZDH=1&Aw>2Mc9WEjMOYrDeG1-6O{}S>4&rjq3A@P3@mk+bgPw{&r#qngTPY|=ms|#_gntXrao~5l!+I@}(j#cNAPhX1DitWF2jM(S=!7-iu!NDq#3*^6x<AfISANEEBz?uBb*IopS$S+#%Gzhz0M<bCKPxO?Mz&gOwMAQWKxf=`N{9q#a|0!($Nm5A%q>&CtSxlcVu_X2_h*t}W?TLNn-Q65}-6i|%1H^J)pr`BsN>p}!rU<()>;KF+vz{nsgRC_5#FQoa><gb?VZU$%=l)0+DE>d^1>zUlie21#yYi1-dA9FpGtXcB=TGL=_xJL5-u_jOwa|%HhFC`YhfEz#F#zWPhKV{L^Y2f7zb^m02QWzK0bu^aCXfyo_Zq^<2M8tp!@BU#I{?`KpWaWu{ui>`UGRUXU_VK`;QwH5jE4WZ<VZB6$UGqPe~|b;nzt7nMX@-k@PDo{85bz#zgV3r<VHgd?i?ci+lmDL5091LJ+}W>zRUhUz|9jaWcRCawy7Lv$?s<!aFTdEUQ;MIXFp%{_t_sf&g|3oPm!N+o??ZIN9u6(cr&hW4)LhWAzWyq*uRNmh;m{3JL^htkbDQW{~TAo`l4e09S35MtD)rI!x#y|=QlzuF(2b@L}Dx?-a{K7un&+b@&Kd{NDm}0HYZlOCTLFV%YMNa-V?&Y{PT?uPnio8>@Ttwc>+-{@K%KP{)%2dYei%{z;OZh1?jS1yh5_ilD}N#-$BF%JVOvX$Ey2$?Bl=v>tyREBD^crdc_*ipQZc%%{WU#terSg>;VXy&UJtx2%a<qffH5!Nyc&RufK+Wz6BUMp84;CFopjx@&Uqh_kbunKsI*eU%CHPl^jL3U!VVG&H<1P;5pt#;-2e(Tm#_z|IxhN=qQdwC-Hx}JV}TDWL&M!#Px<Ovj2I+Ke69YhDXHy<5QK={y(Mu_v??}#_dkxzlH2Lv44*FZz{tnV)n#=LLu!n1*8k|g)TT=!}<OqoF*TE?LOBFuC_PgddERr>pX}n^fAW(B8SjWCVB@t=<i_R_+ej!KmC6|{J%i<zb~-DX#n=R8W8tJB+NOpZ$#`GO8z~JL@ba*K0(qV(gBN@e=}k4ITw()i0gu;B1gDi=@;bQLGBxl^_0E<>jTaOBra6_enmKE=)?6qDQX-bZN81@1<LldCH8HRPplWMknESNlv1?9QRst0t^pGN`F;)}2FRtS?o1Hp&I}t*{><Tzd=t>d$G4mP=N`aS6Gq8B0A&9|xc`qNkY|9$DSJR!52*Z)m;8sQ{EvSPVG94D3jbmUfP<_7tmpk-{q*Pm1pmY~+y6G^KbQE=k3~~9^B*mG02&njTZnz`0XSB;4=0HKQ{{<*|BE$gxKx*c%e5JT|62!hiT^^p#kv2J)p+kzHJ+TU#y2<Go|^yTd!cv8|G(5$gNuhq$Zx;UQb~S58TkWcI9<oJ{Q@DU$cD3DaE7=(cc==N$nIZb_DO$SCm({JSGk9XzJ8Wsf1yV>mZ&8?Qdfl53Wa~R{}cxq3jcoq+5b`G?;B!2F(2z@h&VUmpPn%+^tE_TV`A5s{C<V~g;GRs0Otyb_gGKaI~1?Z3W(l8*&~$5&jpIzU##Q`NivtRZMQ}GQprE}|8Z|mhSJ}oVV`V#@d|sCta3!@YA2Mfazg1!M--71D*W@4`DY)Ym_FW0K7gig#=@9v&uR?&=pTM&@yFVaa{7L^{$JVuV}LaiW&ST@y2AeyY5xN!4nhF$0v;#pf4m1QXhJ{DEnt=Z@UgFm901<|4p#Pn@+~ml0kDSb|J1(v^MC$J=b|cw`A-n`U-$u}0}kcHqB(oF;J-10+24tl^c^^ow+HRae+ltVcK;mv{DS{<Vm|}do3qLHFTexx``>P_!n+-ng8z@s9>6zu+VSllf7L7hzyIPr+&|rhYsYGF<tR%XE|CtnB=kUq5D^!UFwg8GoTs>+WBh9r>t8?KgqvjdZ;}qV-qA#VzCq*>Pg4wWn)?PR_Gk7_H<sdXWj5ls%bPW?JSXk{a;G=2ckU=83Hwj1lg($*u<s#le=^zlWU~KK2bhZffc=~!6!}3j#qT#$bbvX1-$LdKxkrfN_*9PN`AOVoc-si}b9`;cZ|8cxwDTFh+|#Sg@e7|{hkcf^)s84%<Aidd2OLq%IzWj9ipVG6&r3)+KcJfjVBs0mpIQ8o=As);=-o@xt^aMB%=Q1_(*94C{0C1SjIDeFkafWLfr5YT0aE!7Rrn7j{zDc1MgHIEIk5+vdjJA#fYl~F@$ZWAgh(99kJs>jQ1HJ`%>J-6vfod-ppE2k&K_a^IsRw+e~$hB1L?R#!uJ1qQ#RTEeB3=!ghw3}_~29>J~&fL_P?I&|Iw%N|A((1<E`^YakHZVH;y;pCdpOO0awU|vrnMvflCLgNvd$=a6Q@lCfqu42)9nP;{Lf~xJQ5f)~Um|K|a8hBMmsm{zD_h{uDz<A)ld%`OiUIRG|31zf8Xeqdveg$Ntzy{3jCo3B-G%y8+_dwR(Va0?Ew(0)_v@Tq98G1Kcz0PW-z|9T4wfF7!eY_X&_l_AN!Nfa?RPOJuDulYIUh;$P(MxPC`Ao+XFa*VOZt+iPO|l2wxRa$>(ik#eC2oW--0^@0$o7g(y+Iw5$jejY&h06(#wnZFA8>OaL!Mh9p6Zv4-%H4r;MLKObF{vS%RmG6L(1d$HdIzjdTZSBH8vmZur0P`QB)BuCH4~Tn!Sa|=J5iqe&&-@?cd7pgAe=F$$p8szZd4Sz$Asx_~vloYR_oBTx0i9*ZI9rpBbHw|l#vEL1$i<b0Y+NV1f2)PrC)<C#f_(o2c>h$b;Q#UIDrNtF@A<#K|K>xybEzG7JBjHImPVlmu2Bqsm2|+B!v~1zTG9!mA3B=J*0<u`xpusB`4k@AI7f0`Jm0=@3ir>qi#))kwgV!M$T<SeBb=g`;Vkjrejp$5Q9)$?4e%24{~FnUhkn>QXC&Ew0}{>y8VFsGMDak<0wbg>Aisa13DU^^r?L+~A9IaxKYg8R1aa;rg8%&<+&f6@FJSJuKhRv(2D}vhmspa2Pqv@f&t~@7wv!YP>jg?aUw&*)wp>cl3VXqSslv6&ekDm2`2ZCpmFt{|bt%>Kam~6h8p&Vgghk`@`_1^@p{S$%%;o>+92D<m>Dv3tGS9&P0aH2mC-Z;XW(-64^q~lwDrKv<13pObuiAgk{|77lbN)X>;XlavdC>!?_5t%AkX6ROgf9GVVgEllO0X~Te;fz!$IO2Y@t?hi;(-_(E{Ma4id3Ag$;PFoLR@Pr$IW9kxZ6>SdmVLn&{>cBCl26#dnJ8dLHv{7f3l8rKn>m{T_E{y!MFePRj>U2!!MuU(dBmBBmVDnHsjXuM)C!k$hWT-!v6hr`Wn{=xli!z%O~;v&GY#1?lpY;@D@IK=Qcim`!+the*^E|x`ekbcH$=a20TA-mi&NI<oBOxD8c!bGLb{vAI<h(@^8re+xJDx>=8)z7_Eo_l8OCfPhy{*DWn5ZDfUlYWI{sre}Rk#WUkOy_6P|f_C2NT=lTHG2)Qo6^#Se^$ndce{4@J(>+?wpND7I0A<JccuaNm)F4-6Sue2whU*+FP!?_T$@hmm#NY*>!05Q)}OH#L<bO6b|g?3$LW~TnAJHr0g$v6Kka(}#!y{8$-1<s?%_aBLfStK(@kPJt}40_HWUBJG85(fzWc@AK_ngb*qFjmX|)-f+3kok91<^ag|-$eF*xsg8qg9QJss7{U+JHHMWB}yNlV85{cTnji%{y=+4GEP*c<7{0XF1M892F2(1Panj?^M~=?l@5Gx{UkoP)`<_U9>@Eaj^LehO?dZo1Kv4NgNGeec<WdR9v&~jlk@dYo&W!*UwusW{}|bSVw_@t+b4+kV|C;M)Z<1w+kEl~NH5$a_8(k0jz`ze;iG$3#on+l-g|&AKX{0*KYT|#KYMf^AKtqz_7?L_fh$KFDHf>41<n^7ti-XJJnX0V-;sXzFVgS9;8nK&eXwu#aHP1868xul8c6P$e`cTTe44_4+Cr6mBZ?J_gzeYu5lklEUkK+175ksz%|h&ZTaw>zO@6;M*?zYF%s$(HI}|bRf_ce*(Q?)HGyi1&g}+a>el>GHMo10ue}MR|RpbDFPExx;vR|isy^Q`$S0BJ;>9S|DC+M$Kc<#Y&=8PV5|9;~}VcYDHh@3qNk;H!_@gLcZ|A7ks{q^}*e1M+#5BL-KZ!MOyKWzV*|JK}i!Tu4_3CGLQaI!iZ=NpT0jePLCoh`&Ev3}z;p4_{H&mLaK=kMIW=Wk!brw=aSqnju3(bc1Pbf$s$ug1OBV!YLwi}z1f;oHA`+&lljeu8(-AHi+n{Tjvn*N@gpA$@a$?Eg)Q2e>}S{J(v%gKYeHeEb&ux8J)jxc}uRkMXO|Kf*6R`w+kQ<O6*1!8>?D{=mC8FW~OE<D@?hid@3w)*7^v{ZEMA3J0^%c!7QoqhDeB|2ksk4o9l{NThm<LJH}CRMG)b42ge52Z*N;*?42}^^HZpfXe@V57{roIf5jSCo~gz!c@`$8SM8n`=kT1NOF9wh2$@@RoJ&9=A{l`eZcv?0!0dy_`giG{p%$E%y#_-7c^{?jMr^on@{Ys55Pj7v&iqcAYri`Cfn%W&gI_QpV|B$===NRpT*rJzNy?(@6XDKqY**uZzqYKH5yT~=;N8Aqz>SD0EPdp6BPa@^hcn=KlcDI|G^^v$Ns;Z0}61EJAgI(zuuky%A_b9DoQ|`kVIJnAluLS;Ak=PpNdm8Ik?bNL_Cs@f4Y@o?i2X%&PC$>7QX)A0e<o1A$~>ji;v#ISMT1$C-=_b@ukCfNHM|fLj}0el!ga~3-F)6?S22>KYsH89*|#g{cttK7uC2<a=Eoi2!DR<$N~EA*O6b)jM{=UlxFP5S|1Ns8xF;Y0k6W^Xc#uE@J3xF#rKDrNEbZ8*H7NZr;qO8@trH=Gj!l4=?~T|oCi$U5d;TQUH)G~%$#9JBmUDoMv~oU9l*W-Jw2sAz}%-SG$I`!<AOx8|A`_$NZhk{C_*|Qh4^Pl75tNUnIny3fF*4IEs;%eK%O7*Pd-5YG8>@>SU4tNeZchq{y2}m#{8GAl>Aq&k-~mI^WL!0MF{g<Cv3iCpLGDo0|!X><Jt|<FW5ZGO4u9SJz+nEy*WSm*q{6U1H<(8qHAh@wuT6pHX7UK5c_isuygik>>xc5HM=|iya$ZgX9=FrPs#mja{wy;fsW53KqvlRZu}Jf+4hSbpak;$<I$9}PxJwC+<%z9ccLO41#uC`+!H4516dgik<1Z$>-`p?t}Gwz2kY?LFFwGpKY0gVzk3Ux-Z_hR&$i$W+5fAx$$0Bf4*ug8Z$Bmf_a5Fl(}F99%5kZ=lwyqvTx>4G8Sd}nKA+lRTsl~R;-uYhA7>?Q44V(_3&S^G7w@7g6C5qkQd^DReEA9a0*~?8yAMbQT*Cc}?YMpNpr{`*|MsRMML+cDmw*+_|EwWo|3@f7I)I)jtP4m7q<W54gna>pe@{c91NOTaA$Goz=og6NT7jp`4W<hIIbUdoH1Yw`mRJa3Um%_00geGg?axn2wy%_2U!^Xv)K=sNIUmR|LD?z?nbTk6Bw~8@`|CF{^9ujU*O>V#B_~k5PD+i)2|6QdnIk-0^k+|aAI-D8H~+7~_djt9wjLfm_Wip#8X|nA0ix#;`y{)F|DAKR_CJ#NXa7HxcL7c2T>x(&bmAKbn@Idm>`Nc_(ewa^iu`{U{w=%Pf06(5QTzXhd(Qil@6Y=H>N0kV`<;CAvoUiQQX@8D-Wb!TGv{D|g9pm-yDvY)uReZ=FW$b6_b<2O?vZlhKLs}$)9}xq-szqHuiwLciv2IO6yscd5zdmFsLnxaX*ya8Q*n?ab-O?2*bK!~^EWZkbP&cG4TR(9H())YA4~@K>Gn<)g*ie8eDewW0eA8E)<yCI+Ht<663NWJ$q3O8IqF4Vncb_1ojHV<=NMo(lHG<Q#cc$VSV$KnxsBAz4XX8lcsB#Y&F5YrL&VOL{Byl9N$C|#Bl|DK9O;XtWRM?_Nje~lbU-%60zx<^=wmH%16&u(CdnoK3w&j6pjhFbbN$Revwc7(<`?!}$^VNS;ToMf!CEN?)=M3*XMwHQwWIs*(0pgyGqES=uTl8^nn^}UkG6lJHTnJ%429j_!R)(|%pv~S{ww@*{J(9AmjAHHZy<aUOMgU6>W6K_e>lM%PW*>a4DdAmb^Cw0{#T*!-;y7XhU{2Wr|v@O{s`pl2}Z%b5bWCE{pT^4pNZdm`X0V|?+!k?c?NHFQaoRuMUswx`1E$K{QupT@8Vu(9oc-g^?5j2mX4<UB$TJ@L2=?v#D;9dk})H(&~XIZ?T2HY?J&%x_nK`z1k)`CV}i*bp`Q!}>FcKE+DiQDvybrAhmY{&ts8jf>PgX8khVJvc1FX+?_(qd0W0iYLG0|og8eiPviojBiTU9|l8OIh@&&|G$qgpaGl|$2y@K;clPG#%zRU@7JivJX_5sosn-ceCLNdI}g=CT+kiOVb_yU<rtc0YiBJ_bR`2Z9X_{zATc)2~wSK15TU$yPb`vDTp`Sa(MBxP%edC?ar`vY03xK2p?*D7&A!$xOums4+_hKm!7DqDZ%_5VlD->G}BS>)B@ZUgrHH%~Q0)EsI5cPaKiTJilg{7;qqZ<{g*;Zp`;+hh{K{~L&y%+Ef8|4?22l|8^4EqjjtRsOmEw;?A6Rm6UNOc+w5wjeHKEyCA$eJ|!3tBUc}`*-p2-3xel<{)k~=aXdP@1ET3z5XZce?2ZX7Nesg3k|vZQIfP1DLaA@xp^i0=h$NPBtvmyc-i>T;%1sEOUI7FV&@U`enT<dWT2j2n&4oGqb>FL=E-~b?9m-OzI{>D5;FIO!_IJ+_+5-3C9uNgMa0b<gf!wlbw0~5B+V!GS=?A3a6BMmfkgHP=$SNs6cUL0*m<Lc#E>3f{y7%lJV1&ku}}VgI*XSnac?H_0qKh^q%YuQNxH#OK6#E%*(bnt0?rc({zX23_+Md1Tsx5McU1V7_P&~Z0i_3kdw)tqAE3Q5BjAYgRs4Nt@$<|+=LZ@$%6_2*V@-wssi_H0nS@d2AO6hjUo!9a`M<*~E**{zHa+_O?oNgXo+0_);VONA9mIbW*8rJ)^8F*Y4?wa1;l%&8se>f{#C_!C0fPU?Nqsf^i~OI%a~l462k6uIZzkJclfE1IvD=Ukz6nvAmt%{cCpONs{C>>Yo1?9^6i@D)$D<2vxYbsIn+Nmp_fKv-CI9z69-Jiqzo8hd#i^)B-Gk()AZ*{f8bK?)5b9=!t#d6AFw<1r8s9igU)E1C67RXdak%hPbl<18tPtOP^ax+Re-9tuzk)lb4k9}yg7_aQeiy?@mfO65c;Y{0KKTIihalONWZp3PoOQr35eu+zULZ-4WH;h{-YCSlGXDmMpU?4tp<@4yM2|rFA``)X2KfM)UhE5)$ryliKq|!qX^SnSERs5abpf-_aX{`;8<8I@SZ<3FV!C{lJ?Q}Q0ay<Zzk+-6^-JmVGVb?X;i&Wt$T<M68FDNj_~(3~QV(p}C?(n39y9Fq_iu5GI^yQ1Kj#DgQujVOZi+f<*Bc0`@4srI0k+RJ5d25YVY@$?;{K7M1{g7&djNPBfaE`{3;)vYzd`<gUu@$%Aol?9Eg)yP2XM2p2b}kRFEa!tzOFz28@Ozq;J-097NvaeCn^BjH!Z{ZCG)X%(cB-z9N*~p=+;@hbN(>y9IM3b!^QafPkQ(N|Lr$V@bGjCE;bdBKazy(J>iJ<UxT&Yp4h(91F>u7iaX;wd?q8z-Cn$t{|pn2@cV6;YDBS=0o?3|!P#(t#xGgty9i%@@KEyq;41E(X+uujcGw#Z*6_c=`gz1mAAq!ZgOEP|O(eMvCjMCm3>Ew*yRi-!E^>mJcz}F@_<17{=Q@h?z-ao|013o@iihkQ;C=z-Ka(VbbU>zteHjCAPC&?FIY-R#0LKN)e=hM~K<pPTw;`_Wbl4|Z?SN{I?RgHUEBnhFG{UoiZ2v{RV4V}{H^_Qn(<YLQF60NeAkxi7?AO)1qqWf=Bkp|uGrvD@&*6ycPh`JemtB8q&yTC4A%dnGAaWM5&)jp(Z<gZs&m6AV1;RUkLMF>QAYr=kKk>hfpTs}c0eA<<R%gBi@I0~q95#^cUuW4n{{eoks7~2|y3D;~`@^wk>soB`U4TWiCnMB%?hj&)Z-Tymt&{j~!`)+5xOcP^fB)ItUitrb;{Va<X7c|F=zZdl5aAF16-%&S_EcnquR?bCN~CRFDsGKOEt!D8+2(rhqP`F7ByX3Im}W5;HY56B<bc;io#Kmk@8R=Dw}t(`NBrj{Y$y9K`R6ELnI-W*tv}LSCI2b&i2wOg2c%FukTPHL&-sBQB{wMZgCntj{z!@gMv+c1ATbp6!SsbBi;ReSW5xEHksdJ9=zw(20VtyS0~uudbG@wv`}w|B#IUu(z8x{_AcWbkTFqQD-*zbDxq#&k-PkXpua(gEI2R!E|4yPl*tkiG&;caeN3darRkwY6ykC!Z^!?28_uOMbPvhbSv7fjn+t2g<OUD@?eAZ|ZVxIVqpeM^V_WNfHN9c693p9jmzwRBNP|p98giVt9e`Y_JcLBM)D)xYGv3p+b|F;6x^6qcq->(P$gO|Ap{_C@2kg;nkwryAjU-y}qG0qVgVJm+Sb5;g}@$g(59-L~%z4mIn-C2o$`szWi{QvQHALHF~Ex17ZSCQ|(H+Vf(E}D;N<LKXSQUogZhlyL`86key?Kh45602_dh=u(Uj#XSKE^#p$0JEWe#ru5v_6@=Qle-s*|5oJ2MZ#v(n{pQt^G`8S?DT$Q{|6$Se1K$E(g7^<hX@fl0pdPQ)CM^xI9yvR^cX4Pf^<&<Vt+KU78`3M+sgzwOW6N65&nM`@t^I*Vvg)3=Ex$SpC#Md5;-J!ORdP~w?fG>8<elKA>N7o)%N7WJ4jip@V}bt0rny<$o)cve)dwjvcH=1gig};-!Mi`2e5Gckmm@!#_QWCj#o|hXZ_6b_lfyG{TCQCpN7@y^`gI`r@Y?`TSIJ|Lj2Dp{%4Lt_zV*A`@@L+Fk(N9+2=Vxz6GH2&-MR_vj1o61eU%C8rO$}_W-_%z%g<U@CLH|>xlhz-@$*7!hcQLE+lXF$ChOa;W1;PxQqLjVD3R@BkrB3#ohJ_ymg`y|N2eu_5c6;{U>;j`EMwom}EDiDDGb}Zzf#m_g9;_4+r!2qdsGocpuhB;U10%oMqmlPqLC?Di4R@m}2&($WJ<(jlh#T7xCe(^LTvy4DOyfgxpx-f8-$XyBNa!)9)yD>gz~%9iY(xA{KCa6B%xUkw!WojdVa7#R2K02hxfEbdM2Ay<ikF7K}nBv7fuh0J)0|k>_oKd~Z|aFEK$j>49AOIE!>a7U_a)k{n_`mwf$Pt^v|Bha_*QC9!XbVzT>{D{WCl_Wi&bdnv>{i{PI=uOOXJwnFk>#Qnp5QU??*vnPMRUXOiY`<2*V)d9?XgCcbsoRLj>!_!4SAL!_4BkEQ^GyUh~`$k@TSM2!jsn<`n{YzX75J<j%*o@H#W%g%`6r#@m@XSx>RCxz%EBF5J><`}o=Nlm7`pX>v<B5N=`$4=1bPVzD^a{4v_s;*MK3(~rui-yFd=obKcwqKKXV{n;{$S>uOorlSTRCpERZ#3-iu>*5_?Pdp|7V+Uxv3Bb()J*F%PK6GH3cqqR!G?yiq5(M9IePeedccDM6ALtzo`hBZ>xD9Rklnw5jo36P9reOY6!*|4}||xcf5bKlj4z6^gJPE3NrVG!g_drt^GGw_@CP!8Ln?2W8MIgfh2>FNje~tVuCF9A;|I=N-_-D9>bCCIYLP8qLIj1I10I5#J-my3cQR^u*8`7H$frm0Fqo{KbN@A^ERWWBIE<)dYcO!kncm{Yk^`vYhu?1RU~!m?1j{?b3ns-2h^_RoS(grO6H&R1IGu2O8;;PNij*qO8U7~j;LAfDC+&X>~jvFL6JJ89$2@Qgnll?+a9wWyYJxUox8&y{Nm@F;GdB18+qjek?-f*YdzgI;NHs_c4Ye}k4Erx-uE*~@E<x|-TkHIKX{6k{~(2bf8GHm?tjP~zyU6O1pk2={$J4Je~pEV|JD9~4gb$e`(K&71J&ufuz%YotoL>kzCZJy7`62WF}K!jGOjk|<9bU0ZZzlO-tjX0%h$cz|3CibL%b*W&qs4!JYqsNz{_=p;D6mRZ(KUUyS%H>QIjY7lG1~h3ZG<~r;~Um_DzE4*@zfr9mOL)E~7BtZkUKk%2RiX*@8zGj*=Y4wIfxeQvzWzOfE#?D8Sd0_@D9`GUoP^;@TgXqyw^v{cQKa$f74pw#N`Do<ou2IUKpn{lbx?14bi%u>tZ)2NaSHC|p81z}px_J|@Bk$R){RVO>CaAeZ%k5BmV-^fhx7EM@ksh+XpeNov>Hqmj68T<3`94UWXWBWmg6nzeSsHO2kpACxQnm#&c9vy`tSA7K^e2iaHPpLY~&Gwb!re1L{~Me3E_A?_`RcC*1Go9;KdcsKXYb%Cev_2u6W&sFoz^4{*2OtvwEFU9?V(?$#SLx_Ez`_u40ZFm>{x$pN)IsZ>Ofcf_){@MQX9l)TmeZ(Gc4gU^W{zd$+!@utRFR}m2&kbb>5fpFlL~3*Z{K@}U?f;4;3w{uDsoU1#d{r{eA4tcQh78;}T!4T4^8Qo&|0j6&Oarbp7oxQw0ckseDE^;E{=XIcycVFnp&U0)wTZpHte4m?$%|TtWdB8oUE?b9l&qg37P<(3gzFNlkCsifL48&{#UD+0c&Zt1pKigWgXKusz8U6&U)S_P`qJ+xZpy1jpVOE4?~m;H0~8rVI$#iT+}|W2JwTG}&N^Ty>40I#BiqlCPg1aWG_g;zgml0XLm~O32Xe_D$W!>w@iHb~-$cX%tPApd%#gp-oY=QS*)l8g`I&t?G^}-y{I6H}cMxCa7=UvE<>Vifudo&Dm+@@yO4%PI_*d!!HEX2~s9Wzuf6j^6r#N7p`jqU~D18K6BdApRg~I1qce}66IYG<KLC?fJ(P!1~=Q~Tb?(VYI-@~0lzG2Aq{taaNL)h;p{)4CU&JT|J+4rXyfcO9N{*S37Q-+HDKf%QRR>A*31PTBD4FrzwhpjBk|5$~8aR>MXP5w{h|E*;Ge`OE+`}?{I{<-#_wI>*1YrNq;eS+YBvAZjd)s=oP=6oiaAv<y%PL;;tTxBB8S104{A>#jwd%g4jTjswJ*AEq;qdW~oaS;fn_nA7@0b|JrSijN__b#5my$c<<cA`b}@V6Evp(c5|h)>u*N%mhX-ih_n+R3bEDJI_)f{RThxKBR+-S#@%Jzj?kjfF^v*aXu-uZZ8p0FtF-|KldVOzihXwrf9R&+Ct@d2i5@eSm?)Jm~-rH4f0~fZT<{iTx3hf0DvQqsaeP*!Pn8fNYk<hR7y8!2IV5f52GD5txzfm;6_(utp90{cCK|v`*UpCgPs`|2mHU+3#P)cAsQ9vA^5~Wz0X<2o(OSIaerbzrA3eh2sYH9S*ExU%*l5fI7|#5c3Dt$o{}eF<0zFdPVLKVjVE9OI?unR#-1z1;eIOKZ#qr|6~2V_rHNjW~r$2bIznEyU#lqXWDVS-w<187zn!`D(pYAKT7Z)PWE5a{ALUj`M)r+|6{Q1`JX%xA)Nmk{|17^9dK>@A1vfG1iQR~0OyyK8(^~jzu8*e0bOO%o&PP&e|!YR`g>3qw+*|4)?k_YEO8fC_y9hOzZY}6H+rKXB@D;&ccZgtFD_Ih;%0LW{_d0B`~ScF!zXzEY$NWrmEl5NE?V;TBPn7FeCd70+S9*92Wy1-ZxA#6{7%<958`Y~g@{iYv-hDqE(AG|t3{rI_0gtj#@H9I0!?`dI9*F|$f0uFI9!Qa<olnl%|%@3TH^m@@w?zGz*1x8|0QIR{m&x*KbK`*Kje`P$eGW=zQ91_==cJ;EEEUit2%(}e*wn;A|4opEQ$@X7VvX4G8Y&ibD<$}7n3irnArCs_E~&PQS57uQa^JI|MhDn|IO<;_P0kZanC+K=LO1_TZ{Z)p&vc{Y^2cV<=h`gB5l9DrdA;GhPv#NKT${epq^t1!9MvD#5)W37qW<1Ly|E1|5R^YSnU&>JW<@;<6YMOn|BEDEw7Q+K7?^mJ#0M}^y>4oFEyRm_Zx47t<wz=Hk0#yQU`>SAHe)a%p585|KU9QPyBP=58waO@IQt4pFDtk0b+mR8)Eh^gk_wZ{|giQ|6UgJ|C;?jy!+Ee?f_YB+J%44|M|{GMN$;%b7JYa9Vt-(*t*IKb0>`v{ImG4TlHr#7v?t)+1oduHYrrx2<<H98)5r!qdp6NcuN28w}1Ny9-nK*y*BbW4wd3mbrz~qc4LSCDtOGCjPZ`PnBZiK&1+ZUbX&c+6@2x0lh8>=%F;A>%GAIm*tu*bA~!BWKKT$0xd~!_!R6)>+&EN%TdigEcV{3jcr}dQcuC8@A@M)yMdJT;V!sbbU*x&=N8bGY#QXr{k`KUoAamY8@&N`Ri~Rt%!N~I<@f?D@1*`{#A;)tBau$q4rsr^Ed5%Pe$0%i{c$AQ=g+}BL7)ka?3Md9BT55*!WfrJhZi#C0|LfP-pm`m~0Jg+F$NRP-FHlCD7x-FHykI4s1-_&QNJ>ZtlrHC=u@fSF0HtQIPD<T+RV4fM>tsy8`huBP`-@7+hu~hrl9l%2>!G9rbngl4-4mdAVDz!8f4%oQzH68Fy{}*%9V2d)J?&QM1S>;$I?F!ah*^f%M*OpI?Jr{HC@I_np!EH5|IhZBEW^d@FW&$bx4@^oi7@T~B0nH>Vt>KEcK%;`|0l>vyZ?{*=l#E{yV!q@d4qiC3H}?1|Jn@M`@eVVTC7<-50l5(!p6){$SPkioIF(fROX_VPQym8IoL(EpZTv&2tjLB6gmp|KIk4?tVzJ%d~mII{{QhyJh^lT506*j?c+711Io}*k%`iTC`4>rPC8%;COX?=(inTJUG9T`^{bJe5`*>wg*aZFgX+|s*zLap8~qj`aP?BehHpmk{%Fxlc#8PH)KDa1i|dDqahmv#*}4kG16~xr3(f-i690*lUqtqtSBU%9N&1lVMK1GCu|W2`ej+cBI){6N-ay(sStsOpAcu57j>iyUe;9E;6q)YBkik7dI(@@w?otn=lMcuxKOjfw0Au3cMA-idKQrRrg6zNa0cwf)T6!K>Z6o{w&J7fBeb9&b=URa!@h_#wm-t`CLONgtvu{U#j&lX}LYVt{h5vdHPdK81eFE|WbnSn+!hflfPY}I^^!2#KwwUMKy)NKrZ3<gAH<%{piF<tiTl4_$@iVG8B<A_;=FIM~zehLU?>WX0fn@i$5&PWh7cq<ZAC2v^75-<9l)Zn%f7C3MeG>8kA}I!loIXU{0u0mPKSbd_WV|x_%QwJWB>%ksH%PJnn^pTy{7>$(|1ZdIp0N83*>R);_M$vFn&Ra^L~mY>WuCJz$;n3WZ*6J-?}cty<F^D^@jG#h?{E-vRVh0VwZR*{Zqu>cb0++k&O=PVGUV>qgu0|K!T;gR2pr3e!s+r@{PLZ1z4QNXzr<%Zj^X`Nb$GY47H_xL;KspH9IwnkMe1(s30{vC3rR1zIFdME@>oY9lf>ur9OD4@SyQmi#}m>1tC6#B8|t%T(NUg3@kBnZwG`ue3-N!jnDjv!+5c5AQTS*3&-u}$DbFL9_!lzwRpgQ$$YmWs{y^Gnl39I_KD!^%=ddn#gJb|Q=1YG-)Ck=;KR6WW#69;5rOe|QqG3w^@NlHMkw3sbfhX4tI6q*7LLXxk3;S<|s^#XWVI8p25(idVkuP8^*e~_7l=VSxOJwuj0B_khz|TVBztGQ0)D1Zwz%f7->3{>Pn0-5n`|Z%k`9l)*DMaDFPSF7stL%t-DO^_+Jx8ng=bX^8X$;br+GF8Zvz~8p)9;?|a~f5(i5&RQFaP*gxx1I=ct@SO3#0Ob!uR3--A}Xo?Du;(8)4%VBjR4NuL|=oc75@j?;P3lzhllwM9&_9=s8jkMDY$Vvi*_M1|w2q{|6y>qTKzlm3)8p|3k$7KeGP{|2+Gr;h${(7G?kEls@|QKhVz=<%0hhVr3s{$cC2ek3edaKcY7+!wS*?vnI%TptXqs$!H-~#-r#-LeKGbW^kMAgw>wY5xIIHQp4AvEG7sIsoQWUJsby<!*C=$6sJn};;Xk#_0Inve~Vw->BL9p{~vd60bND5ZjDZ8w1LJgKm>PpcXtWyA-DwhkU&TXfw=CtdxE>WYaqBw)3nmvr=2&ywJJg%4)>gU-+1r8KV!_Xs%qD+)GlhyZ+=T-aC2`ouJ4J$m7NhdnG}e<ksgTlbb#yfxmZYUj~&t%1G;xakB+U;vtt|d@7@8U2KK~^u|u(H_GGx*+fmQfVS9k9p06~PzMp>L1mph+iT@@SILsY~s>SuwC@%f~Rs1LayW}SdXbtFI2wS=qBJYV#h2hk(D4aSM#l|k$2Z#o^b|?RPFdyi_e4u9;xO6WACj}WpR7Q;-a8eo|{Rb<;mGOT||H_R2Rp3E*4zYmWFe?O%sEVLb)yZAv0HbP<`!yB(m>2kv;~vsKXlPcS!K$hsk^4~Le=S1y8}J=jo8PZX?$=WYlRCs~?l(ZRLPPBVh)Br|WNv^ln*YN4fYJ4gdPJ#jY=D@_4Uufq7^yZ*)Of-Xt*d{qu4p!DTB-4a!V6iSSLVC<-?%>S-}~^Z^+xZ!L&Y->)%a*xyHn=yleNe{R>z-J*C%~^q@9048w)JxVxju^&KC&pWqvQ|?=N$G$(%ndmvqVEzU&*@oh#4-l*PghWiX%IpWV6y<G$qnUy%DnF;m+8nif|4m$|=YHq~?g%pm`5jIsYyYia&V{r}(N|8$xE+tChD?rYVYA7L)bvo*kV$#kr+v4+j?-Wc4q4LY}IfEM+ut2MJ4)wD$OI#toJac%VN&;(=qw8y;BeXwo@bMD2H5xjO5qBqP#?E2Y=BVrtFk+OLK9-c}6*ZhC~HC~?Ig?mR5m<J@__Q81SKooB5jlqTVFdR<|z^-sl>cDz<tzU`_OJ`x_+$mT(V<MK%nuK)=ZQ;h4Z*p0MIIj)Z9l8ZaqI_^NA%Ho50L};!{Rn^T3wMLl{E4VqEN`~|A6W(1*bZ>-lppSf25cdmJLZQ=$AWO`P>3jujh%{WbS_3u@C97E7RTo9CCLAh<bEkQbt{SW-AWlKt+hanC$7M}paM4c(S3!4`(A^{|G`z@OAin@+zLUW0oIZSR8txdNbdWOsLszB`=y@%`M+gQb=`MR^&6_Gxi579*7OP^>oRvB#!0<FT|`Y3{x(1?5jUlwg79BB9WhbYD@r|qX^fyQQBRe@;vIzhlNup@YGb6>Hbwf3W(rZ0n_#x87yQV(Fa2HUg{9BCv=dmm2dg!F|2NOsCH;9ULXuI<-c_xepmKd#yH9@Wzu>>}{Efx)TVOu9zrcw33zP<k&nN%8R#Fh|FJru4LcT9$sX+cOQQR-5_`g8b0dHN3e!sY$`?tBY{}+XA6KVG=j2X=eVMfyeAI|+*SLgmR_J5L$|7Cn@^SAkbY=?>eC;zvsX8ymN`TPomIxj`A(_;8j1GX%ghIO;Y!EXFuOc~N0BYU>P;4UpNuv0Ttj_%nOHbc5%;n;p~m_8EjOD7><^>joy&Ox-}Ohl};LGa2+2wOFtdB8kmo=E-#|G&rUEBo>IWGe0*PNWVbDh<du5QjU5b$NAPEY8V%A}J>9jh1ymx2pMp6nvbpEqF8bMS9{0^M=!jfjF0H!uiw?oKFtISwTtwE^P~@AMl3Lf=RG4;=k~J44;oP`R~ywFEt<^wonH)w$BUa4*9X6U4A&U%?AgTj_nJ=ky?;j1Dv}QhYNFoja^G%Lzj|-j2|ee#t%5t2RPFMZ0aR_2Bp7XC3p(|RbK&Wz>q5N8)^Z6#eXZ9tgA8)kbFSe1XvEQ1|RZX+6g@dSJmx>Lv?>4)owVf7JQ}O5WRoM=(?(|FoL{}nbbfbeo7-G5K_iXX<%?)$Nn&KJcL{i&PF|j&~fz;F<$$H<Y`TiO6;7`R3UA8Q(mJnR`#odfz7L`Htmo4UvfkIK&|nUQEk-*n1`h)KK&ovf9UC-YK`&o*S}J8BFfyTa__QUU;TD%wBN5``AN^;u8uhdG&NWKVdizVP-A<uJ%3jGm+}2wviPrk|I)4%4G90m`<KT8djAFO^|;@;#@yew&A(7<f7>+C{eEXM{?BMqkUEebwv7F@s_jpE|0zb>UpD`9#eea<wnMwa*PeN@>&k5Yhq)|M8W7^V1VQWO!Eg0UY+5jZv3(SlO&&~--wz8I>jg{4_lN!T;c%Th7G6sj_t(upnBy!&IMDywPlsv6M3|OMK=AT0h<Bce+lP}r$^Wme9mJy(DR_811$Pf8XwVBhIF^D3JofO^Hat4L0}oGc$DQM;YM$X6)QBs4V{vs?6fRR6E^QCP<!xcO%JM>*2^Y78Xrza#`G~LX3d4y6Z)~DZsa^uv{2yHz*w6~t+6i#$kcZr-9<<Gi4TR(c>spcjZSrG%y8_tIUV8y&YJhW>qHtj@u(3-ql^3k<tTkX`*HYNX`0vs~`VW?eN52Yi?^j;64SEiw1`IS)eT94&|GlXJ{=+N@YJjyBf~^f7Kn?Jx2KbQYUW2XZ6{@l1He<i{Q1SdVRGWZF+6~7_zkzy)X1*Uiu^!?W^W(|&L?Xe^fCS+_bAm_%5mG;B<O-_3z!+-~F<$$GB%4Or+}|}*V;lLOV%vlo&`7OQIJRwdw6AOV&;5n|<_n}hY>oL#)tI;{Nrzx|=(=8u`2TM`;NLHQ>pN69|3t0LC-r*Le^<so*0vsv`t94Q_V7<}zOhlq-=Urb1~#*RZ3jzoT*ma~&h<4Gs&T%0oWI2Wh0ODn*Y9HRpXHLyy54WD!Ts5-N~u0Sw#~jEr0;JLOjEI6G(cj%ZuigPf7ZNT!u<&~bpL<hf9uab<p0#cofZGXg!klon2ZG^|HGWA0n7t}9Ty<PaXx&R&wDPNs`~V}&L0Q2`D5X>U@W#S7zc00{Ge5~2wgiJ;p=B1avkHoy)8mkPeI`F@d#Qn9;T(E5$iM^8HeLO&Ht;y|73DLJ_pnQ(St`P((v@m4!pRqNBM-;SNG$^#k~rTPp0G1@nrghMBLmHi5t5j)cONgw};}&HbEGVF_+jKfs8#-^aB2HTVjK%y8Az5Su|@i^1mhd-%)t4;XplD+k$ANVc#lmwg#-H4-g#?4RGpQMDw3I;M9p)Kn-y2CiMcPv9)(uZ0(cNKIk*hOmRO8-h(TV|CQ+hED=Br2%rZDu@+QS`GC|51PrSNKWczCW54%Mtp@^s#(mSsS_mIo8&RsyKwWzMdgOh5a=ih$-vCMU{7Iq#lXQQ=2!r?G;{?p{>GwmGXK0`x{f$)Iw`qtp@?7}7XI68CogeUD`wJiD97}rDM8Bq1N&`Nr16lQj&6}t>1|&a_xv|WS-B$eyr5}ydtxMk`)vw^O0clsr!oQlMP;}&@_*2LAN5ARoN<M$@C-rxvK2O^4sw5tSrDqtbFLywlA;UgdpZ|B>H!*Vk-i<9VuC)bbbg)oue2co6E1oNm|MNQ<b$wa<XCAP)Q&v46Au+$R5%)Xj`M#vSZ;tf+ZK>nF=6_Me{~3z=Io^L3bN`t}?w`$n8T((I{I9C>+ZKg#_%E;i!~q@QwQ{za>oe485kfXBMmYH&&iEfjguAfZV22QgdGKE~4Zh25;IniJe3wmz|MJNQT0IS6_OlSqm>=adkC;R5&rtjq-ygJm90C`Ofob_z#I7g*Weu=T@_!$moJywmkN*(#0FRC(lmFZCocw=oXn<(J)3ZCs|1_O1Fh>x6-`pLEt2;u8Fr@=GxG!~zQlF4{Fb)^fg5kb`@1Nm^q!?9^{BI6yBJVf0CfCV(d+x6xRy8A<YYkY}Iv*U{<s<*810ARVQZLY{h=!^g)ILD^32g0E3SRxn5ar-Eusr++RUj(DcaRx;2b1#>`^kU*A?EOB4iHHG2U}Yj{I^2rNah3Nf1qHvXaFI7g@_>Q8mg`^Y)magN`C?JKYo&+p62~jK|{5jK;IubN#_I+(jGvc5YD_me5_H9Gx{2<{>J1!zn3ztF}9QEyJj@O-dW8QB>t!KcnW=i)G5Rod4!BpbQxS53wzYUfM)*&cbFT<xLDBv@zwRZbyedFsxMoo`qETRIikkUN#7c2PpEkMftsIK<|7bq{!tAOu1kF#bwhDoKrdhJ{?90%`GdMvm4pMT@1p3AwA;&CJav2aRBIvCZ{1Sej~cK0UwHi7<M=u;=J#u2f$?pa>(lQq>T0gy{z4<x+sPcBS?Fx0_-{w<FX^1a{jByrz2--`Y~MepO=-+*RpNu#pVjAI*8G^+tT1LbFGTJaQ1kxIX_{BH{mZ(aHb&i_cz@x)HTmDXU=II_8T|KHIm_VxLWCNzKT_8HbX|_fP2vOi8GS&|npyB$J`Miljo<RAjQP_Q|3k?4aQm4E-!KP}M7ZqJ3q;sYLooe+;L<S&p$^15&tm-l6#sv~Yx4R2u>{=SADs&^xJM80=tL^DAq~&Y?!+s`{8txub9+0U&=)+GTtM0lRO}CD+z&Ajs*Yul`x*P9$^BSfKM~h>&Q|^o3u8TWu|>lxFy1#M_sQ|~^a1P0`8C8!9$VQ=Yk)mJb8MYQwG(cj1~^dzoH`Uxb%Pr_7AA_ol^(#gTXDGeDgm#)rQqAYH2lf^0CGQIa76?X0Yl7)O7I&b+^@oIVrUfv54X^~XY3ChSruWUt09cr!OR1seo$)wx4CZ`S%aU~AopvL`*jdMu?}OraKAodzQq0dNSst(mEs4YRbRro<UTQ8c+XPW2h=zt4e<=g{BG*BM&x-T^1X?|Uh)1j8e@lTV{GH`6nX>ElUT_k#^pe+C36?g?pg!Zt*fDRZHs@4M_KI$G6!kBE}c~jt~G9w@(49%*`daQWvI5sS&c=m>g1{F;bk3N^YC<4Hz%Im(l<)w7M7l&u-X!&&)w*y`X)%-o#Ol03Ch#g?bjdmTebM;ygdJ9%&(#Q@wcn1c|VrCpVQHTvE4$=-7WL@F7BFBzi+4J^vTizGu6%~SS-H3lfi%K@6)jY7IvWTZ?F6O%~ET9==FZ4=ZgJO??2n<?<;fu&tp)ZM`F)wUI6o&<-?rD+W!mxrQKinKe3wTf2+U6|H*?4{yQyJIe&!f3Pf*Sg_td?5$(2;ScNERKsdD_#9=Oi*Um;DIUl&n27xOX<JU0XUrW9_&Qu;CZ2fFR65(rY5w_Y!A(%Np=&FfGbe@BIjMJZr|8FkuVXTiqCb@ijZ#WTw%md^seZ6q}*{O7e=Vy0i<JqZgxX;)x=iMgPZ_?vm+YzjxV!!tMnFr}3jway#(M077+^J#aMm{8uWoQ}lyAgeV3+)5eHX;9s6$bw$4{&HH{HI2=&j)96-?_cC4-|w;r$X4o9AHzIqHvRb!@Y{Zt4~Sz^)Ctk0c8+4pd5k*l_x4F{s#`Nh#;2!^a1`V{#S-+SQUhfuv7@A21Jaiittg@lm`e_d4TZ0s)A{R?kf;Fx+bE=lK&HGA#P%Aa=#w=POwazs7vt!@e}FqsS!~Mq5*Z321L;dC>|R+AxN3lklb&`*xyKDH~F819rOa}w$c{ZP<adW-H8S!Xy1`+TqB%Xu(+4@10CyG{?i;||0x<atc!+iTcJUlmZ;ygE9&*@hx)yHqki{ps4wkt%+ni49UVV!(4r|CHg3RJUQ5rpEj+LBKl}UQ^QFzBTSE&BXleoLmKK=SLE^myR`s;NvhL<;?#`@w{e@jB<;>^RDXWw=J+o}iFEaQqYkVzeUmo+?m80h`tNQxSBKN1aVBBwRjP+yepF!@=ZC*sp_p_kY=a|>B5atV7kpIo{V{X%YnB5rJ>wZhS-z56~35@@vg#QKqng8Ca=P>RsMI_^W^p-W`zF?Jt@IP97fb(MJ0Sg%G=THM?z(n2$t)7k$ZU?WKuDS0pleqxlI78I~NF89<>M01P21MH1Alc0hw@;>gg8y&v>cS3W?h7}#uaS8mhWt*z;}a<aHQ;pm2Y5nFd2lp|--{y88SClwuQTpn-yVkRJHphl+Y<W^#Vc>|fbswOF5&+aweGL{lU_hW$p2Lh$Zc|eJz?KOV=ei=j{6Sezhf)iM(El;FI+q3!)C&*QvtYjE{H8%q_03>Z0TMEo;{1hw{LOy^(%=W#{VE{K=9!58bc}|Xo&E?0&{{2N&^C@0l`BnA%yWid<0=_fe7XS5u>cA0pz>2mEwPZ)DO}Jm_!5U3&KX#VC>iYkDtK!Zy-**z~s6-t~nn*t~Mga)Yh?otWi=2VyO{{)Qe=B28`(qkY*sA{N6_FnBMpUY%_d83UiA@Bd5qhqVRrdeWV%sA?Jygh@0F{tz$5`Lv?g8`VkA48rS#_IVk+ihO{$UY1Fj*XMAvOF8rslK39I<u8xlP6WUs0VRuWc>TQA5y)3Y@r#Y5)*Yox-Rk2^!=`T|2cxm2Cp1-iOwCNc+zw8@xd)txwvcAXsb~49*S>^d>wPyU6@&3)fP-}gwxjuyZEsJ7at0I_B?k{NlIk{h0#s2w5?4L{jFZ`cjtoteam)M`p{{kQS|H%WH|F51!?ypd5{={rqi&#$w#JI23Yl6E|3+MyFH!}Wjn2%uLKl6RzzlppLVHvt^Hlg`1{y&QGKZ^X1+As^@>!$PgG(<YuAk}>_?w#KLN&dgQu$?>(!_8gv?K?wpn>@WuubxT%-akYQI7SUPo??`#3J>TF?(C06CS!cYt_X$ejQgSix0vq>L__ZKyu0)O5053`%8n4#N3n|WJY*EW;L@7^&gA+A;XL8cOvACc&I>lR0XBCq&@m6WPyTn#N8T6E=vI*2F9I*df8l>X|5D_BY4W}-Q4S%(fAIn2exOnMQv*!YgW#cN2pv&{{5Mw`Ap8%rmVCgH@!nF^4FnM;X(yx(1k)FUkD>n`D;iK6apZa2ggV*_a6fXa)C<-m_iK{-wU{f^CTj6`ZCzi$Tp)=Wku03e2Dv{~^Z)NiGms)YH@t#)3#B2G^|3T{ZUg#;hS)V-&`3d!NgboCZ?dLeEj53{pypQpS$^>UAw-+nlJDIa>xZ?lRO4D^b(VfSmRQ!q5^H-}s`;H)Gw!eGQ5h@9{pH!~c4#c=Tv_G#(ywQ!>fdAZ@#$1a@n36z_WTRlOWZG`$NIF@+@G0io!^-)$a`b0FS|BH)Lh>Z|Ls)l7w#9xj{RBv{-)K=@%|I7$p6Yf%fIp8W=LoFuAQgm_lS~s&)6^PfXG^4u^!Zbt!q?0fa3pp;s5Mx?gy>5Rq;QBdJro4zvFB~2*~}IjlAai8HjS2j%fOT=nXT;|0TF{^3(i(dO8J}d&z(D^X85${)c7r|Nh~4<ppv>as}o6_mTU`>l+X)pclABWHSHH+)E8O7^m74E^iOUriJ5C`7`}2q!-Y@;=pR^rgKxxf2U>|o9Ovn$$i(hz*h3!s}s=~@a~cq9$oUmgS__?2=}`cf>)2v;n%weQ49h7N+76TDVPRn4KNL)4;aXj+&7W?ra|S13iJdO5jw0ALWfr-_p2xf_Z9!C2U0H(NSKCNDDInvTPpsCF+YeJQytOcYA8f1J*dUJpa#N;;88UZDs_YkH4OesePL~KzYgPbJ){xaB<_>{>7oIK2T0-Z9MnhZ)GX9fP<h6bdfEHw(;CnpG{6q2H?VDxjqTGk(s@oAkwQdMpEeJv3A-NEVbifH#<a0gJnPfgQnjJAqAs*#z94Z|Ao=#c+qUrULay7oHa&f9-EP;lfdzUt(lK7{Wg_GJjE<IA+|3g9eJ!y;t@TRu=6)}&0c#lhSJLwfR&=Y3<>dczmdgZPvrvhjRpMs?Gc507wC|Jqi?VY5c6xk|n&Vr>`nA%1eP$cueCH7JS`}0C{0jH&S{K2>mW=<+b?z_qe$wZEW<%(@e<Sagc0Z~66aJ5^^b!6K?hK!`bJbYBC>8(LsI>s%y&Mrk?kn%VX{q9Wkjnk(`-S_fH2+Pa0pxzL)cKM7(N4V92Hg%2wP7y8$^Qr<%4s^%$p6ffpX&ejmBD|;?VUNey_5Wx7`{JR@n6ah5tA!l5YC6^(t*hAQlA@zJBQ+MAuRw-^T(jVXF1Pf0287Wd~VK7v<A2^=DX1IyO8@^$bHZD2JZ!31$mVQNa@ifKXo8~wg&k2`W*g!iow6{7ckKW1k(eA3@i<kc!5D>$a(Dtg#Q5p%PS2K4-hIIKy+Y4WrSK+p&nF0@Gx`6eskswRp39^9R5S71L6a$h5uE_^J>f!s*&f_$nWa>UUdYI(Bp|rEKSx1q`qL3=6@VLg2eu`DRmY1(^)Eh+DII)%XqGlG{wN=y4mNaP5ozVn^s?8JF(NY5jCJ;HZ=F^E1gN6R7WA+$XBB1O`^utLZEd`HUIIdUez$2dN7)LVBN|JL+B9(HnUW1i#;0Zcr09%w)Pgabenq?WG;PKM^_-c&Vul}Z5`b&r+r-ug$_h7a(#FU3yfqQKZ#sl(A5end+GHWWxiIKx7Br^756RS)X!3_^C~faU2nbKqkS)9y+>-mYR3Lm23B^jOg+$?U)i0Y7D&0Go0;Z+*NRxyr2>|Au7D+-%411~a#-A<tkQr5YJPvh7~?Zf#`ziCmpT3yw$WImVt-*Qki5U?XP6~*{=)qRIqQ99wfl{$s>l9}Gy_`X&w1aAD*kuM=D&>pk@z1?-p8o8zXB01OUV62<o+xKu9}Ge@;-3oG*$N}{MU5>w)FV)0PAKN{Lk|Lvk`7@iwOJah-40sxOqM@kEMQs|8L0uWX=B_xj+q&++MhwRo>b|Uyu!>o*)<QPzP>P2X0dbZj=9+3X#kc1W`B>?}?3btWh!NaiBj1aV0&IbK@NDyOH}I<h~c<zHdjOlZGEPz^`i__>%iREPYsdQUk;XcoBX*3&EcrAb=VWL=6b(UsCZuWI!oZiUtG?)b#@X0}KrqQb8efxEaERYaK8Rt%SfKl@KtvGIND0x}A_Z5JWwYoIte`j;u;h3q~6K1?Ug_hFVcGs%FDvE%k`>2BT{-H>icgiM5bQKBo$wCmS&MnJ}@As#Ay?XJBG&#81?IA#I8QZf~Pr{2kj32=BL%^V^i4sITMxWPR<d>&KA$(PL^Ng7*|oe`2Br1PreU|KT;@HLM!Eht-7p(3)zkM92Qsu$<gn*u5&|b*+lY?PT70`f_3_z4=s@)7s0v1-CV(wYO0HZ)bP1#GKBSnAgP$cI3ESH!CdaZiQt%t+1By+=1MdbsA+}_N}UqWi{2uQs!&iJit=T<t}T!uJ3IQ$6m6&Llw2=<NDrJviH^6PdzmMWgVC`YE76*Sk;pd?sqrC8l?qh8lnN6%25N#Q3J|iNyl>Z0%fpBjq@pqg>6e=fw8{V{MN<j`HS&bF~xm}{lb5#_Y>bgt4TgR-q)z}mp*?o=fBMRHO`XUw*bZvBlxnn$oC=trwr;y{?9@9re%m^o*%h+C89P{3j{99nDg7g<S-ur^!(EH@4I{o{FhHdzzQ3BfN3fZ2z8u;5b{5gabNLYwE@gzE+B0Kw#*G|5W0F2;#}w9`jJoc|M`pjKWXrPcMkWZ%-k2Lj|ukVhSq?4jO{syF?1kD1G4y^$?x3UVZw=+EpVJU9A$FzpATe3Egk<i)Azd@-1lw=_z-^NzaP2p-z7%_e7fYJ2Y}K5&(3+t{e1B5M*jCGK<*b&F!e2p&;i8}I<ORi`<Eb?4-C}x0zu3N{Q6S^m=pNZ3rL+nsMHS*uE_kLqHZr7Xof&(BN$W(e$;^g>VRpOh4KZ#BP>*ZfzT0_Dld@!L(-4Pf2i(HAZ5T%-G?B8`9K8ogG9a@$>d_H@Y&#gs=<BD%UY^UK@d&EkI&(M>XbT4H^eLayGWf}TOnyuZN!SMjF;=yR1ofqUkTy;3BssRq1H8wd#$d{i6SD$)>OHS^of?Sl5T@_Y~DP8=x>EhgmXVjg^hhJ)Y>a9{Vf!K-3MF3OU799Irvfw1wKP8Sz2if=e{~F{j96Pn~*sf+z09T+tpg{eKh|adS`9R`j6y(AI*QkI$8gzM`g7RjMf8cKo2vm1uR#K2K1;%9jJg6T?OTda#&6cSlOj4mXZI9JCw%a_69@;+7&0~i(!$(d<Am9wa)t&5VK{jkA?>Kjj{fdYU=sE#*_Qw$@{V9)B%J4&2#a;NEZLs%tk2rALg<Y5t~=2x<DD%Fa3dn92Ov8^(^=??)xs&eSf6=KY$(}C|BKIB)z`m{?ZN*L4-5s4_Pw}!K)|3v~nUsR*t7OOve@G@&B6tugL!-@->7!&xIW!S_^jn4dlIOz+J}kdkWDXKr}$=4sL8W;U@F_o7+Q(AY4uL$B}SnILsJ|Qn~s6x#qtM`R~r0-?KG&-G*GJ@AvDV`R_02q;<fD{P$%Z;3J-Z+n$~Cz^h9>_;t@u{ue}GuR;jw{W(ztA^pBk8en2+>c^5kAdp%h?E`-OOT)KsDQ*)3r~?Db87NO*PyxOJq@AE5VMYz8tRUPsk=w!4fso<443hqX<b1#oON0B;r@&H;DG0UJIYR8X8c34ZPcEhz5FJS7v1rEQSg9M3y24S_8Iv`lxF65sNt3i@Y!knq8#Z-w`GVTnNEXd75I?@ALLBu+5J`TAjTBE(4Z-A}!Z1Svc(0MXSIKLl$5I2vXowz!GT#X?5Ul!1Tk&^lsQDHL<-ljKg&J28V2rDfHq=0#A2>p{rO0@T0RCMm{SD9LOZcccn`M3`S;M7@TGK(+bCR`PoXGbL+?VyA#S=L76-}s2{#RD(zt|INdzw)L%+$6()`k(-3-^0eApgr#3y7|Qa#+Q3RTq|B%3x)uGFU+@YhMye+n2x+@_$L&qFCJa3v$1h;{Kvmg_-*oQg!~b8~=0cpJ>E>$^FNfYY#Ai{2%!d{!bp*f&8CC{x4Pie?y(A15S$(wsA26$^AfTgN)_%BJaHkzm+!dCj!a+U{&Ww@8CFBwf~2$6W-5aP9VIWYV`k|fS~0Q5x8U&J;8KbW*q<5{C|l@ClknRlfijnS8ff+tqVCiaF;p3!^3d~;uSvN|27k@r3D%Yz_s*19E;e5wKju3;D5hDz-m6b&E&ra`R~p6@5lHbMDCl&dlPvc(8<sPBNy;CbU=Yx;M+xVf_w<*kspCd0}3LzFZtiM7&V|6xnC4POf&+R6G;1jckklx=~EoO(pNw<pbycv41D^Qg^%<Zl>Wkmv=jObpdX+I5CjjYtPnD+3WBKvLBe<D1%Z+uNFBjo(E$t9UJx>(nrcIg9b1iDtbr81Ac8a^brN|!L1J+Y#EvmIX<a=V;Uk6r<SyfH0?*6hw(ve(c>#m(>N+`y;|mx!zB<2GgJ4PSMvcxvxa2H~lU6E^36i=^@=%URf2XKX;#aC=b3b~thT^}`H!_6h2dlPF-7YFV$B*3g9b%<*P&85MNk{0qQmHQy9SGv}1BT~lgW~=$z3<Cs=1s`_javqotMwdQ`c{T>AA;pZ?r)NHo%&T_DH=dc=qu~NSE47-TzBYINnsuN?%1;;w=1gkVjZ}>mi%AatvuEcYpDTi$o(~p{cFho)hx9Jl*00kC9$kS2`nY=wFa;h{?qqoabNfKA^(~8PiO46HTwI>THjf1{^P7P|Hm8rZ<aUbeV48;lLxei&+6F--mnm+^$TEfT!<j)>*pxGe;&C%i+r~w?`@U$4-oFJo{nI~{jl}(5Khk@>Y(fYLzoK$OPv7YegJtNuyj1jiSS!IM)Tjn78jVS|4aV={sNDV$CL9x$Y9JC++xgD*d3Cyol6HKFSswfKV-nr0pb7cU7@&<9)xR*_loza0gU~AN(Y4h_T+!@-0{CKu$IrxlexbabN@hcKajpZu)Wp*e?fcAe;=s}B)q8u9_>EV0RQfJ5!gK+0(<6%sdr(;e?gFeK;{Jgy^F%9S26hX6ckte1-AAm4v!us;L(eq9(eXH4bQ%%RsUhX{?blZQS}`*35MwY0-^zd%n1S+>jR~JV31Kq$n8Mo4|HFmn6XunIF9_Ep!uIl9Z027OqxJ$lBcl}U&*f!Ya_0X&|D0qc0^DU6o<#_+#zj}Myi3Ni8a+{3gZmEOWYRzj>-jTli;<&rQd@1eZ^l()wW?8uIn^)-zV~aj1}*Req~Hm#1K)VHNr<)DoC5CoU6aD^+OOy?gkp%mo^f~i$bk)##5Mt|HJe-{=+PkPf}wqRo+DY6W)U>tGV3Oyp80x%+=yTICFnff7!1Lw|@F_SNY7C*KI=|Gqo<{dSh+K^)m01+ShBui59Husrhfu9AGVD|C+94S(eorz;box(pW|AFDLhxw<qu03GeCqne#7}yuWo}EFkw6w8)PIivM{P|E0}O_-|{>^<l&KKUv!St#s_4L|6;|^L)tvNdwx!bH#L+4E_g^|AE4PazAk0Jj46Xpax7={r{yNFi6__81usj$^C`@A?s$sv|4yS3I0nbDEKWJqhU7+{)<N<%-#l<_I`r@zrMi3V{y#S18_Y(fcy_6f|O^!MgOk6z%GOT)B$PlyLTv7;nA@qJSDO;K=S<CJH_vtl;0N(xRMe;_$vM%k8pwgw1H|2bawqe`R~AI=gHjPhw(pv-aoj#)_@?1`NI3Qx=vsVKif*UbK70U40qICz>oY7=$41^KQBzZ3J?XA2AG%&1Tz;fk^6xxy?cHRpB_cv(Y+|PbS;XlU1h$}FQ@@uz?~W(@aRzzUi1TAeM-Zh9>8}%c?1pAbpz5~Xi}w_svi^`kUm6y%nkeoQV*0Dkak4jiv?mwTOpB#Foj%C6(}FTTtK)#R_6v1cg26Jcxga*DzQ3>pU02a`jI+GBaKKV_Y=tbgs}o%YqUlLuN5JE7zxpzsL|C_DF_otoR>BX$zz7;{3VomOgMQRNq)zX|A`D<i7XSwRi%HhVoso;+eL-{7P|h!fM|%;1o_-twVTSh;i?~n{yirC4e>>OgzsR<5zHCy&ABhzl@<484F|UY#=Nb4gyWU<9FBd>=m%u(Ha)Man`nfZ*H!dE=64iS)a%0<>qN_%@f*|{krm*`*l*vxoZ`FUeAhDc0pvW()m=(!?lbnUB>$I^_kzVz-%sA#wfvl%FT~hiP{EEyer^-p*H^eN{y)nD*wobgpF;jmp$C|3^>O}7*S87%+Q4Ilt&aV|e+P{K;lKS{1Q3Dt%mLQSf{A62<ogc7{W&`Kcbud0fS^@2@Le_mK8wb}XW?l0DvW}Uoi%)g|4YWu|J&04$9y{fKb?$Qd&%2f8lnR?w+G~CfYO0rRi`h|+)q?^N(le&(i3D#ZXo>Mrt^KR0fEW`T&EW}72^iS8ACqs{{!j&*OUL=jQ#%f{sFDE1_ZR#d4QMH2lCjKR=N5FaDOwg)$jtoUHIK@%oP~_gL~vdh<JdW1(X&9^(w6L0q^bw;MJ`V+`AUSmd-?%&#}2{VU2Fo1JMNPfOr9~J|&nZNdMuost>X1H!#R(FC47<5Q`rO5DYY9jzA2oWb`LA*L{h|frPP||H*<08i~RyB8HwiQpOq>@l<%E#vurgMrgi@ACFi5p*r)194P<5<ME8uG30n8eRia9So$tnTdKb;h>?Db(yvkU$bj%aRCsS7g2!U{op|1B61kt8b-%pFgfW&1F{63yQMzB0=tPA00c(wjQPOA1LVYfXQTL$xP=xVshe$grpNX$A=E`fZ9)s;MP_Nw}Q0ur5TgYcO!i5}m?IUxz%A9Q#$#<E%g`hqN+>AM%U1h%4er8$+h>d;px{$J7>;}esnFG$Dds(6!?7NB2FN>Ap^*amiOOf{_6;?3jujrutzT&;q^S3BWzpwlF%~kVz=EXc2>(e9`_sRR2<o^s}dL8BgH4Xj?_v!y964n(y!v8+4;JMt^i2d^w_XF3>N5ER){v4LG5U^%8OzY;T*dI*phpd~UG$4fhH?5Wa0Mp>TXdJv2jfU6!QSg{=4Uc)&@LFK4`A-kvyJRF{Hq6GQ{gI#a|Bp{3;ntoU{wodGM*eTh(E(MbPakpbU<~dZApehO{)-koB!vIBb_C-Fd47ZXag{OuYKk8<AVBHBEqa1;3GQ&3G4zA@KS=mb|Kvsg@82><10)xax<EJS3qWt+B7K7z0~?zVO|=e44&W{M0ds&rYCs@;Kv35_2x2Y}L>(~o$PfP>(mt3E-kl4;qe}s}2|5*oEB9Ty6ohk^LfF)$2;8UvTe}xyPVfbMm<I&*D~-T`x~(8^P+6nTkkOA&`VZ3=_z$Q6-~JkY{Y4MV$ba3JD2BX97;TQkv6h<q<WwR#CNVaKz94p_j?<yS`(YMFEH*Gqb0?1HNeoUJUzOfLBb`WQk}2^yVYJqfh%DZZu*gO%xt|5C1JtD98es}m$bEBuZmu|<G}eM1#VQ-31%iYz=IXaZ^SsEB8uA@M%xDXR*ii<k6Y4zrfC%vd!*!12N6!0FAAQJMADMr}ShK;CoOb7PbC<Q;$YYtKN%5W>cIhp1wpCErtmbyEs5L^sZMS}U4LDa~Qy*CqQm+y1)Y~Xk>@TOdzqXt3zBE=yj!(WX>sUfzsp0h(xBEiX^Dk_pWB$D6g)z5jL81WpUI4S0@6TzJ53?KQ!A!n`GaG5n&!Ps*68@9>)A<hDGLB8FsdE5{{S(cB5#@oVx%gku;J-&M{)ad%La?;?sk(sqYW|O4dH{+2G7ms7xvwDo02%lFS577O$HHU5C~TcG99w1&gWIej@R&OSp5(vh{NeDX50G)dF&pRN%HHTt`hVg7&0Qv3-(mVd1G11^AISKw>iMYwkB%fL4G{iIPN3Qk$bX6RSCV`bv<4VHVrP)%|Ev)o<o|<=`0v$BYk>Fw&lb8a(6yQF7qEf3mIGnW=j}l4-OyO;!WN^g(5Iu;024JJxU1+u9{6?3qxuPXb;$#-&N*`nZ5DLQk4-FPjxlFyfs6QoZiUs{<37DhAfQii1PmyFkU?e0_p%5XTvq9T^dSrxSdO|t?)NVT-+txcLud_<Hp2=C9&Cn)VU@_O%H+7Y)_^e<3bEu$+*nJ^8*3eV1;Y6-gZp6yBB>4W!vC?F|LGGnQVH3Y<MMfo#N1Jqipv_*9_Ai`Xd-TuF4Zw|GCB*weYrpKK53i<(k56URZ0UXglIy-=qd^^)|C-Isxq%f@HYwMw<Yj*#mnz9AUY6jT?LW659vqYH%R*<FFqgd{+jbL7mK@^m!$%>_N}1iXWA@0@1y5xb0zmT(Z6r*t@*FkdL-1EPyIDyU*>$5@46D3dh4}e)f&+~%c=F_*Y^<5Uxsm?9>25U@!OXq=gEI^UZCpuTI*bYzQO%jS>yT}={Y^8HxQ8f#B9EQ(%(<wzwm!1J-`fs@SohDR$Y64iTx8TH2<6Adf%lh;P^f*$$uNg_Yfz$Z2o7L(&yK40fN`h)8qfvi5ARJabIzN;W%=CG&Ym>o2C!JCfkA7GIJ>0=MJL>7!EIbf3F22>HkL~n*6`CC+ZXYe~!nD<I+BVZJP-hd%|!{wnYne27jmn<oMkKMw{Qk=xi?_{r_|w0W~DeAD5H7$$xKLP4-n9aC2KA?oc~4|A&9T|A7TH|F<^M8sNd$?k@8INSx=hw&%08=d)c~$JpjGU(e&4nrRIX{!87Uv=52~1a#4*4@=KZ8t&AG&F%B3u>-E{^TCb0mvKa{)Brd705>)Fu+$F}g@5laR69X1V}ID7(uf#bTIqo5OFV#{pkG<tR?w%cZZGItPPG{X3@ER1g7BecjJ0NpFR>#lXY(h);EW(<r1%4K#m5*Uo=O=xLSm<dg77wOjBwKu>Enq>)f7_4>-}UNPf+~Sy#71lM;Ra&vk=F762u!^P8?GODTG{e+jt9Xn`njfvF1o6Qi!C{Jcmdal?(hWiSm0!v7|Rh9Iek!;&wc@V|nc;Li$n#%6JQUByZt5pObqZ4Vjx|i){BTPac=&w#?C1o_sH_)_8U8RbI{8rndVU>%FNtoGK`YZm6|hc-&3R{m%3I$lT9m6@>dvJ<4ER*V0%+-iy~?-Jv9wOWR%>di=J!Kc8KzB8vA5TWQZfSH|==E{Iu-`7)=6ZKFJxUSG!b=TT$(W)icFa=H=!h5NH558yjGQ|bW7|7k|<KcNaRtSsYyuJ>KK0*>#k`5(G*k>bCa1IU;QK=>alG2d~X^8F@9<^*e`58w=VFPj3-MHAp*HyT^!4#%b$gUSEFaIqbL&5ZwBXAdR+N09%_0Tz!z!1A%GPw=JvpUD4zdW8E&<8fn0Aa0VQ8N0(2ZmO}oCZ!7>>Ok-ZI-vUh?g>}?mo|Ul|KBy><~Dz&0cYd4V#BPVAMn2ivCLd+!1_A6T+3&;f>^}oxPUrp$Is^y^Y~2d=&csl(0v9S#1GI5I5pAvf%pa27P=3iQ%gOskfV$vYN6*9QR9eP>hXjQt@6OJO&&P3&I6}*`QXw)#u61|u22MC-Khh;r~}eY*t0mdi^H>*)DaThW!}-^%o&KT#o^km1l)R*RBIG@^)ADAy^Qh(L45B8A%iO_gi1eS*&bwoo>&knV9sC~V#auBhM++jYFr`_!OtS;qlMG40>;&-;rh7rTNb||x)U@Y7mR*PCVrOnw?kyC(-8WTK^md_PQ=iPh~c%OM^r+B@Rpp78eS1m!^{vSeIn&t=_5&m7`acF>MvaZk;ABUg5er+T#d7qeiJh15;aoRYm&K`)SPU@dV$Q*$auV=TS+)}llj`Xo&8y91qbHmj$I9Olh4cW{4#1zM<?=oQ!kn8y{uXr-jT=UytTsdj?C#h2qdR3q4_RcmvK8Si(-zf*&}24W$w-<g)pPBpde;6DhS(#`C(J<Gfb_Q7nAGd!9-&XKediuO+BBN%<HA*^_2ELqpp7{G1=(rFY$hSC19)>Fs33fvb-MOQs-~{AKR-ryjD&}xC?pjvY7l|jBp}!gN*mnbAJi%{nyg#ubF{>)wb|nF5DjvPrI?$GRGP&Gl#%=dVgXdTxSfzR_cK}{lHejbG|h_!br{k4YP1zXT-ne|2xh9+vNTY#q&_51J}vrYpNdKPq*Kt2PlZY&z%!w)eA@+p|ts5A@>EB6TNe3fRCz6I1{@C>v`W_<a!=fh6W6v4;WHN=L7xuka`n6KLcpv(KQR)??D6AuP~o$F<^Kp`m54>&eYh^74$P7ON=qo^9qeN)ANgsGSlO*7`DK0m)$o@!AiH=%D4?_$F;3vXgc+MCVhtZJ<)z!V@~(Ub@ckKlWJ>7|F5Za^T3wga8{H2n9bNB&~rl+RP{)9Ek6GM3tJRXSj4!qxK&X?Z!eVbr7bli4_(yeb5$2DSjzW!Svx^7!>=0GQ{O3^4e6i0(17ZzZuD7GXkJLQV=q$p97|di#xi1Qt3p`DbC(i}`Psteg^aqdf+`P|{;q=g%?j~v^1H-hp1VlE-?xza^PB14E6-qhBaNvIJ|mYt!^FBoozF0y7$;@z{OXwOkFRf_UVcn!PyiE&3EZAoKfgLQzAm?Azg_|L`D9{BgMusz@*4W{F?I4|RPDU5uBkDC7+xzchSd<{!9enJfTc!1{@?fIJJ5&kL+?r&z4(6gV%eSVOgD12TRFaKe5bk)lp;`)KnVh629z0eBf6B(=**Y7Ls6m#(4OyQJHE%Q`0lpkyV{&+V66F<+y77K+X_C6=`zMAVv{l7*GB38Bk_N}^8bO-_n+MNk-5K?Pleag$?#k_7Vh)M(Dx6A3*)|UfAh@2aGgE~ZnK9d?z=Ph_gXYY`~Rh55awWubKAo|$^UzYV^oa4u}kw`$}4HQ-A?N8t|a;D`h1D)+k$v5dA~nO%@cIzfJWwSJqAF=4_-|V#6|M{LV~BE0ba@joQQP6`e_4D82|75%WI&3QLj?SXkYz2*LD#DMUA-^iW_AK15y{s|7O)Exx4{N3Cs-ewNT>~DjNG03{*7A$_A=XpUkBsEVyl%SHp_$KvmvgHKKYJ3L4-|)?itq(1)K_HBdblY8KL{Zd|KcuJfwq`w&)wtmB_)SbdhWEvT0NZ{RV@JQ@}sLDpwg<u~O**6)<xZk|U!2SMdr&oK9MGyB`~l;>NC=bPotf!hBYD&^IvB7ckbYMwXeSVewL%6=4O4U{%`Skib0a=!;(7|420isX8a3L9nC`;~<PMl6^2FVBDY&Q9#t3chP+BhqaJA~(zYzsprF5G?+Gy&VFW?+2`#4gXcr{y#(Y1@vE`#{u}Rw1L->iRAcrxZ91v*7+mhzCh~!SuPyKZSnu3>Ho*Uf3fEODaPja@BjEX|G$5TdxxUQ*#P=?&HKy3b#nM}vNtY~`{xtA$Ybh2T96*ocOVY;Po(3)sqJ`jZa1Eu-GzrI({cZBk{TCyof>e7x^W>fO9Q-cBh42lBb?#H_+J+P_y5b2>%GX@&YKJQ4CMVAKKtADzk{snfA+U)eeCb%KK`%pzva39Z~Q0PVl$`%{K@YKx8;c0x=PIh5bC^K*Z--0K(k?Tn1i5ob96pHU645eqc^WWEYFWo^90IR0jVpPgTU4F2usGnbMaW!E+G8(q89j)|G}#!<9JHoC-{G#@jDWiQhgO?rBoU~?p{pr!TAJonEw4@GV_3);ka`s3HOh0!=v;2@%-vhe3@~YID;40j^WAqy?AhPJ8mCLVD3N-ppINnZ2?})8+>sgZYzTAr=VBEaw^UiGc-9%n?I({#d5{%(ng#vYs3zjN5FzYY*k33D#f}w#kwjVay6FKS=RU>4{8+8gE}Shp>CN%8s$Dmy|SO9emOxAG^kJ%4J#H$lgfgUXjY{ZnwXbHlS(DgxKb%Js#pRIxn19^1R6+5G_fdyX69wk+^Pam5iN;URn5?<ni*Qwtb|rIE2C{KbB#I{3LWZNqNAXW71|RWh>mr+O|+|HiFU@g*)}z*pludvTkv=e+Dlo>0&R%4HLIWv|4y4)8g1oo^80cfbv>ekf%e9CHOvk0`nBZmR7Pt7@4uO4c{H&s$KO{TjR`565%T+5R@1*v!Aihm{7wsgN6<{ZXI_@yD~m=%18z4o*QKD5ML9GwFNelFx2d`|&*$@NVkLjGg8F-n<nQwHMwa@R0{>1^p4XIrtBHX|Jl4Rx0vc2)hkBKXN~KZ9Ory40DWar8tqLVjqudv$R^|&-EmI6tOMec_QW_Q|3&WxmQKB%+ix*;9h-E=o6fdChMShqU6Xb&hQKd*;h01)_%xDP8^W7^)F)EWszn7(r_qT-c-WJW}Kl2-9GfU=k<9jp5Ux0|sn*ZT0#u`8}7pTnl!`xr$|6~r(s4XiI@39Waz8jHl+KioH9@rD<h22qJ*b%lBsR7PN@K}vV=S9pHro(T=Bx=AIc-UFP({2Pz!vAF9|8F1X|2KDVV^1i#?xA>oE}mtAmx|jL6Mb=xI&e0gJ|HzntqpeXSUS1Ck9<FYukM`3+xu7W&BN>X>fRN+zI`6guN=d}GrMt{I&n?r1E*)W$m`xnmmI<yN9h3~Hq3wrwaRVoXl$7`hT!(RQP?!c8cs7t5W}%{${=C@)=nCTwUY;`eFxiNSkKQl&l`s=3n#*7g{|^Nfig#ey+Ha)5ccyBu!hgjVF8~bHB#1Eb6G+d^R2nAK)CZVg$O?P2p7@QmBcC}Y+Z{Ow>4^C#$}is7txE^(XY*i@0$7Wv!_0<mt2iGo6AZyW;5DtwHm{n;NgG-Pe+!HNb+$~NcD9_s-H8q1#ZIjz|GhdvK2c++_9UG^^s+*)pUNA>gR%ZemBnD0r3J)2emKjsK$G(M~bgg4*Z;u=)D1nJRax14sm=xq)g=ba!fwo9<T{Jf;MXe{tY}P_p{C4MO{z67rj;1qT+AfxQsdIQa-b#stj?Gb!qtAHm^pk%u~f@6D{Dj9E;>LlXo|i&oGGR2RSZA;QA#9aFn%d7G=Z7-VQ!%?cmSjLB{6+PEsy~pTlDKIxc~)!xH#7EP|K)LIq!kh45c*oWsxk9hdRgQgxo!+J*2~wE!+lX2E&U46L`C4o5p1I4mIMO@+N+{$#A3HyJBtj>j_FF<3Tj3>HrriA7UJVBw?@u$w%R`=hYbX0*oCkyt`3wH=A&JYTSC<~XdHH4bZMkH>1+p3dVoqp*1LP}q$hg8AbHV(#dEm@}#m=8Wu%+19-=cO<v1dtt`VZkRTx6Kn={#FPQ;F>P>rES}Iu&4DTP{t=sH{NEC_{zr(k1#HlLfP;kp8|>7(n&7n#sr<j%5$cY8v4J?89ExM<Q8>9H4kxz9;#gWF_QwWbd$23tz10ZYFo(IqWZf@l;Rpn-nutU3UU>iKPoLz!)XkrbbH}+j51gYGoQw6q*;ww=`(I4<$EEaOWbBKl=ih}F68CSP!`Bb5;oB#-@!iu*eETdDUq8;ko4Z%=;`%8(JhvB_M^bt1DCPsfs@>r3z6dow_~z~qRflj>+8g;<hV(DoA$^WaxJ(anj`w>idMi#uxZ*_kCc+gbBVBPi%ANP<r8Gt6DZ76p9*>VFskzCXoK45mQ`_+L^iDiG!+SitTjRnWe0h04u@7J0IE=5jEqHOB=bYYw7d-dXS&c8x@m!uS+s{t!U_K-LB4hO$2E@%hQr8iWhewk6H`DQ)`OlZ<`JIdVvhjxa>e2yxbL|MeyLJ>mTtA9;H;&=Q+o$mJozwXF-WmM*@H~FLe-6LgKaYRhJBROYox-;_j^nGVN7Q#;UD%74^ippw9N@hk<aG|?yQ_!r<1Jq2)(Kwo2)?;;0B<kt$J>kiZRd8WQjW=I|G0e;f80B(@F)L<AZuUnbH)jU@2?-mH+&Ya&+f!?nb+}n8Xobt-8&SId;Fbu>Ej+A;j=w1^F40I3qGGW7Z2by|L!aPy%(o<tGQqw9@~b8M^pI!k)qaukagtl@VVVPD09Eb9I*+=<hvudxi1cP4$7SIf+RwY$(nO<xW?~X+Y^JUyO~4oiDemw>+;#bBxD>&#?AeSxV|?5SN6o?!uCj<P7THJL=%q2`D1^iH}*$(VQ-ix_Jw(2f0PgQ$UcwniS!|VJh3g<T_HV)aWlvb$^NcL32;SPz-FXyJJo*^w_T7H=*C#<hVA6su5b_RitxbpFn8{6RmYS4HzM9!=IU9eHINA3B;$8ilG7^@PRM#bGPXB(J>!SHtmQEq-ZHLF`tXt4;cD!k%>S$F{e|yAlK0!sLLko(Z?F09fHcPaT@l_mlo&$J$Kc%lG+a8e1DB8PR=99z8;?h0UyKiueb%WpfTdrs*FtLqu9)yi{{Qt=Ca&)?;S9a`>6ooJ6U#EnP2pseD^5jip*DD_^}%BqV?sBt#pL0AP=`LPBK=Yw`ZSx-Ll770k6p>p_~uC_-rT>A=QmEP9N{kCojV6($s3tZI2sQRMdRtQcp?GMPbT5Tsbs!K$*O#EB9ZS~JnkRVbB$cz5k!43ss6+nq91!B)i}eaCsXnI!VY|WeII#q5Z`B#6ZcNz#|Ibi<D<*?$J49CHT?GCrotbuZsCuY8Tj+%4gC7#5`KAd5kKAM`I#s2L*{Y3yL}8lbNlBz+`oAQ-(EX}w^yhW7x(J9k56x-PLS&tcH`Tt2k_nXLp<*&e&Kh1xlc|#x}ZwIuMf^C{KdZ^c>nAwaSiXk%)tAvZsYwM;_V%lncTn2?fdxiRVIFW&U<}!llOKNzdXKzUmje-Z~QHP@_yexy@L0zd5u>$i0k<46}O-A_#<-r!C8g(PcCRY=Vz~P>c1iQ=5Efm92Y#ls`2DJ-akBte^6K6U8m0Qw|#SYuf~;q_+FkZHR1<q&b!-Z@XNzX3jcV-@)4gg@9#(Me}CsJzQ28%{};#b{mtX}mKyXm_3At7?YGzESyHR0Gg){oI!T>-&FB02%0axmL_c{(YsnMpx8NzY`|+urs($Gi_3#<rrKi-y$J~E#dKd0eb8a3^!c}U<rCreq7k5Tc7b0+e2g_ZtxJaG2v@Z!4_9o%d{#0B#n1=KFQgD7x63b+q*%gmdJ7RD=H3G+y!&ru~48hTqV4O$~S2&&~Wu!WGIN5}Q2?5v>?TZ~DTd*x?6Sc&J9(*Hp!x<@z)8f(7cwBsWqL27DdwQ!?itpm@rO!{8^CCUR$9f~)6TWL^!F!nvF%`ZmrYS!w{Q(^9CS%Rqi5T3iHEL8YtvFY+N?D8=&;vf391$7diHvhcaP|0JoZg*CFW^THxCX)NWbS~`Fs+<~Bk{iflK;QIxrHm_*y)(fiti^Q-Ebt#6^Da2;y|Di4zWDJW70>tZ}XbWeZN5Uaz$azJWB2*H(E8UiNuH?e62L#23}^I#nX$2@bJVoJYsCQuf`3;D+vFe9;XJJ(i-rDar}vlB{)Kz*dK+v2f~#GWKaX7Utz}1VBFgui6=)i|G&Pv58vE8h;K6(V;R%lJ-mRQ9$&{#&oc1a>pS@U%X|3!?IZm8_96cI`Vn>RKK^=h7r#EgfuA3d|94N3`zP>S#u0Lv?>MjX4d4H7uO8$(${2oeH(s9Gkv;eM3u?fZ^b8V*e;{9eWgP$Gq2~U-;=khP)601O;yQUJ+!g-aBJat?ukI=Q_2xc)e|3lVd>6mJzK>sJ`z7z6n()_4Y6Z3H{Tum?=mpES8A<~b<oa2>7oAYNzM}t@;O(s(WPL|I7d+P7|Kr{%^8W;Wx^V<QlB?g6_upI=?(=u@+5AK=As*%D`xo)+)9XZry5HYl-BtJe8_Qpw-NMiE>>piG=lyi&3~`#@Kc=3mJX8H#4Gp-oS80Oyh%c`kA`Z}}?b4thdrDvSlwRYhaG!Wiy?A~>_`gT-|1q`U-ihtFO}}yDP$I6;Q(WDvad~e%k$|i83)c>&<HnJlxOrkPGEN`B?K20FNv>p`KZ;vt4<m#7*ZJAy!`qcEoKt!b%`z5eSe|CAJ+(cG{HH#ohTuqI5cZN^yP1FQ2qphb;uSQ6@2TYeHs*Bc2LF?NH!x<dQR{lBx;^1LV|)<v)Ii30KjHk!>C9uNVe68~*t}pIT$mHPv2?eah^12oqiaKpPjaq#y=pLdZN{zh$8q(<KAhrj*cIl%9BnBAS5HCE>d81L@&Au^ALajVuQOD8{c&>tNa!XUAm4ZSJ0jI%4dUHaBH4WvT;`2O*E;3Uu6k*-s#*%oEJ~tb<>IJau^6n%6#1un3ifit+sC)@)r0GJdGjn@TseZrr?;y)!yg|@QsV|6XN?^^9H;sp3ilr{wm&!&NB&3S*6uLX2SMcyGA4P4o^#;o(PVn_?c_iG?hSJPHe=I$#{I`v@w2>#FZgbL^^n|uj6c79PQ1XM-@U|N-@U*eZ=c|omv`}xC)e=f-3$2c)=B#GL-<Bw6l2j>^!{Hl)_leH?Iqu_XD7F*bq*v?5TE}Y{kg=#U&)6*9?(-$M+Co6e+1(1f66?C-|y?s-#;PG__tI{ruTUNLikS}zY-rn?te+{6Tf}=5Py927{9%F#Cw0j`+P<`rDi>5{C<M>-?IGrKDX~19^#g|-d}Rf=j8NbYRuz{8cGAMYu(7w4uNQpKy*UDb0rt}om~ByG5crwnRkra?{4UP<!Am*@kjr-D|yN#{QmSNet(gPKdDiF@_zs1eg66NQ|jw81$nmeJAUIizdpG^e?T9Wd4~B4JyphWmBV~P?G#_|_R>LoP2P)7czgA*!fWOeFV741PzR_7=jaJ8i7qhLU=H-0d57TX`F%<o?w#0)JIA--&e842IFyQvgQ5i~xPE{*l#c6%({c0IPTW4V4|mA<dzVh&;k7e(bmIaZXI#RQjLUd*^Aa9%|H0L>xP9(0uASI}%Llh9uW)`(BF^rLQyOrB9^feR?t=+IYW-W`*Y@BoD%Ph5F>eT9+z;N2?cxcfPKU>nBu9|>JY|h<S=Vzt{k{EM_^p}=zm+r9*gl`tGvOh;x0`^CbH>1a+Ayq;dM}$Hm^HM^f5f@1PWH$+e~fv-PI{GaYR`IvIL<`qI$In{@FD-d|0w_e!T05IiZ70aI%8kp25e{k5GQR5>*vFN?HsI|IU0kTn4y2eis)Ux0(#Ufk8ZrLF15>|L(MX1X;n(;Q<YL5YExoZAl^RG^#QMLo>t!N33JT*vL1kN+(6dY;rq-HvbcX|PlTF-OvWR~xZ$gW)DOrQqo+rx8|0<rykB2CNdEKP;(PW3{j%`?A209W*Ei(TH+(0_{Xf2Yg+G4y62E`<8ozz>62I^r{gE;3+j|$OAII=D->t7MFy5cvt@!_vxv=2b$#lj47n0*M=IFS8oZLTaaNmgg;yrGkRNQ~Z_xz`ul1rS@=RCNGKdBFYlAj99OMauj|C2ud_vg3i`|nYI@8gd*j}`BK|N1#~>m~K-OZ@fymw5k!;5G3A@4puvctjnbZ+I)7K2!5M%gZbN8{E#~`rna@`+`43xA>cW7Gwx`?{fc)+2RR)k>By~3UiRF`0K@O{PkKqi`>IgycbRV{<XT#zkV?A{VQt1GiurcYQ=4xBV_Wq@LJz7j}V9t_&OWZ0P4-#t6B%d3%tH`2rsDxuP<}^;z6YWuX+5{WjRg_IDdeiU>}~G-HV6J10FC(xJwPlJd%dnN7HcYD0xpkxOsF3Zk^nVyJrt6?mxM49?x!H#`8PZ@bcbGyn1j8uODXO^`kp@c|QYBZ(pGfoIxgi!S$m%adBTN&e2bwruRRd8qOFRpz0BJM|fhp$xU%z>gCe2U=lrWRs2s0aA6+4LDkDjy@162K>K-&^E2VSVg@{yPlLxY#{9*T7+c4|VWu@!Od5>&qk3bmbx%wl(DA>;xj>Ii$^)F-8IPUe9*A;Xg0S^-a5%vm?|=LDqx}E**=3wb@Wg(TBhuYhBgT0#d{@naGxH9+i9=!2wKm4KHpi%zRbbtsGDbAd!LVkPFtBk&-d{Piuqye1F12b<1KZ;x@b!Zmcy;S6o>B83o=C&JgL=InnL{LVZ={-ARN}gtPmKHb>G7pL@cOnuJ$5)fP>mrJ4<Pdj-9HdRpOB<H`P(bZQ<<-)25rWSF@w;%eREh<D4u-}RuxL1XNOjpI(7t7W5e;=x3BRFW7Ur@?&F6?*YNGF(|iYysQSK_^d~RR?8t`jU%3C8IwN&t;$?p(&wrPg`S3g;+&`nZ|4w43d`|qtZLI-k@Z0_K_?2GZ=ex!>zw;~0UE$uCJ#HxabZGI>`?VQsjr6!ky#MiQy#MJf@kVQa_<(O7kbifL_<kdY`&r!1jb~T>8IqR>n4<`OXN>=yzu}i#$CL;7iO=&L{g7~9e(QVcm(-!noiGf2JGNB!nEQTvb!rWpaU-!UAxhuxyEn>ri3a@2oZuJnW>SA3xd5-PbU^9`t}`#Vc0}?2TORwGUf@d}|B`zAH9vbpU-pLkuetwP{K1vOS{vvGp3^5toxqcGdzlN+3mo504WQRQkcb-xljs4GSf(J@%>k2!^+mVVO<-9*=Q+!Ew{|Ts!FmXyLjv&T*#o5mq79F(omF|l)kE8GVNU|iY>&jT<Y4S)&c9cDKjVH{fGd*e@6!V$_KW|QdH{L><^d^w8yS0@n1eg2c|F56E`e#C%;_@|KIFXTlBw`mGzD81O~e*@{PnX&V)?{@n4@^#0V8|1!Nd;L{#%@DR<|lbyj^j6R|59M_^I}QD3=8|6z_w-e*HNAzh%yPGIlF=`m9E*%Mztgj&mlcc8%%QeK4n2J<RM@6SkeIVrmCVOm6=nCbqRu_t~#u1?5w6-%q5!2fn(04X<vW!?VkW@rZnqHGyP)5vljjo^O<SzRVq>AnS;!@x%V~B|fS>K>8$HP6<%slWy#g^#vnvWoHDnBtfm0)URc2G^z51@<vt57NIT{&b|jT1I_AGMW~+#-o1W;pI$w}yQi7>Ci4Q{vtxKoZ}RHQPR;$(IT|3`7kqc+U=IJunP2XnCWQMZ@B{PaA0!V^zWJ!Ze{SD8g&%po<S3HoK0PD#344(2y&nBr)%maQw{A5=1^Ljo&;$HP4fx>|@r-eo@%KxkE>HNZV)=y+k;QRU|Cq)7%elCJRzbM`8-K$uw~i>@{~+G{g0wrxI*<F9s~_My?u-Gg|IPa~`QkUjyZkBnz>8bDzL3vKawqj1MFW@%Xf5C|sW0R+{Pw!m0jWb2hz5Mc^WM<oeRJ)op#@qGUP)d+kMNYf?cwR&%mcRL+JOXI*cpwJjMb;;)qU(2qIdI}|K)u*scnS-Pd9w|<UYPMG~oW_6S#4FH_nkq$5X;^D2}mT;yz=0nm==WdizvzJlWq_dw(O}m*c{FX}gV5ZOnQtZ{O9k;I-UVVe6tv*t~F}DmTp^ht)PiF>hoaOz77ELpn9X@NTXCN1StWSkCw#hkddB)S-2Vab2wP|37~E=A-=oD)R)6M$*f;FH>WV*UuS;`C|uQOux>s8`%?U#&yHWQJt`4XdBG!U0>bPw2oHUko%j|&H|%(KfUUgM`QC6*}i1bhynOA^AcWWoWW!Iw!6pEkg+FR#dL}9YL3ty#vGwLL)6?MH;j0%`Wq)}4Y-u#kMl{sILAEZRIE1+$9UmLtgl)_ZCLB-=vups%9BJ7n_84mc~V`oFVz2e?*H$)R%QtH-ijYzKgPS~ck$hWYj~S+3SaVldP$G}QZ&Hu0n#S+n!ezhOMCh59#rjWk{7?DR~88G<@nd+$eRny4_LnAF{wv;mqAaEaRP6-E&ZXc?@PeNoiQ+NSo9y=Z@sFO5E;na?H&35BR#<P^Z;Me1H8)6bqeD54bJ~bFOtP;1;zFAx^7Uo|14J>fx&&jFSkUm4y$@U`MuJvd2@dPuI-OSsOw4$Z&TwxykBpXzexV|8}o&qnFIVJ`5ARk+7d(q-Whr+8u0F>_=01qE<hmlD_R4{f9`*EmAY_Kw166L<%r4!-td^@2+#P;ADrAt?kC}7T9}G?+e1CEX~}d9X<79@dN2IkH{;FI`*?BxCLYoQ+&Hxl=l3S!cyc&nKXd+2cWe)ICI5x{8<FBe{`;&)5`BK6w<F<zcn^ET%AEevpO^pFsvin-Xdm(XOQyixi2H7K<Kez=5}fCb#)`><VKcZ3hIVa<uFdOW=HQP1BhC%$-3dpifqUs!l6@U`KTB2q|ND<0=l?7EzC&ToNN`(BeVT@4Q-)*0z^>@st{FDX8w0m_qv1Sr7}ibdjYWf7VtSYAAKXjU{fH(FX=;Y{HOin4_13IJVbnG+hiBJM;rX=_s?U)09lTDCUsHXHrN5ysuB7_ub%zWM5dL40al?iNT%!-Tkl>5c%wZ0MyCK!b5z$-h;j(xdru1xr;Y}+lKh(ED1@$b%7fG(vsa9FErZzU<|GByGY~=sCZIil)4-LR~PcrfSqYQjQZzAv2tMhxv<2{=Hr?dFKQ}JKw!@uRb_Jh)a!>XN3Aa#as&hH{}`2REGsnmJBrMJDu-1O|WNbHI7f%n=4u<70GKfd2~&FUjf<t{H(&i(%FL&pBw<TZW&qYJtX@bBDza5~2eWO4tw0oi_d7JuHQzt235U&yT=$VvIFcMiniY-%v?*Bjmri(u2I`G4zvQ=>!ik2g=513bj9Pj2F;d*@XS@I9Z8Xn^ov)i=uD7QGZb6<xi3N|oZZe!6oC-&04wr;dJm^8|g=5!FB9o2%jlm@6=!d`9n|aX1C1(j$-_;(-vibyzoV3MO@L^dH}M>&A5u5#;;-wD+B1SzYPc_netC-<&z~ohiwjnMo#PCX-@fVlOcwSYq#r9mRsZ(d-SyE>#3X0TrntRgnJDK}19m5mD@l3Zf`RgG0XiS$n@qX3#Nuew^$2l3dTV-o4-bZWn7m_x;?@Y8#i!PpUq`viwvWO$>)2Y`2=LCG)0a&Xnl41MUvQ|2lql#DVP+@7ECftJFLD0%ZUGbvE!>qxGL{Uou1G`!Zk6$$BE3iF+63{VwEzjdRDt&SDTO2bvLYt<kCFhuAcC>}z4}!?)i+0(mgj&l#cas}Zo(7Ae6mz(1csvfl;-ZMKG!-4x7Y>>u8%3p%%I@pQaG#wRw<F~<_~uGA$T{}UgHi7t^`r5D>-xb}np{*|iFg~BvcGG{xN8>i+4%R2&PO~J7P+toayW8uO9UfO)1*k>G|?H}f?bV6RB8<Kptu-(_dYn?4NFP@DBqk3Ul*LPGKSbX^|#&vo}@u8S8Z<0Qg-k<&9dwG&PYs=BYaIxeF>MowfowJ4XuUu-~6oq3Wo~v{Xa8opZ;GaBD%d)!gu(GNnvAP{XwTufEB`KeNbS7WLuu5u)T*mwe#v{*-%V9gd-z(R5+JsTKeSw(2cSDQ+GA=0d3MB3`7y0gd-uKOjeIw?LV}4>+j_LT%#^X}rka(=--z)QdvO~Sqyco~Ttgl7?SFi7Qb5xx@N&H{H<11%W4j{SEL*^LYlxqD9-;gJb93Xj=*h6Ao>r2g1eW+s6metGygiocf=Z?&gDo#`VftA!&M`8jL_IGYv0SB9zm_4HVE7yC%$RRjYkd2F_g*Z+8%N&8U5Ff^VXXZg05Givn8Sld--pkxfm(@5x><7tvyJi3Wjf+)3|1M%*=AnA1vAr2^C9a%pCbOKZ@b7Lv8E%UvW6Q#cu%9#(;|Fv@k4|6U^AF$qAHkgTO@@0qGB=jFt=7~<E_hs5-7NmEvR#vX*CTj~Ew)%s#w_Oi{Y=`S)yIE@|CZ&d4^!StAoUSGn&0mW^&ZLQG|5=DbwBcLmk-`h{gZ7z_@jz}=W^muZa9e3i9sli+J(Zf?P{K(tPvFapOE=IqW=$T?;g=Oz(@7@i7v<ua!2%zb=bMy9_wtaU^i_v?8f$n?clap(7zSt^#1f|uGG{-Bgr$DI?4FSdvE+wJ?B2%I-wvr1`jTkq3(P!YES2(`nceq8i4t}n8JUajQf!Th~;YXKvh1qK~A)8F+7e{R(mW_<<AX_!wu&OP)#2`o)LxU08i}Lx*Dsjt*~f9-&d}0|L$FIGB=I!pZfm}@qhcAHm9g*&fgUGGPZw}IKO%F`zBl<a|T5Jmu29w^i`Zl#dYGSFw$4eMc%P~3F|u@i!A!RdVS5lL~%v}p6KI&PcE0>TWWyE%r(C`lc8HCE$i9XV_Bx^SCKxJM`!aj4xrWP^NDU!U)?;G#MsY#>v%G*GG8l62}2@vuHUxRSZgx_3oXrIGrHF+*SlBO4oHa(Q{#a$KS1UUrHA_95IIq>Z;bt+@~%E-c~7T?`~91>_x1U$w}bCmYc2LKpN$>MXHiqnfcqjV;(aQ%FSg?6sqk1lO|9#5W<IcJ@^FmrZ-#E|KEucFzpZkv*TLN9@BJP@@(w}id)#OZzjgDd=UnixwyJsj7pF4*Z$!veJ2)?zhS_5V;Oj1J75*LV=OA{Eo0``oYm}T8j>BT}uT;Al>tnQOCVx?VmV;XT31-w-?ce_`W(@xtWvSsPO%6pF?Nt0eHFsC$_X-CH{)=V5K-qU(_7F+ftKKsv?;cd|7>kqsK!0QgxT<%eZe2PD3#W`${S)hF48__h1DGrI!lDshK7H2B<w+yk|5=TleE#;Y@%C^31Mj@~EBL!@ApT2<|02|u=BcqL83Vk<cz%ueu9EqFv}?IhxSkb>s$32CSF$6N$!Ed-O<phk<=@bMB_Dr$u?SZgS2N>-;pedlD;LdGe6`^<>iaJ18|34v<^M7tK=3d9|5tQ83*L<;@m;Knc&{#dW^yccS^5C-@b~fza)1H#CzEk8Jsg?Th<?ngR@zR7oz?$9eZ8E}c&iePRcFz7^^_V1Z8%4Mtf20YzK3#_jQRC4_IOI;0+~~#`c?(|W!WkZxFd6vX=<J|?Zy$A*ByroSy3pA-G?x*&DgSJF6K=dg*o)y*Yykc4J*}nVM%t9!hc$@H)4D?>3ZKmTjSxpTE%>&_x1Po?A;{q>$Fqx-%Gu}gZP*DzhpW*_5HkM+~0e}EVW)m#ss#nn2A-h#=~MrFLZ7D89w>TJ8-a_`C6D8VcHu0>xp|=H@HsQD>#0y^YigfZcW~^3E?hF;mLa2PL^?=4ruks2bf?!1gYWvNDAGJXy!n&Ptm5?Ba|jF@~dc**<ZI%Im$$8uHkLpR@zE(5^slvC?=Lq5cehVvJSwX@!ng*f4KDPd#l)A66gPPAAz$eLHauf_bU!KLo@8%rq<#tpFI&4L;GNX#Sq1fuFRvh%pIkkwbV!2r@1^y+Ni(YSNhQ(ex<m6!{T|k&)9#j;wbKx<*D&#BmM>Jg8yq-5#)kMtyG_*a3$**{=biX>31)}qiZKnc`_aG2X<qd;}T4_90S{_=C57fZEF_cyV|QX;{Q%1<GGCg>GOb%cBN38CoCqIzgwZ1kpphYJS3SnEcj1H!|8N9U_3pS8isg(7i?Q)gXzSP?bK1PU*DCstRw&Jn`fyzPHFv>m!zLEN13tzQ8+;6A3v7<2w59p<bbo<{J}fNQ?xl*w5vI>YR$z(^6^>LIXm13d$+E_(m9qGGw^HXu7h8@-pl4&>2m<$kbfuy$<n7Nefz|Jn41H_sQE(~izNOB5&!$QEJlFfpV-*5-d5YM(_sO2Qp2ltX)9;LM{KpMN1LzK_4(3dzaJk*YizWg3ae55(Y<qPwD>FYlOg?I3v&y`_T|4jo%siOX61CXPjLJ`iT^j6#sAII*+^%85aYQ<?Lo4Vd^DoB30k-K03!$WMdV&DWXFXeEn=VICE+?3+lg?r8l>7+`YDB<77lEy+EjFv@X@F)AM<atM<Fp@B>Mu%-U7_)r7!<z*bcR(=(yltbbmqs&ZY(7ydhLsh2ntyiUXuf+2e$rj&`t}HU@)xc2#51YRxp;d&jcrDppB9iE+%N>2ns2ls1x_&-9}=(dwOFt2}8lW5&I+1-Q$6{Z2`y8tb|(Yl3pLSYMS({AV82j*a-2IRU!eWbCggOhA1}8XD-ks=xheiNb%y@l-?v?7*7Eb1`aYKWdgyuU%i)b&Is|zk8RpIRG-BU+{muShK3)CfFx8+&%lu>S#4Qcaz^;J5Kx`Vg8@V_%Ho%si;T|R&!a_*v-VKVXQCP`L*l2+D60vEozmja&7&?HLcH5;lD!WQZVK-4|uF%f3C8IiX*CT;66E_j@)|Zc#;}}y)5&wi2DoFDwT!<C`$@LdXOi)92a4M<!F_2%$YFkwd=in-V~H(#p8Ht6mp{WAt_)x@z410E;;`i;(rzM{uKyuT8ZFo%#WRykq4HN0~R4*<08gh8>InceXqoL$p?fN_HMA%OFL?YCDaZ};ZH2NE}H}ENh8tEv?D$t{s;BxiHJR(uY|clZQq6M_}*$?5RWDD?y!mQ7X0sbLH+aaU!IHHFi#}z-h^P6mDsX)2CPO8Lg&_>qRpotV&drGIFb^BBgxUohzY{s{ay%V9^~t2qqtV|iKEqERVU#k>2n#`{%venI2nn%HmbG5M`bKOYPX8_h2c9iJLrvK>GMCl7v(AYaXu~dDF>7%2cj%--_v)E3;w+vY%#}T1bTL9kLhEFB3$NJd2K`h^Bgz($yh$FyBaGIPHd`?<XKB!kj$HqK49Vc*6;le`^evSOS7r{vvEh}pfb0Vn0_rgQrQie?=S0q1pnlLs+<UNLKJmDwBm`n6Ui#}E9_q=cslR)5wTcNkbr~a?j`f4p?{CAm~CPH+Vu_R{l2?@SsVYaqunL0Zj}&Mr!=c>GT;9x?s-l9IUWCu|2Jh0vgrSOh5x^w&A`KxDX2&e#zCJgSTcVq`u8@&Y~ty)>+8K)Ti+ye6=c4mTDw%Dwbf;Ez`1PI2dV6Qo-)DzH`M;$l;tR1tyA-|53A>Lnf!Yp{Qxecg((gwO$dZx|8{t<Ta20Ghrz5<TTB}><hAPE>fPVMb(J-WlOs?N7lN#Ven{Hqj%XR@r}h{9AI982+-()PfI5J9IiUIiRv_3(+m}Q3=-8*fzdK-)omz7qpw^r(QF<ZBiTS;=^sB8@YyUl|k!6kX=mBOb=axQzQ9}p366UOibi-Pn-!f+;T*(1$w&R&|OhLkaXWXy3)-3*Sl^;Rw!JSB=#*X%0kKG&Wsk1FGq}NwyP5ifR@i)w#YJoySysA@XY%mgncB=eD-e2xbjpJlJUiDK5A5H1{z8cez@y}?_waDD(gaXF=!pL3J0zNn@eg9G3I4-*X&>n^F@|0jy$om5s_k{xl|E1)GQs)1X1Eewk-{okp;=Wm@HW=2o2X;HJLqebzqINqeeiXeYoG8y)_+Fm3GHO3#KdJPSbR*Y~@6#Fik^Z<NbJD5RYmPDY)6bOFe~SMoh5wsJVsM+~Z7B;5DXT3?Q0wI$(|46Wt3J15YJJV4$~;`mJA`OIXU3AL=+mt;Mh)x(Ul+$$uJ5ElrYOr#VC)y{UnI7v|7*|b*e_`w`*+KA?3ZiU7rTC3!~cbRd{>@D%fzG8skq3zCVGzxmRnn)PcIYtUw`<zu7CCV4lqGkMkMQ2_H6B84eeTqravxg<CfnM?;8G(JmUc7QZhy$_?LcF$+1*_Yt}(rBJM9R_FtmOSY;-2^W7Vls@&g{eles^ckFU{b-yt0(E$;==&y$Ykr%xe8DTq-xYrd?#6^UMgUSI8%GlrbHHh4?77?;Gm*-?mTImC7gD`4?V117s!Fu2}4foQ273#7aq2vwuKEze_P*@HhYK09pQ!r!n0Cey0IX-&tEqwUypD<;-`47U}gs(fmdUQ{$oH7ub<edQ4qv2{hhWMY1xV=uOe-8eung8YymubXlyzgcNvwqIhBy%l>G1j(I_!pZvb|m6L_aHAZ0_jov6(<SL8v7|lYuSx7!F2O}ST}DXc9GW(k|$I5Y(;*U2RXnKN2vLas=Wlfm6cHYt6X2+9nP{mB~Z-|EQ$9=iM&fh*8PzKvO+wtZ`%qio<1JEyLLdYuAShzVYS)^L2{)SKUbA&$hhgMiM>_7kIj(ws%<1^622eT>0Jz=*6Z`>o3NQU7!~xPYT~#?)~2d;Kk+*LBXBF9SU-9Qcf<<gQOmMc%A&)#SDb|Vrwn*pPQMi&rk2+5U#Rl$$Hf2D+*l;<_kg4AObqDW1^vvvg4@P5uUy|{v&NyOvOtUZb%K57)wfDD{8t^<w12g}Hn6r_vpS>kH?_p!?UTfRk%m3i@w;=G${tr_;`;H!NTIK9VE#Xd+}fWwa^JG{)$2Qb6lzM-Sx46I&Ka$~wP&@@(g&$Hph)8Yy$%0fDaIT?#s*Hy+^QtprS`9u{+K*{+$Ed&nD9VGIL@aYU{1M*adADC&YFa;yLLnm`i0B-Rj*v{*`xX*CCHoca1XK%?nY|h4kVBl5AEE*Smubx?Q0e8qjoq_6Kp_?81+W%uFa~CP>m6KIpW|B*@uO?fc_s$J_r-N;JQj_gb=rtN@s`iSo#g*9iYyOtgvXxNQ~;!Rn1fV+dF^22Y>!=m=ErU*kIomV$OQ#ml$r=4weJEsxk2u(*|Sfg0YGpT;)B0E2koEALD;@^Z2hhe+&iDdyqo>C;D&W-?KsO0pn;r6;np`hgru~_~PRaRDYoPkp8eF4>&n2L8Pyn;@Dm5?O`{01ocaIj33ksbH@&X<9xyY0z^|UrTA|_W}pl5$N@)~_vcIgPwjt<n&Vj1PQ?L|1Dra%Pn9Q#ceU@ZyhD_we4oePM0#$(2D=#;-n$#VYWD@EPaKP=z}?7CJgC|xZl4GB)JnA<km#cgGl#)pazCY!Wc)<>dq=l_3l=?FF-Kg6tD@_P>uTcqHuKBd`G*wl74{k9Yj|F;e~)qgezDB$NhR*nls%H1Rq#)rRrpsXnpQMyj#@vIO+0O<mK;0iYnXLzi&>WA5gqiy>ucG!J)#(c9?9BW!H}{tP5WOXhN_D6+@E&4^!wPCoWJ(8hW%?g_Alf$v3xXKI)dsm={TAi0&nLvm@sM(dQh{>nQn;~`rZ$&Z=ZH*9{i&Vx!RbctW%OcNU4|LU-XLfQCuo|wszUbqrw4av+(eAI_{rH(Kw(u5!EuzB4e%O0qL8O@z~2*k<<X8^jkM<SvDUd`}M$A?OVa>75!p-_b=eKay~Lbb|5Fr2U%g>NZId>q&?dZ$2|5BaS^v`6S-i$ij#-^wjsga83}wg3I2?o#LgjpAMHb~;I$Fti*V`!nX{tdf2GP7gcAa(3j($-R^tV-$DiAh8CX5T95Y9LjR9Y^Myn6s!C&8c6Mvx%=w*sgL;7LU3R^_%c2$`3akRrii@_LZ+JV*{!%f>@${-W!?tXBbH4@vb$HK){*Z(W0A^rvUza{ZM$`2`f`L_uEcWzYu8{Q7qSi8UyR-*=}bv0iwH+gCw{RN+Y_&!>G_#RsR?LB<i>Jtp;)*h1v_fYc(o$ahtKS!LmBT{|0AS1vDSwXJK@`7D;J>Y>N>JG8vf_KLH;#fa5K3Ej#t?*wgIYO*2jz#R`b9Ti}$3>XMT<^;^pP_T>&#-Xj6c}PdnI}aljdVEh89&N9)Ld*Vux{!=*bVE18QtE4rRm3TUOWRwBX^_H5T@q(R1@pfM~VLe!F{Y61E^t~uVw71rT^8R*4D(xJXsmH7VK;I&(iQuYvAXG@?6!IC2Q&Az9$atQ~PDf9#batOEKHoR!ENxd+GX4>D~ff>ldMtI=D_SBKVP*s`CC44f{8WpV@7GS99tG*w581U$g6lcyj$1?qAHu*(0%t2ynyVg;p?QEHyLf2>S)IkbdZg)b|s3t+hraV|5*Itnh#5w9Z-OO*lZW)0JcY!~uf;hO#U?rUnp`JV53r3m??cWS(-}ac%D9mE0H{r3MIaU5mMvqtJzM;mfw4W8ti+Kgcg8b^8bo)U44vw<498&kFWJX3!2K?{z_<?^YaUd{6Y-O53I^fmlf*?uC0&_PJB*ZAT*8F5X}EciXHuAkurirUSOGCjM9AfZ_n@Ctyw>*q8l3R?mg!@)>Zmn}iLsM`P)vA;hsM26p)Z9a_AvaloI%-e_Xlxt4!@3txWrSM=-r87%sJiFu=XV%d~I*i5bCY%@;bU-sDBv3#29|9>9-tIixj_JLjGIVZ($(O&CRek1b-cCMB2iD_6kX*k9W?2f)B9q7lOqeIJ&(2@34n-=KRxiyA%??mi(rw$l~O$#m6I7g_{Qp7R;H?ZBZ82>Z(J0m;D4LQN?$P0C+7SK3A`UAuY<z2zVy<lCeh&&)1a8hxDuj-?W^4dfVFcTyCnyLQDFWa_M9FP<li~@PqF+tDrB-URzU;08;j_rndgF0aCJPT^ItvC{~8|SDYuS;LvF^T`geSWmUz0vBV|4DpL@Xzua9sfoX{EIy-HRvYe*lBn`y>N%Qg!E|`$XV_y7h=NT-U|QHM`bs6I+7zp{$+jV5AK9b_Eu`onKRkZs6CrQtQKpyzp2Om8)C<wS&feWrr4KxK$i=e!TwP-7T9>J7>}+M;s*6hep(oIx~+xf_~8owJxn_*4oHrAnfi8QeWxLfaq~=OIBKaWRDDIO)am^Yf`8#CBPU(c=Pwx7D;l+w%uhTgmP5-`V}oK3WsRcjRixG@Yh!~I=?9S(=n0qA))+nTYsCSg0~XGj_ELT^cVK%gnraT8jZ4*h{$%R?bmBTAa61h9-H^)InMBMd6Y~-yB@U$oxFRjsgZY8GDwBC_67heSe4+49jUasn2bcqh?w7to;eb%aRauiOWApMZzn#lxVf&(~)FtB;ZXKo%hyD0|m@&i@BdO_owEGwxK6zhZ{ge0ph)>@A13s7iR>=*ftv|xRuRh0wzMV09L{BW4JP;e^j%Mr^+)sduy#<_XMk)LsCjRSgU2PWs*H7mnE8GY1#J=P+Q63IBD4Iq3C$}wCYgSyB&cd3x7O<H-5>_Mo!D3KPjOk~Jas5p(c}Pz+XJ|2VfK3Y~Vdtv32;ORsDDqyyuJtgG6Ajcy8Em`E02gEjx+=?|7RU|W{)_`;J%HLT*y||=oQRe+f_qVx7=RL%nIRqsa9*YQ0S3@FJGE+oPG7W8xqzqB1_b%IBOzoL4()MQ{iX6Qb%#0QVLxRAmd>1j-J8fseoiQg-HXcfaJ8S$&B8cpea%D%+&h{4)D-?@O%LOc;J;4x#4*~5BxUtdN=y>1N-n8Ac9@zX7FVfHOXBuW3vMKbPNzOKgK5Xs%H~d;2yd572;RlG(X_tg&9!qUV9B&mYTwF0kM&58@I^&#tipf8HNF4tnqXS?7%I@PeM9GhDx*<f-`2<aFEzzJHUA~r)dKAv#9!mB6KG&gUU$6+mHfMsqW!RCojpd6=&v|HZ0?LH<b^E=-tEzReXUsE1yfkxbx05QW_=@w{|q!pooY)pE_zUb#(QNN{*8QP<NzZNNGW_Jb*s|HHJ{E~*4H`eYaYIpv4S&MYTQ=lm&x2k2b<{_+S^pu0WC3;IhUvN#%BFu&FpconKTRwEX}cL@oeU?OBk;=Ai>uO>5S(Y)By&;Iq{!LzZEVp@LUG*E;)i&dYBi|qzv(<25?6rxgp+f3u39EBN+R`8An4I_od%Y`TzsBETQJN(cbAt{Ch5$reeQ~^+ar)Hx?VI0oF_#ibWQEvB10+EC+PK$nI^4^Dof5BV%p*7U<LIGYmFq1@rFhFlhkezIk^nC-xm?4ab)GV^oggM(jJ=kJs?O*iz~LdpDcM|K-wbqy=qf`#B<tb(TI3!T$j@wy^?XGMA9`_9YK^uAYx=i>#R6Pr~Y1<FInZXsnwv0UMd`%RWZC*{;FVSP||k5%04eX>1EI;ed3(|31+H!U1l|a;OKSPoO|)08boMYkf8ROCE6YkRP!hh|`Jt)&7A+F@8v7&b4dfV%SX^i=n-`D*Sil^Op78!~1`Yxl_hs@oY=j@cU__2EuaW04(6St;^>l$ioro!8=gKdtGNPc|-I+b!4^b2Z&SUJ>uDD8vfHXE}-VTcRWE^EpaZT%=@p^?KZV&^%3bmkg<Yj+@hwqlpcoThy2w(yBlq1V8XCIN(1!#sw4V!{|ciA^})hv<FRbcWY}Ac!}QSuF_rnZoz-}_uCY;jN*rY#A$wrmmmK3_0UC*eM#k^POJYZ!;r}18f9-kLKcbmnt@;!iZ<i>0eEm3TczqdjjYHHNo7XLZ#n_?5e<$=Z>!QX5#|#^Qh0`stY~EDZ&zOMeV+UjE*def+IT5bR&7v4PkMen4AqU<U9ba`^@k`?!`FBpKQuR^Z)W-mn6=-#loIr9ImEWAwc;ePcZNH;x{j=<8c12sm*hq~a98gakAbSQKkK3>ITV7*59p?ReDh@DVTlX^QjN$!yVUFb(ESfn9)>Fn{@`!#IJNRo%89f+FW>13KiUrv3yqYnP{z_e+PAsPj-uXF=F+FXcoTCM5_|K%q$)F!)>iAC~ev$*-RX;$S^aFay-ppFguk`+w#cH0={>}DE_Y3~LsX^2`eFXmtW&i7O*gSU(HqA1}`WeHqddgrdp4blyNB6?qkv%YdXg5r!E|@;V1hYo;z<la`;fYn#hG4_Y5!k$7G@RspAU4GP!ZG^i(ePM2N$dZ6PW-=EoJHPpLF5hxgj27`IOhTClyKHPoSH@J?_8_K@?vPw<g|ls%W=?kIj>uekZp@`z-fuHNa9}R5y`lJvfpN;sMt^2vrXYYooy-kfSfaOK)%Wa$N`N1f`5$zym6cyaFW=Oxk0BB_Tx-ykm?^Si10z&?#=Msumr29Ni2u=QDXv~+kB=tze~H%(WU(t=tYc;8qf>#C$mkL&c!}g2c$BuJ4r6Nm=&S+@~l$$kH@W}S|7mo@K4N3u6d6pegF6PeQjZ!$`iz@7~^i_$h-i}uFD*Otb;rkLHtK4JyBZ}ullP_9@>M$^o{KfHn3)WBt{7jh!&9XOey;?&W;_}iyDg>Zs|Pcjq8vTwi6YGFw~q##UtYB$@y$zKM#!;^Az?QFCO`a*sst35sh%QP{a9!9O`M}|59!f>>ty|4URv>|C8#Ic*NuC^ErwG5~KXFgSmk9+{qX=s5i{m#%5iN)*gL(biugc{a{1vZ(C!FuwC1bL$0bIuhvleKcYT)!g@%4A#uO)PML;(BR^f$wTp1T4SlTwuTdu0H;xy`+y&Y9(8vdB4WovC*?&>y3O``}CHoT}jq#`M-h{17=VRu$VdziZlKy}%+laNKkF;c4x51#ET`}2wAnd6lo!Hj0e{YP}X69>d$O>{tW}w)1<^itEUAHUTXNGw4d3Yn|;7;To*oho+WYz(1q=$Mc4oH@MKx)gFow7#ALB&nkKQCC`@v+Gs0UPYFkJ$H@`F{>`75+V_{oU*>)jK_1tS7*UIlxxN@GW!4sBz1Uvl-Lpno}E$R<>;c&(9~$<=wyhteq#;Re%3Db>7ux0-UYK!g--wJDmB&WEKDGZa0trb497tMcasJnb)vdae&la;=gc#=$I(#I$3`s91!8YTJb`-^KxFdlt%263nb=8d#uJ`{{4x*o5-8nXijWXYJb^Nl*cK|jZ%CC|J$eoBoA;$PKc}n)HxvR83&w*^;4!eAW`FhQ!J&AP{t6Fc%PtcE8x0(4wlcdz#NO=n9NvjL7%iVAAot5qDiO0-N7EA)OD#ro;aBljPu!%#69sYI-R(_Nvl$G`3~t8kD6n;5%Zeydl~=Fi^7fUa9qteKnugA)IeN*YQeZ_2*&lSa9$UoG{XIpiFm-g;3n^TJ|mpI<D>TNcUryxOBp|9zM2)|{Y=JC+bN^ruwW{@9c&Tdu>rXuUZ`L`E_(~qm!+eDxp3o!oTvEzPGY~9U`MP<*UYzzHT=tWmD##A!N0<O(f92pV>EyJgc?9NpyqNR&X;8%hdEMM;10NKT8?FQvoKdQip5CGqGq<6JrV1c%z^h-`iZ{_a+3m4A!8_v$Mw`HlB+b5msH%BdB>6mRG)mNO~gd&U)A-C!u%P{>h$qK88?*AiW5%jIf2%f*>Embts!{C9H3tIZe<>DIVT1u4+kPSz#aZuR>F~WUP$dXb@U)u4DCz()fZM{hQMa(7_7CiQfsmUT^RRwQ=bNUs5Z|PoHMp(F{Y<8r_W}*&t~44sm2Ao)m)hz<~S1j)A^l&KAg<>nM7ZgxD@5-sK&hoKTq+$S>8QihrJT}*Uo<$|Gk$@*YIzr<DWXf#hTikrC{1cm1BtevAo6tF18bx+iRs<D_UR0dbwu4xy}ot6o+h{C-2D^L5{IRT)^}3Uw$MJvA$anLJWsf_Y3~T4vH4=SgSbT>A0sF16@s9qj7+XW)ajnv0m$_eHizN@l?io={GS@>nq(PpG60-jf4YI{WK29(m5bs)&q($2Pg>lRC9)p$$mdEewrQ9Ot?VC2aBS7aFo{>0%TqG2DJ{$Z}SrMZV7ijQ@7PN*tv;q@4jBm-^`8lL#fOg%ZbDd`e==4_~VHR``2@|apjx&QPk|z?Zmj6>qEPlt9`D@BWGkY-e(<9*uR_}f=YS+dE#DF#Q7?#BsU5EWuBnIKjVLWNunChu0JjN-lS5e#xkD?Mi%3`j2j0!uY&)krSRRj1Oeo-NH6C50Uqjaz9?~@^{X#QM#I@mG@jFAe^dMm?ynoMujBuktouExVO?Qg<|fmw=4-t0kJ#6(k@qrkz+;wm)Z*8vFUn4&C?1fUAjEeY_L9H+sS5(VHX@qwGM(DygzW1|y;MV<sb_r}q+dZa|D96JZkK2pUi$s2^n71%f2rUZA3eo=Qx4Eg#yiAh{u1+^?=EU%F4F%hV*?Lp!U46+(PVv)yo2Cqv_BH|xFX1H9eg$}!**&EXXb#etF4(oE=3UaU@SFDn&3WS7h}#Y6h`^tC_m>i4#_?<(g%=3jOT=s146y@xbKZ@ns7mS&<=(FMA7}!ywSu<Byn?q+Br<-_{iKJ>VBpFWiH?b#{YF1{(aX_11z7R`T#r@O;K@Lbby-;b18YpH{-Z6`-yNTM{Hj*S?vwJoqFEQev;w<7i*0JL>D*__rd{NnFok%o<CA)0FUKU5l<egt8O0uWk-^judG*N6CuvR0kXHj8inyl(fh=GjJG3Vz1OSwAGu?-GKu#Gw~KCC%fEdC5@^X1@1<{#?I->9VmYB~$6z<MrL)ojse3k47i?9Q9W4Cnid>2R2ezwzfWin*Wku>e!m|IL%mdI&_5wb6XqVa-RK^Bne4vom<&rzH`R`-}yAxYGl;z3z0dt3PnJ*@LdNGFIE>6H5TI~sWN5>&lQK#R`({%g|mbZ*{BuZ&`v8vpI%<cJoRv2!`e&rb<xR@G%^NIebh~G`@`=F9#WxO9Xz&>^Dox&*87009DOez{HGKkr1#w*6Ea(Qn6eUuz?F+CKg5AVUrnB6E<?@-^1^W=}K<d5pYSZaqPaz`4ToYj8cSeZrK=YBtjH{$-5nCSVVnh6J7lRbcl|I0br=WD$F7V&?pNH_kbTE-KOX(r#ZY`o35eYXS+_s-zagL2f>p2971!<F+{s5o&LrQ|}fGPccm^4rz&G*n+diu=_k@VNFgp4>Uf-=BJF-`y+KO*A~~cw7IsR6TV)QpfKfoKby*nhE~P^m$8qUx3my`KmphoK08jmmZ%ns5X-Fo3b>vZw7OLG_^0}4RY-T=47W515p_3kF0Pn`h}Ou-?ND2eB%9BtS?HFm{%qR;1u<x%ttMb_9YMaAU|C8obf;&eKJq{iSZ~u+y^-a<hRWKL)_`>Zb)KWOOSCtA4kOOkokbb2jgM5tLWop2w`3v%p5t08esoM8=5utI12vfz<2c=wNA%-g%$Jk$?zh^chKC3c@KMezm+9Cmht@ZDexqYy;fSmd(}*AUp58qyxvvxfbDoV+e*J5@xMUV0Q@XtfUd;9=hA6NU{3Sk_VwrD|1kMM<}Ss^{08PQaeG`4&wNHq#tvkTgN$#=T*!nyPDtG2OmjjKze|&T5^AHY@SQjky$46-9Sm{%Q4}Aj>{!emwzIz)b1R_b@j9^rYT=^?ccYl!7stxHpM5wP&*vcf1EvHiD@zVWxx7n8)&>yAqK9SeM`eb54pq-r+n+xMHza>AO2l2+qw7?b+PC@}eJ_4(zs_TLEbqFIe&aIPr#n;S_;;w!Yl@Pnv$Z+>b*Hp(W^J4=opB{iS)KF&7HNHjq7g(3+{ih=c(2(l*5fX}dw4pHxX;k~sU$wDG5IoW{;Q15-O&5Ft{CI=F)c1ipI1$(er>5Xm#0RzS{)1bsEHq(eTI`qm+)N!t)8FjD|uY0mFigRJkQbU`I|=<c<tq9_T<X5&-Le?nH+z2lU75!-^6OP^YXhV*ELhu-D$>de&3#4)lBX!^|;S}=N{YRA<IV>dCd*|{!LAOM|*f#oAapVKIrp@rEO)-farni`rd}u_1xe(+wpR-Hou6a?794RewKY$WpDQSvK+PNp!iAci8RK9RO-Ma#)m{)%!@@uMg-2L9l+`25OPC^$|p|5?^o|8Dv@^-#Rs8?J}&Rn&Y>S?F#k7DE2I(!GB-fx21y^Fl(C`-c5YJfHHO+a(%VtZ3k=o!1%tLO!TznXeo)p8&xil|h45Rq5WCg8Kgkj6=c{*u@7ZLd-XXAetGqAVUcEO+))9J>KV%<q&t)^zJ3}O=@LD0Cr(p+6PugDcP3nQ&cyOnA{NJxSk285mD9JvAvb;o;7o?)RFb(IA8Bkf2j*21!&L2-l<*_telz00TrsF)%T{@nLt0h^uTAGWh)A^{mP=wkGCsB8~gc|S+9$Y((`YW`nrMSmuaF5^Ltt?_mZF%v8vii#>@y)eTd@FY23>t2gGyYVlcjBo1?rSPFtGlG_fBk^Icc0~bmJey)&~Lsq`i|^xT1R_GdrWJfH8%4%H|pOt=-1XaF*$yufA^2Sd!m0Q_CWu)n%cwXzhm`B`(I{8ANW>(Zo+SO=}WiiS68)lmUpzhmK$r%D<5pQCGR>Z$3x}UCAdpJuC6FRRari+pUTEn+7)>RF0mqZ{#Xhsj;0V#$*3quL|Oh}oX&~EiHv9z8zON$`2Y$N!_*#wM-GP|H)cPwqUC)<e#+9Ls9B-|)cZ$r7*BI~O*XkF>(G9rNAH6nat~6eVUj|2GQNA!b|5iWw;)gLILL$7_@b;J4ULcQ|M=X0v>)w%Eg$LerSTiu!)Nw{v8Ivsgr6H|kL%F*untci)~Y?d9@pJM{hgb5boVC98>p|jL9Krc4{ENXj%EFwDl~rcBldr^AMHo`(SEca?f-Z4coXWspWxYlL-pf{`te-de)T);*!Y_l{nzxjP5(Q*@NenynwdpU?3Fgcr?ug9TwBwBzQY<*v6Mf)X%LJ2$rDrO)cqzAZ|pOfWwGj?u8#`))Ku*JzyB_M=$W2|v;Y1(vq|5zHJBXwX}9seXy5Gp<@)0HhZxTAH*&q>Q9qMY<*f|M8#*_8fB8-BAKx@J)@S66*wnH8Voa|5;-Bl|9~Rb4esk*mA*ROr$Nl!5v;>FN2D?rjn!R4`U%~4JvHs^+p8k1f(=G3QYqGRWE7RuMSnkjEG)vIib4tc6)0|a1(w4`%b!+~qXzeL~Lp1B(_eQMAl72C%&DCG-Ep?Li?0crCsnn<Z=ba6n?|z%QsM-3gQP<18v-JCWr|wTJd}Ci~bI(hz6_dZWzkdG--?cRzaOkIL7R^2{?Kk@KGL!2=e)~?hSclf8%bRIW?RjbbmY}tf+2o8_hEuC{bhB-?eb%V!i?!#a?*H{sztocQR;DYO@dxdBsr#G7>-XO^u~)ZYKCMm5oAqa`Y3l#-%sfA8XIR~$UE1P-Z4Av{uRX5>TF_6P7~<&v)%5?F7OM=;^EYXq(|l%?w4*VVCiaV386FPnY<Ql($@4X%ze%~Sv7=#XsgFs!$IaG9wW&N`V||?H|Dp8%OTYN%`WW#j{%@?0)53<-H$G@%D0si4;d%HHe=yPO;~Cb@G+<DhZU?TmHari%M*kQ4$ictc*+*7}Njv@0Uc@H_{2QeHo9X|4^#A7SZ}d;8#~Avj)PHAKJHy^VZPG%Ut&jE_X`ej({%Q38)Aax5o|h3{^7oF_@4vdSqv@1VpVUsxw!elidA?^g|4g5}u)*Z_AG9$&(oB0c@lWOd;&W{r{L|PzrX!neAMHQ!|8x5NLt-jZ7cFXKTHlQSG{Kjd@%|nkwKHANqFuLT1Dmh^595E$|9d@r%h3MJPt%7#e9LsqjddocYE~JF(i~G?<o_?j=S}^e*QWp5xZ$Q(I(0If^lfFx>h$yG+Gma2zd!5IR<BRGxkKul$Eyr)9dt;2k^jF8{~P`PqVT?6pF5ddOz#)AG%QZMl=`pyU)pmj`9GKZzv8+4rnTo6tba$AXU!c<r2hBA9ZX*2|KbZj8#ml=BeRR)Th?b@;w6*j+DERJ_UWkKf8~vJsi*n8_M|zQJkS5-?>(d6|7NF7riE<(s7^oc_9Fj(d4Bt$d<*<AzIy8a>i!>-e=5j7me1v%XV?Gh=cV)i$a>ScEq`NJ((*T^kyvGFaWTfEp>6zg{r_e4gVFz+`dcmi?E#<fl4ku)^M9$&Jk~|(6Fg(3>BlPyQ^(1~(dYPo)A(+|_&$*FePT25y{Z3;{f70qOn*4#+sbrDvv_>w|Hk;coAGxz<L}uQ=}FdqHrsOyP3qrvMWM-nb4Abf|EB(Vi}g81|BP<dKQ;fC`&+YZCh-1Id|sn2#-u)M8~;N8r>~hcj2&*c&G!GMu%+q1X7RP=X}mDTM=!?5{fv*{&BUXo`sC&OQ}h3qjYm!W|K-|W^XH-D2lBbuD3)Jq{9wBG#eDu$kM*V=BcGeDd;y;~^&j%NX(|23^96ivMxN@d*XOauho*mf0iXXc{i(&5b9#Nq=Z1PdU-K99`BS}0J#Q$bUbTAxpBvk=E&q;(T0AoS{Gh`N{n_Ym)%3SVtk0kq@VT*l<o=5pf8}`vzJSm5c&hGCycmcV)6f_5`Ag}IfAHs*&>NaRzpP&R$9zHV-<Eobcri79F`vKW^V0l3X=J@&sm`C|b3-rk!(ZDypU+=5|7xmdUe4b%|Cjnq*5eEL(C}ySq5TW^ylH-I$^5z-^XmyO;PaQ)SDODzeHO7U!p}jh&u5G;vtPi^FYj-fKO6sExxci(3FGOX+Qq-npN;ybitX9J_&EOs{M<BO@L|5NkNHB_i~0E_{Y~@dm(ACj`t!@_pEah&o}hnIy+1$Y|3BlWpRxRR{{M4+`Z>%0!|#8A|DefrKfy2g>6hxdpW#3G=|9zV|AAlO|I*~TU*Ny^>A%!<zr?Tk>DTJI-{80S9ql)GgXNp}Jxz{(kN?IWXmb1p{>U?bWceqSZ}H4q>iA8(%`4tk$A7|~dFIb7-(mSK&%CRS-@<#m;yrczKK_Eg(%#1hEdPcNX>$A_KElT|IsO2j@XRMHTd@3;XFgTOf5T_I;xl#J0xfx_CCkrQe!(+esN+x3idVE!$F0!@ZE3C1j%9mvpviFubVMhb9JfPfp6Sf;OO{=Dri(gmkFR*eSL*mnbmf_@EKOK;<C$*ixC{TLuV`{?3Nv)4nW6{Fp6ErB<6ih0y=ijX1ATa=56iwR`|(Uab=(vEc}0J9+!q6QW&q29EC=z-Aa&digL%bZbvy(^F^o0@!&#2NNSYjvggO1iTpbU`D4rR`ax}{^JTpcekHA=7F;*Rq#yFlC$8tQ&2|P1F9gl$pudq<Z{|~csD#Z"""


def get_embedded_sorpresa_r3d_bytes():
    return zlib.decompress(
        base64.b85decode(
            EMBEDDED_SORPRESA_R3D_B85.encode("ascii")
        )
    )


def materialize_embedded_sorpresa_r3d(output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    target = output_dir / "_embedded_Sorpresa.r3d"
    target.write_bytes(
        get_embedded_sorpresa_r3d_bytes()
    )
    return target




# Built-in original ToonCar cone assets.
EMBEDDED_CONO_R3D_B85 = """c-rllUrbY19LIl`V*fx<D5X^95-luqxOFOVlnUq9V9|wWQj9XhEP!qqH4Kc|3_+aTa0!NG4`wlQn&HDp+*~w9mkmenxt59BvanH9V6y36%$&Gp11CI4_q%Pnb-jJ)+a9>*<oj)J?&q9*@9FQ{CPx5(-EFOH_Fb>;h5xB7kCWWYq;NQ_Y5|KXK-B`4x(C$#uS(tjs?`0jO5Oje)cvn&AqgOdB#?YXUIA8l1^Ih{e52ee-0o=><nscb!z;wj5!|89s;zT3+^&Zsf$5cwD|~p&-92(O;P|fe3dF|7oiqH?TDQA=J8cs9F8K%R?06}wj0Z7Pk%(36a`F22_ZXZS!wBzixO_8BtnT$n?CV{IiTv4<23*w_Vo*NHBYb<AYu^jY_~fo3Hu%kI1%I{pKKt<u@h#h>Se}<ko4LhsnYDb3q9V^96RhN?NS^5Ys~uA3g%8B8NBwN(!zP}{e>61326Vs5d7}AWE0Y?!ZnFA%hJWqUDEO0m3}_UH*Uur`yTj!?^QI~+Syzbjx8}+d`E5N$e0fODb0Erh2T?k6fo&Wu!X4Rm#q-wHYO#FF?+hCE^9T>uF!<(#STkFMUwma#@aK+Av-35?&(88#p1@f*f5lyhrpX3>BLDSoci1!X@6~=F%6o3|r;E>v!J{j%_474~=e@jbn$<pkmqGXbXntvuw^v?gYu;Lc505^n;G5QI@s|yEQ(hli0I_&J<b-h#`qtW9GT+c)$Aia8@iSAN;`i;DtH3Go5W@0NcDtVVy*502aEi?zci@+6(-rIUM28oLekML{{W-0F6|H|=lJyU??7@N~p7p8ne;)tTAK~NjyiIw+lxHAGo=N*fe<Y7*SfqM$fmDdv$KMevA2VX8qguiH!{;hC-y%M)9!9Aiwo*OxC#i@0fzuUz<2CEyeGivPqU$-HIU|nf8u7gcpD6b4i<T}n#%cbz`WdA9X{P$gO;SHc^oQ8D1rMBXDkwH)%K58!?MfrxP;J2BeMO3T=sa<QJ?f?TQ>?ec6W5q8OYejqhMAN>^Jj137w_+8&Uc?f-Lu6C{wbFkyXE;)tk16eJZv**obp>Vjjq!`L#}~z@^n~4t_2;rj_wQOdU8EcY2*fS1F;(N4Dt*bkxrgTo=IaDk!#7dG~NhVu$X)?EP-r#Yn#YTkW1YJW^yy+Q8$By+yeR3Enp?L!cyv18oiWcBqE1C%`_^9+)9+0uC0;(fH!u;p2~;+D%tgaTYqnAXV8BG*^81g"""
EMBEDDED_CONOPATAS_R3D_B85 = """c-ri}2UrwMw=LWtDj5X9fPy435G9Df)E*E8i7FY1CNN_H6)}K0BbZQ3U`}Adz*LzqXI}$m%sJ=y@17UV8D1C;{@?f9`<(l=JZo1?_v+o%yQ_C~&vY#nIF9QzWbhDof9sxulDPjX|0a~9MfvB3h~K||3;17*2*2cisZR(o6wbc@;rt5_&i_Ag>d1TU(SsjCod3hl|E~29x&DzeITI5Tu2ZKNZsw?6LahHo&i{w^zqmf3lPhsrv}nOCo^(Wr_kY0o=db<h{sjgm3h@@szkvVH{J&)KQ6b*K{hxsUpy$5}r#=bKf5P`a0)+D~Ksf&bg!3=p|FQGGZA@1I|I6{)n-d^FfB*pk1PBlyK!5-N0t5&UAV7cs0RjXF5FkK+0O9x3|McIEg?A_f2oNAZfB*pk1PBlyK!5-N0t5&UAV7cs0RjXF5FkK+fdBQ#nmtg!e=vl1pacjIAV7cs0RjXF5FkK+009C72oNAZfB*pk1PJ(l2f}Zl|KY#?3hzJ&5FkK+009C72oNAZfB*pk1PBlyK!5-N0t5&UAV7cs0mA%$0mA%$0RjXF5FkK+009C72oNAZfB*pk1PBlyK!5-N0t5*2{{;y1{{;vTAV7cs0RjXF5FkK+009C72oNAZfB*pk1PBlyK!5-N!u)>$!u)>$0t5&UAV7cs0RjXF5FkK+009C72oNAZfB*pk1PBnoDRQ#^CIDCC=l_5IvM0#;t3Y+1EC2g?wD$WjK>Yqc`0xM5KHvC`QutTTMT@@43iH;EROmWul!8Gu>-in<ePUijvVwO1WCi~7FGMTtUkYEEzEW_*1#!(^*Qy;A+w%Y1{;|bH4|TCa&S}ZUC|x=&+!`KebYNXQUW@+e!|%>`a4W7)R&JjOUdRvm`V3xtjVHa5J>X_;vnu_jbdgxfH@G{G4MP6>4KH9tNHf+u;V3aUX<LQgWavb~^PdE2AK>}Jd+5@1r$pwhaga>j0Q8^yAv$$jc>bx=S!^Qm{hM<Kly>IOMvD8$i`;njPk!#oG^zi0CDR}h`Tag}2i9lYASRIn+D6r>!XNGh()I^`Sp`GB9wh@YK0|do!AS>GxIX=>+duhFm-vAFz!7BJ8Y3xxN#R6D+OUIkzGMb(w5_WguUCP&l<yO1$@(ClZApi96H95~S2O5+*rW=7x7}{S^ONguWWA8T8{;!;izl1k$rSu`<0Mu5Zw`_2*<l+NkNmw?ma$(Jg>-k8F|;nSt8%>F8+J<hs&37IZ%^sPT(-dZGD%4{hARs!s_?a6m`U5eI?ES=2B?bzFg_D^q`+duiR6p#tm1$2mFC)j=@eslVOR&szIGE6<OhtghN6Y-VV0I&mGis6%LtsmM#B)T(d0zK?&3_<tt2h^1>tg>;kKhvmH3czT9B(91C5sbCZ}$0gs=5{!MFbp^38k_nYJUi3jfYDLpU?)K1toB&cqp;VQ;A~j9Kx8rrJy*R_8*h@UQkeLS8?9K$^MEVw;Te;W);>X;BJGZ!nP<=eSfk-u=0gNlNZ5(t1rp)@J8o(49Di>2Cc*r(8%SK3&7A#Pj8~1LR1-CGup$FA{Kd6~u&0X8L9Chz3g~WBP<w;cppa2-{cPAYpbH5cqN(#9%z*G5#Ad{;5r8S7}dji5qNsewWN|e2AP`qbAM_4`XRh-;wSIQi*b}s{U`TXawW0w}8>Db=lf)-Nf(kc#~8<(Dur4FjQ?*mHd#Magf}>^R?-~5^Am2T`VfxM!XDX(X~y~;jdJmD)BtjxQx8T^JNt_ob<TUP2B!l6V~KSFJd&?36yNxRB4Y^nijOY7Y*l@Eu#|T-)Y%|&1<F3Qhiil&@i(q{Ej2;l9|Y76B@DbwQ=IS4?&E3JX#_d8qEg(_(lH7uiLLbq+Bs1N77wruT$~jyCo}F<Lk*}%&m4zGs&`w|9V%Gr2L=APelIbh9dSvYybsGTj}6u?Li)YC$=X9+tV;Ko3)RVNU}ma*`!PpCa*o)Y!m7H>@@LnY)|-CD`)|#(AoJ{mFvYMDG8?9bs^7@zX<tidLq`pYpi6+{&;v+_L|7!UsQBpI->`Pn__!Ds%f#3yWff0^Q!Tmd?pc^l(Zu^kgt#Ytr*W1rS(`~_9pWFn-|FAzyCCfHQ5j;9@KRli}H(M=U@D!@9NFmStWnQ_lAbijYxd225{+kZ}BAbZ*cpG=#g#|OReU76(kLY^v8x|c9Aj33hFI>iSa*~r^l>x4$`jG#DB)|5peWY3vv?qvyp!j<5{}Phi3XYv8L6W-wOU9Y|lAtPhE^pr;(cE)?^Phrke9r(Vk^(9f+pk1hFE<pBvl6<wNV9aI2d5&leAeK-Yn!6#1&ie}wHhpzp-G6#pVCsyV+Ee2?92;F8iX@l1@**&YfIv3Ebw`d&5u?S}V*<U8@i5BcYiuZ-=V*{D9;8d4A5Jga(sEBND$M}V7eoVWv?ui>lvK<mJJwApAUr+@MX2gJjKrH;hgq8-aOH)8v4XprsgT0w{Yxx~(?>iB-r-T<11oFNvSpVHgA^kL$?64CI6?yx*fk%m4ptHOW%$PvD!YCz|gPVD7Y1MsqWA!?ZSjJCRWQ?%-Od=<V?f+oD_t_jZ;m@}h829WDGgC71Jz@DzvB(1}AsvK`*abs`_)r7%ada*hQ#;lH7e;R9fpC(<l1HE_JRrs3MzmtaZPm?W1BiQ*z#%%xE-=bkA#w>o1Hqcb%D(wkB@t9oddzyUT@r|AjHGpf6rqZ9Q?cjy0J~bWfP{seoUACa*sR=(eSg{_i&Dg;ky=ec&jbO+<2iSc3Se5>ef44cb?RJ*Tc$!U(=O(k5{OsbE-qE1@{RGj<Us0vMUHlo2$NP*77?nlqhZM2l<L?)*wYGuv!OrZjvgcLu`N;thu;>Wp^o}d&?TKU9fu`-*YttxrWO{{+KeM1pd&rXzc+5Q|wr}>a3+qx?)NEJg6y(KT^jC*bn#-#2uXb%phJSuan!;Z8p}`(D;;j{X^ih|s>)w@3o<F9_{Y-~=6Y|pYAW?3xn~B$sX4AgSD>hA!VH+OaCS{gMRgRZVw<G$#Pf6>w3)s4*)7Vs{m28A)3Y|OU2N8t@SBd`x<+UVya~ZKUUd#?%Dq<skuVe-vjM<%>BD!&}O%;By(MrM%IY`3y>9P*@r!fu1<t(}JDbiq+9*9C6tMGpWTfqghGNR|K$IQ?_m$!li<i8}>y8Nb*{avf@(}!9@AN244WjWo5{Q1LenDJgqXwuf1efi$1N<8=e!h06vcXi**6kL(tGQYSavlX+uXvfrova7@=JI|K1x^j>#Owwe_jEb1K;RRx&c8+|@drlVzR~@f&Qf(pc$w6YZe=&R80{!neLwf7JrsT_UntZ(~zg@v{vgh$Z(mj19^Zz)34ViU><i^~gWd(D|;hT|F_-E}Y=`i6D*}bJH3oe+zloC&pRxNXge*3eew^wu({ubNTaA4~r(k{yiR$~5%$NY14aw~TH(;_muv7Oxflak&F-eEqOYyCoWO8+Ub8vjK!<gEOBGA`*fxrg~m3-gtYf|zC1YfA6klb^3t+cp9-%s<C4|LDE)hhNbaB+*HJ{#i702%N%va(K;e(bru@(DSwqz|Hz{^NIhi82HiGn2pAK_2YFJaaq|8Oh?GiS8lg&k-sqie8T+mc8D#r-rbh|HC%rFxvtk5LNWgs-0Ve%t#8CeX*qzG=?S^{C*<Hv!snmaYqE=*c}FvK%s+lL<exDH3&~~7CnqtVd^U|@PcWZ&*N{(~y3Qbqn6Fk$xL>@++J>#gd^Pr-{Cw4T?H)Le`DYg9pEf~Wz~`TW8uHJNqdUL}^T|jqv$%D73~a%CGPH(#qVRDyyu*BzkNN7sM_u6a)uk-?`AXyJ0-$*PPRD$r_`w*SU_O!5kWcDIWx;C9SGkz4tVC1DJj_?#HRP*{dD_6|6J^XNqw-(UJD5*=Yse=JTWSHHuNq;#+SK?I)y8~fTSLBjdTs&X^G`13AM?GI%mDMx%o_5K=jR>3=bv=T{NmG@t-t~E&tKi;=bz~}Re;Yw3YdQs)Xvc#n19yPkbhbZT?l;s>4^Dfq3&za3G+|i8uE{y^K|gW{9}Rn=UmJkau4%QQ4RTLeG`4)^UsZhljO5`4sC$>XGjhCr?%rA|9yq}s8ys<*Y505kFLyDMZ^|sO>+C4I0%}>uqu3?@oF%?U`6f6ukGVV!o{_&vg1(-jz>Cpen~awx6vgr*sLDpR@?iBIv(TUZXW`*z2EEjs3o);77DWYyfx<YMi|dN)x`7qBNwo_nO5ujpUk~&U`~A_sO|mOuh?7UU|2BJ_Woh+-Fl*=6aup2*K!=c`2KUGn*MX-$ttqB>DXG|Pxfu_jX+aFsO|mLgKJ}8;F-0R@>^TXZ!fXG71q$-YMoEY`_IGu{=oOo-q=5vHkefW3;U;G4gIsW`J|%1)poq7=x?>X9xM7!ZSMyv`e$v&yNdoMJ6?ao@wyj|*Wzl%>x%wX+wrBMztuKhRrI&o-Vaptx7v=s75%Mkgg*S3>@3y@&STAN7LkO2ZIaJRhF9_L-!Dycqj9Pz=lLX(<k3~U%xW>a&%LC%P3uEJH>WE6<^^rt#<txq%8IoCwaX#mjlbry>xEgw=CD6_IM%J!zxJs|qH&IwMPr^hh^M%&VkvdZ*(%7PeeT&;>wkM`1JRCjMhwl3#Ou(%f8?&>y<z>BUzdxtI{(3gP27w&`N6&-GjV1{8p}2Dgprnp?08+rD*P_*AB%YZu46_(ftFaj0Qr}+hr#B=p=9UIKGpL1<6U96Pqf-LSiG#QC)4cNkY&I3hv4%{L|%LrTPcC)aD=$5(3q|B%_L8wBSF2>T_P_&Byy5u<27gT67=tJb{v~m<lq+LcSIyFKGQ_qNmpG{@lEtU{G<j;uy<!WHFgqt@o6>M7+$PtC3a8xMfU_RBk%GYn0uiq$cvA=cNe(#v!l2(^3!^DfK}!vMEcAc<i)4%%-JNar&#RWxP&g5oln1Kz83ABn*{Ru+oj$gXv4Z4#63qYWop~E(P<55Nz5(`snQ?p&dm@djhZNmy{yel#&#F0c+O%Pa|-A!_byQETc=vS^VK(^&*#1A%6xyZr|o>^ulAg*8CMUc^le{-pE|XN+g}!YMVGFRmK5A`6t^QwnL$HSxRkk&>h*J~mT#V=D@y6Qh<5s7B38rpgz1fQTir2{dA7bvt834Q3)*f`UD~i?=Pbm>vc|Be=Yfn!)Y+=%URC&Ct-gtx%-0p^G$5=f6U0Y?hO!|p!`aH6gGsOU!PWBl_M0!gLSr9=i2pj>hQ*&)PVb%$hYagmL|%N_9`ljh)a)iUJ?PCKyCLX(@`pr41tKp#U%GA+z4GfIHb;J7={WGn4{$5)bU-98K6GMZn%=Xe_&T;H{iFu;#{QXIP5-Qj&%A?5Ov$CKc;+>AHYRu({fPb3zncD85g+4l3-<oIhj<$DMN#f-(vq*D?bpmeUVH|e4x@8QK|J*KV|r_LK2e_kTC};E{#nuA9)5m6ueH+<OC-VU*!EBK=EYP}8c_B7|HbXjlbQA=pwO)c`%$bezTQKZ+7_j-aSbMtIX9iFe4jt8(JIpHj4{NFen7V@RS_Fd19-If3)QokL|R^``ul}S;CZsIp%G~AJx11NsfwFtHh~9ye~=mGlSuB)s=tqHa&HWsUl_p(7ef*|q`A05kRDSQ`jl>4>A{vfdr|fK%;=|N_M1<n(dzvY>$8?(%h<WBpLJjM>eO~>qdm1sf9r1XhCGXUN{WB%As)XQi?@U>Vs7oG&~_suSn}9sv^xJhzqak+{qyZ{r_Jb&17dN%mTg(l=3Y$Y+G<h1sgJ7g9p8TXBmX>~_us&11o3$w7Qgv6hrS6NMF(2faf^u<RVAJu_x=1M|2&`f-{+exMC)0JKVI8PTl_vjl<NMV?ejWU@juW*5&pzK&*%Mb-zA1plP2N;O1`Z4`w^0IcmUg%oL2Ss-*t8W$Uo2L{mY(T5uRUrJijT`oL`Z;iqyYK?A`r(zHZ_jA2+b}(fKsx=@6pxtUs&n{Cb_ZLCzX0!PWuh;@z!wunmz<$h6k}bf#AftFHg>{JG&PB-ueh;>@7|EU8fgX4c3X>i3>Zt2@6u-#>?uEn|bkGxqPM-#nX2EMNBqy>1VxJzqS3=e9o3IoD0RXzC5Rw#W@`20tfe3x8G{f1Y3edI}uNHW#lJ4QDAUQ(^o39g^=m%xQJ;{O9p*#N(Zg$2+T<<K^SY^FO@0Ppn5^d#=W@_Nlq#%kXCO%%G%d<CE`rw$^+;{(r*f<M}6iKAwNV=i~V&d_JB$e@pX+lJx~f;@DAbS;6CbBzi$0%PTls<$SH+?vb0Fzmrhy<<v*rNZj(*BFraoB&VP&%W+m=)$z5@JtK`C91^W;Vk5S>G?#sU8V4iPchQq^`Bl#E6t`zmer?;s^ZE9S3b7(V&53x7s}pmXngIK@c6V#p<9gNpHtO9U`RDn(|1%M0v_MxR*7e^<gHO#M{v+2*f-^=|$v;6mzx|Pap3nO?O4VjP>RE~3f4NROZ9httI;b(jR8qx%vr!80C;oXp@Biy?Q?_2wP#p0%lpT6;iT+gS$09HH++XE=hJ|(i$Uo2jlm1X=r;60S=;%HAyK!4_^X*$0Df~hwf4eW~{64zc{?jq+Iw?>-;HJJ>B;MRHp9QDnkat~jh|AmrR$YJK`OiDv5ygH95NDqWU<=RaK|q{0oK=sY)tz6SA8S67=9l=3dzYP~mm=1RdVG$6qxnkJo?o7?^T~}Za&r}1E&WJUPdG4}wRg#Gv1YaBi|6~^?#?<rYAueOI)Z(TOk*E1tR?NN)M<6`<oUIY4<ApSf9B(E+HHuNc;v=-_F(los;+-RGAcW%+IWWCIZ<mqAJ0GG^YQ!>J|E9N;q&qQ6FwhLo?pD{E;TRcA#SH{!WtaVr+;mahyL>)R_V`gt?!UVIp2w4i5opM++AG!Y7b4Bag=z^7)d%^>jl;E!#X}AE*Ek{ZvKJd$eH%cFUS?tZx}<T$n+}y#n&H8`L%5i&*$57Ax(oiJdF^y9y5#G3>!t3Zt4%g%U4(FKien2`6K^4pZ7mLzZu)LDO9{RKZ`ZEt<0vFdqbkQYJOI*`}#-zc|Py|xlJq+UGx_FId)|uEfN?@-$9O!9an|l;S~pe;-Ba9{<|l)VjfOJyngXz`tiyw8X0w3bkx63mGf2BvF;!F=lOrqA99wcNd4FEdQW0)V=G=~QcANVxkSs<gXwGOR@;9TzPLub?LCOOm00|wc{jGhsy__ZJw?BJ=8CH84?I7U^q05}@)P%0-^v`bTp-orrK_=5)!$S2^UL#>Z)(aCw|R^Ab>Gb<Xa~?H_SvGBx?@;%=Zoi$OHW|Qh0fyP>XX>2HhyeQr%Uu~P#LW*{ycwgw^l4_sHM2X^&4%{pgtS%*oO@mzpUDL^8DJyhmR-EukH2D$Mf=nW3}e<@%$4$AJ0GG^YQ!>J|E9N;q&q2`91HeL7n4qVk2h*rn1I_+<)B*P7bVk|8vXqJ~>nHmF&3KNwVu;ck$`HmJBjF;23WPS2mBS!e`^3lP6!EkQk+j<Y$MD;;deC>4G^2X|~!_qG(;U|MUKNer?;s`{&ye?(9u2n8%1;Pt9ZvLr2n*ghZ(4P*A169lG)UkNop|-v8Ec5jZGDioaOrut8!~c&F$IU7l>O`g?BsAAjVZ=kxxRnoWi=V|>KVLjqaDxI|bUwVez|9a4q=R~JS26aPG)_kZ-XFU0DDxbSx&U1)ue>{#+jv}8}!_#a8B_ecJDKJUM_=lAFuRjL0b<6LQxtGn3YrXQPXGJt(_E1{zf?iN*de(ShjBhThpkoSHzV&%RCwA+T|w9^+6vu@M4+Wy1y9afwWJ?t4E_AK1R+y}a_=>ul$cN$hTzwzgn=Ra3<fvBZj#80*|<`164@J17nzD_Eu?tJll|AgtVY;$|DXXIq|Da;3wzF((w#$8%n{CU3fejn&JxsCW@)HV8KXFX_>+l75<pI>b}|Kxc2c=G((KCkidF`Ry^)_gwxf5PYE`6qlno`1sU<M}6iKAt?Ee|uk1Iv1c0f7i<?@$XV|b@?|+x%z19<9Y?O%4n65rHEDqtqOAMpjAbyid!h5RYR+W+tx)}4{bf%UY*n6G_j_IKI)^@MyriJl+iXo+W>v2pw&UEgFaNz>Y~*}A8KeDa(d`Nozq7z8fY6~sn0d$nxNIdvMFc4HACB6S{iajXbq%g3(gp=p|otlnc!b@<QZex6gfs%<4b;Bb6jSCr6JZ@V9k^><IK^Tb1kuE!L>r$3O`wJt+_U6+u*0xTw5&LBC93VO|fQ%r3KbnV~t<a61QlDHNI@aS#nmKHD|-wa&}xh&K^r!{L~JYT1r2+!_rR5u*TAobKo2~C$2r`%ymHP!nxv~v$W>MiSW-AYa))|GH0xdkmbw)Zb77L-J~@q^x=q0c@Dvvm~+QZAgy^|o&W41{p^5s2d*PpF<KA&>wwEWxlUYX&WrQrx^O-SUuo%u%Y1O1FTw}cc_FJ4*1b7D&Yuh50=ceSH?-ZkAVeV6gScQW1i^m}#?PUMV61oL!m#F#^-zRAazb%?-bXj-ngIM9j<pCxIDU#m8zKD^iJ!u8e|~6tAR^I5;_|Ls6c^3KaIuJJE{==mdLrVuUedBR*N5wiwim8TKxRUD*#~VOTpq_I;=10*>4|F+(MvD1(Of^SKR19Ii0ID^;s$d=@GnVP8;Z4IXoutH5xBk|Vg&vT!`cX3J`7n&+(@hqMh@>|Bz_)%HknJ|Mxh;zWimGg%VaJU%VcgWmdUtoEFz5?hc*Sv6mGoKj>d8{H$iI0U^#}HD7C3rrgG_0I~L2axMjN3$2cwnk%l%6x5_|FKsy1M8HjYW>9~FpH<_D)b_zEY?Nn|W+G*T$w9~m6XlLO1$=pnC7TQ_Jnu2yV+S$mRigpg#Ik?3%v~$tU#qFk}oriWFZao9-Otdp``%K(+CfeCpW+Hz!+PPR}B4;kznON#@^YO}WfK~^~#(1sIN2|wW;dS4H%SM}p`Jo|ZmU?Kl@NWSwZHSi7G1<6Q3#~SO*2J8lj+S5F2)A2+mS2;F-W#BukAF?jcNW^e_oj<~dbo`)q9OjN<2DPqMciU;373QAQf?WRx!iIrS8yw_T*a-%axs_3<)dAM<sxp4)aGEB!>yItWmqob)=BMhESGcZrFJEjE4d9)yBf=l=p~QagjmCEMy%zwAl7kP5$m~ahz(o;SIBMWb|7|gyAZp%BE%kUFJd3JA5n}92}jXVWNk;w&@$xiM0)`30o-CY+Jk5h;&yw`9zuHvx88^LFxtcD=Ll{q;Yx5T${j^M<BlQo0CyZY2e}i-ImDghPI0HXGl;X?ImCJH0^%Zf3Go+q8F2*}SGj9wuOaI++ETQo$UTepI@;^F#d)+h(B8o9E~34O_9kxq7us8BZ=s*txb0Q$4sLagyNmo%t_+#ixqHaD!QDsBP3{5rkbA^E=AK~rlzWEdbM6I}FS%D(zUJOw`G|YVy+ivD%ZJ>1seOXw6Yhi5KEv`E_fcwJVEKakB(<-we8qj1+BaB!K`(E)uZZ{DH^c|-JK`hv1M!LbiTI3th~ru`{d+E6-0S53nk9F>?|;{2=N#54pI66!=Er9>>A3tq&qGu}jxs{EoXd~Fb*1BY<!$-)T=$A#XFSx!_Kx2~C$YX~`%Kt)@Fm-b^*VRwG4)}u*&?jJ4AeegVEs+B8S5WTXR#K;e!^+2pD39IX(qp6$&fuX@XIQo_TNO4u%5HV2tJOU#Cl@gD$<hKB~NAnSWm6Lk>%KY6ZOUVtHU<TOEU*%WBrC}GZ?G76kcm|U=zZvVTt=|No=wQ4E+{QF6TC5qY{pii6PCH%}HC<SK}bz=k2L&0Aj4TkJ#_bq4#p*!61^rii{hiLsT7*IG943ZYNkV<!iC{k{RsSu!B6%wq|?2nnA+EQabFg30u+06f(AWvcd*&qT(!LXw<5ZHZQV+q;z8#=X{wwSYQFN^Cat($N%p;qrc<H$62L3)@tR;x6XBgm07;9@o|wT$%lZX@gZWF(FN28dcywC59kd^XK=V8f;!!LFvZnHqWBh;Fn{y|n&RA=ebTgr>wAuo87(bXOZ$l=eMbtoH*4*-Yr=H0`{_I4u`Cp{Q>T-;+domuX(140HIeYY;Yv#9yViFYN(`5Lp|d83vCS})Z0`GmH18M6`17pC|Ner=;@^wp=e{8(Ax`~>g(rq}V8hKEw_ZoWz({K}2_65E-u@j6X{~kHm#aB$9g4!>(t#yZ)BH8vOcNj?Y&f}*^qwRaCcxfh%V>*JZ^_#{P4JJ<hb2=MLh$JUaP|2lGO*D!@EJA%w#C{&1OIVg^5i#}>2z5HZ?eH-S5xvjosreYrobKL{=|8cAG}=F53c1}K+u>GaQS;Y40$~qjE(k*ezi7*Huo|h-%1HWhWsMOF4cpb5tAg1PqhIT2PeWr-O26In(*RqI<XsV47ysE$-*>i=;GZ4?w&3o^BWt2=FHio#j-i1{uC|9>ivOQEu8}`l!k-nWo@?mR0?dL90!xmdsCm(bXf315iVXEEm8Sv4qV?HLPT8`(Nj~>VYc6F(y>b$*7jl_$Ub8R-?a(*`6&b%{~QHsORvz+aVns6C;^%s^^x>i)(SE|D#O%n+eF<(Hn4ZzAd)e$F)dxK4{2lxnS4-*-8lP->>AJ(8iiZ1Z?npXfmL(pdOD2iEGZ;ch8K#?yBLzoyXrt{yM4rR)qcrN@g%raG)(m4#~zaZX%5J8`mHhpRoaZ6T&Y!Q`59M3&@&oA8Wq@9@~N|ICF%Fg7Eak@R9fCFMqO00OAJ#+xm5BayWf(fhT!afcYj9iGMEI@$wy-)Sn+*5Tyz);is36HWSte<yU-7Uau_Kg1IV4rTCi_hAGq9T9qIgj1Tngv0y~XjD{cFib0aW(eV-)v>ijR&4?R>Qj?EuROe~wgycNxfzV>puwqy=`Tz#4-K0G8E)pJrMPS3kdVONM12^(NgY5C5{+8~KAqoRVzM0R;c=RDFlRhxZr+g54$$$n2n4~CmE?Ju5k+mKOz7kOWDk21+>C~IX3AJh-H*|LS;RT>LpJnx9wxY)q-2SE^OK9ib0$|U_j4OBn5v0ir%5-r1AVt>0kOK^y-<j2&Z5#)W`O}*NC{Y!OS$y8Xg>n`17_KRG9Wei#+ZggMSMAEWTENs4zBl3K!TZvN?It}`yX;5eL<Vwr;#im2g{AMhEb9$xaw*9=I)FzgNg}1A;{1&GVe#x!aaqpExUK_juipgJr_au?|o5+%HW{_p?LH2EMgn<WFl7eon*hqG^66fC)6VnZuFzCJ-xGi}}He?Tk+ZQ@X?5)&c;5-Y6Q<_MQKeQ&YoJVGvaNOCOoKhQKY1^>0>2Nw+1VQ_!Ra&0-$saVDO@{l5c9oWQac>MKPWysI=BP^kW%uj$s1;mDbER%Zc976-JNY`#f&`AwAoDcm64>tp$Len-vdjNn*Visd2h7jbQ1<qaNd1*HL?*u=FAr<8!@r^-S>*%mbWxX@b^1=m8qT6Yhl5zeH+LBJrWesyZ4UhhHH0y()LG9_O+i$$v^dIU5s95X0yM_y!Vc~woi*GNa%P9Z&%!KnLhB`IdTauXtz*t+UUG)LN%1T@d{^=Dpm${Ns-`T@#1p3Ojw5vjP9Qh5he5l5VB%%s3pza;vit8vv`?MxFvB;KsM`LbR-<RTb~-zbMfNU{%v1DY<4<a^m<@|*{BmHkgO`zch59V5OMs|v&ko?CYs=b?>n6H3JD;X#4`X%RMf*<|Zlh5nlHl!&Zt#3|0kyvSlpOu218Hi{$*B2Oz|u!k`-Y~F-`*9D+r_b4jmNp!s(vO)PnB87^FU^>b1;c~IhG{43}?z39m$h9U18jb<uv&IH+sm?7hJO&!s^y8bbdjJt6%9jXu0g5WEQqF`J@I+#dh|=cCHCtMyFvr3%FcSP?S6Svb-gW!*(v8olnMNJJ<F1VJ(I}rDZl3=(;!SX`FRm_RS%SC?Dv`nzx%m16nA7EN7?KQ*?B5FE;e-6Dqs>*@zL6VPT`F;r*0KKI_a?V=sQ4AYGzGm6pf)4rF`2A0dSU%v@!ctKfbOale85eupe3U!(HrnpS;bcfKaQANhow>lzR7?tYNar~#`V(4D;L7X?o{HI)o0RbY1;l#&HSZqT`~9xGK^CK<aj71HLmCH%GzaNF~^t=G3gNpfl~={|qhzf^DgID{>He2=trUrSpp>&$*{jw6eQHUdAh4Yb~~IM7~NKx8@fb7vG=PECMI-q)$@@`Y0x(}t&J5Z__Lne6h_Z5y+c?MJC=fJ3F_4-yll?+PqvK3ycQ4Hl+>5~ac~w6sqwtZl!KWTxbhPI#Xix!D(T&*;I?Wi7~U>#pD(u~rl?MVCq3Pm|^+99WS=k8S!^Px2u$jrA?lB(i=~3Y8>lR<ENy+0cKfo?sRM{s;8wGf-qDF^<G_#!)hOeggA*)(koZxk6#?Vk*mlk|9t(d=z<{I+4gO@6^j5>fKgmX0N7JTE4#BMv`Wcz`{q&tmMc0TqBX))m!xCl)g+}8#G_|vVoFZa;Q~Z_NMz8T42>5$VhLA_;w&OZBU<$Tt1%4a{gU0S#-DuoX<BQ)`ef`dE*w~mC=EXSIMT=)B?zbIR~j;XabYvH2*OO9)*mg=8<!W>~f={U0}3W6}q%oRB5?$)&`;;mk7QOGN|nGwENMbNb7sV3;I;@FT3AcZ;#O#CIi^&S>q(CM&4}nhUJu-smgYg2C!Q@>p@`QR4Tjt-*tUy@bd%_`uzM1L}Oluh1ZHeq##BiMkB`X@8crIBGM4!5aW5(AjCv|ZW$t@{F!Ak|Lz20Dq<R9IzN{lF%vNhF&i<5pYOnH+wk)$5DO8D_;+9tOZeYD5la!vc-<Dna_RdU9Jdm&3b7iI$A1TpSc6!LSch1T*uc+a<lkRKZ06@_@y~pSZTuX4L?QoMCSnJI7a`ciYpWoN5PSG}Y>0ileg}eo>6V|^$NxCOe?fl$aghJ50CAZAjvi6M|CWI`hB%Hm!OunHpWP6r5oh?hWr%b9cM1GF8-6Y%;u8Ox0OB&@3O^qaagEpA;D75t+~ze(5O?|CIuZ8}_xX7lh=+(rh{uR0ymkWO8R9wO1+OK7c*W02MZDpE8%DfCd_sIieBs|`Mtnnj=jZw%ewIHUyBhZ<H<$h<o3?dl8}ONR$u4ykuri1(p3s#TxXxnxbzg{<N9YsPH4T}QWd=Alh$SsI{vyU(CPML(!6e)+19Bfd6?Hz;kLdRQK?+}lvICp@k?kwKP<!QYCb64Hc-@q^h-R)G?Prr4TRzdxFN5JP(QFb}_Kwu86AB&3MA8$VtrxDY=cdKK%YtZt&_U=T8kX0Wxhg~Spq6aln^1D^(QtU^m`oq&#1iM2`y>SGA*w*ypNxXx$QddbK>F_;1DfwLsqcm;+NsW4a{O&3y<Rw$I=+d8oYx+#+uF}=OXJlb9qYcSCT^jsc5v=(28$fGlH33lIFPIe%1;`SUCSPjXw}mqrKS4BWXcHWsB&7Am*+&1&ql*0<TT&anzZlsj+DK9BYHa{i^j*L!tpn6M43}E=;86|u=s=l{60Eb60I2z6S2N&eoIN6LG9pzLm&98lt2bvQ)hF_MoXsHhd{82F$^nBCP$iyfP3M_+}#ZzcG@EH%n|R+<0X=WxBf7sM}5%zY6Wd<6q)*-OLS#mtfcp*MAj{+9R#Rru>}Jj(VS2ZcI#R_=6Lony*J=%vA16gn>*|$d2&ddUA+55)a^<T6ps_Jq&z()GTunYixx0;w-5E4A%-E{^<nP_O)}NQ4-z+CqMf%kahcVmA8S@n50o1@u|aEJ(UDi(nRb){9DRO?T9vi~+eY<aOV7`Ak=yO!$*cRowz4wPv|OvJ89)C7VaU(3=D&AC81tGW2vdX^!W_{OVZm!&AzCBaAljCHuR~w?Kxwuny*4a_D&TY3z94InfAf|kctAL8_j)Xv(#L~tQ|b)%qs-`rA_F4Jmt8K)m-W+m)EW9+OP}RhI<TyZhAixwDh#UM02-fqK$pI8hKz>w+4CGfcJ}34>erf9;>#|V<;(i9#AC4K=LsO}5%Gv#h~9{V^6#=fxqlR09pXd>e^I96erOYo&^01XwJANRZ%K~r{_3hQZ7?}mq(i<KHKMah`jMGM6C_i0+?h#;1#>NVEot@YGco%T&GtA&h;)|*LZ7yJKvGYNvcjBM@<ki4!hKjHZ20-I<+)#+vxl5|ti}?1_(;?fgCSz2I{Wp~L~>^Q0kUvkJ@)LJIcz+&kDSRcf>9oA;8|G+tTJu}bJjM45yvSRUZMxCc7BpwkDin4I~z#j*hMg}PY4vA+Ch{e7lVSzYZBx+nXZmn0HM7@;Hq~z&HlL%dLSqEZaucIl_7k$4+f={`VhX<Lt^tY2$ZhPrB8Oer&oRl!ASevq)(SOv_-dbM78Tg;%4%gibZG1oc)98k)JQ9Y}<Avgp<5E6N{5*Taay!Y#U^cSJsd0vB>UAHV(SqPf#cCX!ba71>F?v3|p*i*txRjk}}gPl%HF-^2`GAs=qpO5AuS6n#;(#?p@*SM_us993v`xc$@O`0Y6$M(V0_z(3zqs#3d{k9^@1es}IJoXs-=uj?!bp8lR$_LmlCMm*2!A|0NCS?+VS^I)m9>OZM)2D`Ioe4)}T2RzcbHbKY~(MeQ8T3ie@6UyhR)-Pa`Tx(|C%Fqh7axkK2^NM_jnEbY=VhX#8^GugKNP8`H0YfUP)i(y2zJzCg?;n)T*Y=i9a8lfK_^b>-9WRK+~?rR?I>j3UcHVzK_JKzXjNTxl)nSbX8;lhtOye=L8&N@HuAHjdID@M2@Joveqh$uufA_fu5e@Bn#iRi=6ZA2uN=Y=WOKJXyF1@Sl$LYr+0gDi`~l8yU4=wZ_!Xl8c0xal!-GR(6RteECR+~N(0EMIoHylwYd9;Zd;^<jguBQu!vls>!Lo;{dYj~(4^#8$<svgk?Lz#jOrwZZR+@*q};FT1=MZu=a!efW=UJ$WrQem)$+m!HRrNJ0!nj400wFH-6gQTMaOkt$88#QC0RW7i=>3v-0Yd=sJ+;3-;wdBMnDg|t!VO5S5$Xz{Wxjhp_N?#qg1&bB6!xNl;xdSbzzg*|iYnHk9bTCWGp{*x$8)fujLw1G~zkIuY?96!&uJTG|Sb@C!r8)iiMNDkmN@H9XTzTq{XggGMBp#hj<j#z`&-k8@-*=4-;2I2M7-L3(f+SX6<5_3er>_+Sv=7`yNo$p({k+#KLuo<t()w@?xF<z4qm<vjV&Lnx53zYHtbRRT_?7-_Y2Kfae)ZirM1x1HoxR=?0<t^}#w8Olh#VsKlFfZK1ywFE&3q9xkhU(+B`ZF+z?l68#JLC0yY4QY;hS#%f+hq4!f;nQcx~ga;<_OvS${w$*AKBxQ-Ir`kwD3AH_Kt?0`77u{yarUwqM$8a1M4wIY__%mxBJf}FYwy44)S8R@Y>sf*N;nj46BdV&w9)e&p+z2n^|K-uQ3<=cyG)OVlLpXNi7jRv*R`S6m!9v{FlTLbAbk4pH~{6B5m>dG{n5nZm%VnVP3Guyl^wK6+71b@4WC-^&HuTd4XVFII8=aMwRCUh1feZAFtJ>cs*Y?&msNsdVYy*v%$9A#<t1s_ZQ}fJvx)z+F_26-LLHN%KDK#F4=v_#>9`G%ZUg;1R}a3y7BW%_;;jvT?|ABA`}tE>!2Vac>NSa4@5sif5ZU(9c;uPUYm-aAIEDrB1V?q*X}tt0kV5Gr&mXtk{)j|;PP^NmN8)%8PZJ`EL>9=cjqwiR-FNzva<Khx6>t44`svUk4DhTcpx#H-UbF|jRUtIM@aB*J2*bX9vCwr%K}C~>XBrse#3z*nVChdjS67<%#~p0AY;gB<<6+B3%MSV0=qUCkVPgTWc=P8<YBQF)Ss>drTp)Eh#QFah!6anmuj>>leUkPYJc+2Id@yWQxLy;slY$KJel#m*4m$ID&FDz=SnwCv2o66$!0GXIyKxH9%^)8-D}YP{Fy&js{Q%K;E`1Ovv;xw+^9kOQ}S{a<Fz)ePd|p`A<bBP!co$!2JO$ip_)?d&*aW7G{Y&8b=EjYrq`hTIq3RaskZ2knTiMM?98D}759<nHE4eZ?wKXk{%qX4&Ve-<H;7pzfi^X0e-=Dj1O0jJPmGVrosO_T!whs?CP?IIf3}}tA>|KVKN{8rT_P%v9pJo?u3YWU%#mhN?a$(ssSx3?pRyi}p{QMZx!Rvi^wvuGyD>i3UO2KV%k7~-<6x0I?a#V*ETr0>`a4&#bFE8h$CW1F+`(L~_UEA%W>W3X_64h1@vB8-{T@5mcw4^qr@?VuP&ghZzV*(GrCl8-${6eqkDfOIdGB(*UC|v9C$|v?U3^Y2rf7ib%I0j@DJ_uqE@7<Zbm_aL8?__Y{a0~dW`BtUkYu^<mbUITOR5#By&#8_29}cH8jg@F(Uz+f+Tm0Jxo93E4(zj#4UNpD4`23yly58LzI&VW+8q*?b`ginI>Ma5liaz}MAWc`cP&}G68X?GK)ia+KK7kyutL?TG^d@9+;=$>M4hDX>V~PWV1vb~@LSOn^lNxmcX7mQ$lKgrOxw?3SHgT?;`i&c=}h_Wa`xHto>-1>6GzVP&x%7#h=R%|I^%4V+;<gg%ddD>@vNyAdFN*%o_^yn-TdYyRgSP{c`H=qzWe#$^BU<~tLFBOEF|(gdEUAgJN6@5?p(KffiWVr)grN)@dh^H&M9)^yCoQ%H<UXU>PAtrbZ*m=(EId<e;Vy)oCLRWM-h2*n<6dON#8wp>@$G*E?!3;^xP*|QzCyZPUcr1>D;C!9^=>#(^R(mp&c<ERv?i#x2d+*OU1j%2RzlJ+N+=NdXGEoz;2Y;!@$8iB=WRZ>yMmHBJOk(f81%xjNFfsIgW<#$-+jima5C==~AuMV%KG)tHNQrA>IOZ##qSJYRxwMT+C}5%rjWY!UNj^Otqk|=XH>)ZLsc*GBX|9U7To~$=sV{P|uP;h#OQ-uGYeo4$jiK^>2f|)9uYN$>w9~aI-}hxpV7{mT9s0wQ*v<ZP6_C(P+t~q0!8u25r}cZ<^A<r{cwKdHF1*G?_%-YR6n_&~_bB&M!RInJKy(!1tuB)UyU{SM&1rB-$@w%i|@Iks+RJbq(6CqnCTr<=JUsS8PwmuU23Wst{g-wrhAfpKs6gfLO_t{qf*@PrkORyP+jB96d<f7u%zyuEkW#zLOiz<ZHXSmGiq`JilM7$F#CHk*43}Yr9743}xmUBE^&HPGa$XF>K$9pR`+xC`WnPt_t2w;M(!t;u!RQ{q_^lH{B>^UW2x4Qy+J-D5$r%2#<GTo*wI`bC5>XpzZpl%iny!c=p=uLx=e}F}oVHUB7$%-JYQspEDyg$>GT!Y*h`~u2wXNXd6zDwnzPT6PKH<dqVqr^0i&n%lUkJuIW3m)Z$;Hbq(6CcXQf7snRfUJjUm34+YTMyPwprLEH64Ie!?oe^8_P@O4N%cwK|GtA4$)(9t(eoQ>y;*LJlCyhq#bPS_<++jX`_8@A5ehz%(_BjUBbI`+>cHZ^Fw7LF~IYP)`JoFp1iuI(CJgSP7oWgDrst9!(9kxt$-YW_cHyV@2umukBjZpx*@J?vN$=qHM9;Vf5M(WAs%swEcRYcgHeMu{X`jtBP#kL79^zI7SME<Q45yUrJif^K=RHF{sj#vM0_JS|4Q7w2iqf*#_x=i9Q+pC-^p5re>~?i{&u0rigQu;gvtVl~_SY|^=WT9{s!7HTKSos0G`H<nE(bQT|aI*o-jZNdH;>dD?c)0R6IZQ8SEQmxtWsHJr1g&0V<VnsXVy`b{69`#yGrp@OjvrfPINEV%s1JBH(WLx-Lx!R4_XVzoOjfz<QdI{M*1^08`j_K7YA@a2P`j44QODB$D<1dTZ%7|pxqi#UHgeS<=dc62^H#@T~g*Er?#3rkRvBjG|km3aya<v;TT4%EZmx|b>n`>CT5F<9RlRdLi+(G4OD{eI0F4cbP+Pb&okU2h|_Ae$o4GZLIJ)YW;$<{QT#vYaBGl-l@Et+zmq!lDri?O!%4;6EZb1$ngr~A{`z&op1-`uOjV}uUWwRVuJ#TXf+!CK<+#!Sd(YsV|Y54{`o+$9&eT9388f2+_!V01hj%{CXue;Lcdvr-{?e0?%}u993WimCO~rCJpF_4cqnK`Y1_?S{1O$RxR16z7g-OSNOQ$v)E3P?aUtKTPL$5X;qWtnK|$#aue4=LXWbS??EyGS|1`*~T`}#S=wS+~m#8s%?BKv@Qb2A8~8w>n7H@w3U@{r)cM%CNS$^Yq{DawAVSQc8H4IN!m9*lPq@}23B!~a<xM;&WNPiDxWosnDOoFbecjW3yWMTS8J!X@vqQ6`F31~buXB}(%<bS@lN+eyY`om+=2b&YFpMeJ{8&<wQYZeHqXlxM_7&HOVQmkqSKRGvA;eoA}KX!_gW{|O0|2Zhc6c$*MCav{s-;e_Z@DMyEq;-!12gdLChBY58AzlwTz_Ny`6D<8F1AWa_g)Vl_|*|U%DoxNVNd>oGTRd7P-Tw=D&#M>>F~&iv{0oAsok}IXE6I>|KX;I};Cgjh@IIk7UP}FF3wD#_^?nHRDUgTyRmFJn7ux){9?I5!*|WHw<LQ+rJUXn_FD4?xj>4I2p$a-{5Gbb<vx?yC;9VI6h-3;m3<hI9|*<AIC&EUOcN|ym<X!jZ|xJCg%Tj5y@;j=Km=*<o}Kb8vsAPjK%R~!xRG+f#ZvF4dcs*#l=!BOn)3N(p17=0ge}+YZx!W_ZCUD7`-feOU{}{1E2rL)sX-Domc|=`0@zHm(RgQkb&b%Xbs~_?|?;8t<7&ZUi^xfN=$IP(5Yd(kbV9Q#pmA_`22gkn&;mNt&vkdE=sjA#?IZ&=3U-KZ<@qN;_4^K)y9}LtugT9MPD2**mz}DNB;)hU&DBDR<)5-%QG3rqlLLwDZ%mRo0a_WsJ7R8h1OJU$Hxloii}=4@E*sDgQ9)J)le0N;CPW<!+23!{}prJ*FL{m>@~<wT=H}~J2=f57B<YJH(wgd)e@-f^;My5P}}RbLW?5qS99R=b0p^HsgCz0n=n7;{TuRgg|<^|@2@JfV{3c=T%nCBd%q;c`=wUc|La!Q|I4*zYkU7wp&eV>`_&5V*xEk7RA|A<j*mTYeB6!kUtUf8E3{^7`@B%01uJ|1c^L0McVhn8RZae>(1NXPf3DDGsO|lBg|=&L{a0u?)HdH%^q*Si!}9)6+wrlYztuKhRrDX(@oNr_U#{4Hp4QNRYI{9a^oQEs&s6lc+TK4@^q<;}Uw_(vDzrChJ04YNYt?pqujp^J^<Sa2R@?EeLMyAb&kGe=AhrFzx<Z@bp2$$Dg*?D+J$tud9g)~8QnGD`TrK2PR;pywGY9d*#%tMfyC{}^+<_L>`B)`B6<WMqJllin<q)yT&1_~nem{wNkO*O6-qmUq^IE^P&HokJ2IhX}q*}=J(Epp(VI<5qk_FuUAXf|deau*?wsD&k8SISvK)Ck07kzSnrCe>}+Qz>^J0!x=Nvchop_$5(%0{!mgA<E)e5%?XDzsO(-+C*V*t4a0$y5hsG~l9J*kUm|H@Owa(}JySd@8gYYRj+CqNwflRiQn0C%Uy%JNfQAW!AI59(lg_3>`EwL9TXkZQE0!b&=QGM5+z!X_3yh_x(s8+jnAa!B^#K1K0L^RcOhK2+k$3J;mY^v#-%*rW$PG^gCqBZ%4V>%srPrkZLm@?7ouumt3PapInoKhxC=J&0O32$qH@J+TLGPXo>xw*3$S}Lqi9xE|$Dj25&W`ni&nHY8e__BdJ!#f2(9P!exfI?cd56T2eg>Q{0CU`ZfD6#S6aVH7@?vuxKsSvuG_<vuG>Tvam#s8P+Y)wv=jN@MT-6I)<fG@1m7dC&Rp4^}<T3d|@rszOY8vV9lO$K<gk?zi^c5U05N<9<3vC>~WnHT5IW=zqK+Pq@}}u>0<Dz7;aJ(3>T?hMSENWSm$*xT%`<N8^ftw=c1!j&!QvO8A~sWMH8&?$`)Plb7zbtum9o0`Eq_zH49#aLYoW3wcfaG7pY!`J1+Iazb;tgH7<CCi*8bF3x8Y^il2SCFtpupk0H236{D(#wNAL6SFYeyD*o1|AY80epMqDdh{JW!h&Wu=L#l5Pjx}DnqSt>ZR`lf(xI{dI{jlsW)uBkhWdm`4-MK-yv>EaS;}Tw%VkoZdDdqH%`sgcN(+fWj!?hwjCT;99!*R)AZiG~iLko{x8TZ%=m+~qr4Y3@I9xGHTQlvT+W2E{N6*?7TxiqQL#5nYrgj*Y8|4qg%($J>ia$ct*8J8Ji6q2z%ikl$SCiz=&Vv<yKVzN|iVk*}8+BB?9M>~a^fzct{OsPIg1}@{3DtIl5$ym;kYE3ke`k2Md#iN<WWpeYmEG`?5ssYv);HOM(A(o5qXmqijg`eu-xzNI%&TBXrW63KyG{7TWirWWr%h2N-+#(mZ<+UDi5m{1&hd}gdir2_u%(6?+u0)U8+$yPVgfT8zf%W;wT!qNOW95}+RwLh-%fp`J%jM(wNWvw?$XtzBjCL`u=iepl8uhmxKvJLo{{MdCBqOWU1CX7&ufWy*|0DkA{Qo-T@BS!NqX%%!D}sG@)fBI+Yer6Cz2Ta<;Q8PQTaWd%FK4lJ+0UeU05nwdfS#fm;dKHQUZ2aDg@X7D)?4nG1+RW9h*u2RLpMHK1NW88$PBD^pJ4$lJB(+&u%0~9jGbRRf%#+INN+8>fPNCN?s&(7<v(8zbFn_Zg&Ev<yaL{6bYSDdtzoJAYe`(W9>8C@&Dh9<qhvxzGiH0zmi5*+NceYov^D@SQ`|?|?aZNNx$)3ElEAWz8^k%P4ouT9gSK}%!d;gMBE`oJ@G$5SIb@{EN_sQ~J%|1DaJ%;G-g0|5`NEOSZ4xZ;S!n_fTbI%{9n2wbj~%RgwTSGz{pWfB{5#h_e`kUPj)OiEIZ*6Bk9=Lx9n6LvB<A@6&`)zZ%q?<(7Q@1!PPbX)o8EPLEHx9ho=PBYag$)c&@jk(?GE+4cfoDLeh}4Di9A`e5gh-T4pT*)V8ephaEH~0wGp#njBP4Rf7A#vaZI;p+_h5u`jH*v9^Q*?da{u0URIa%#QWqkcTFJYiYYYi9wVBvWGQr>9}I_!l4*M28gjR+5m@8B$A{<WXY_J1>Z=cH_%sn@k7enWI5_O7#vbfW6vYor1f!ADNs~c!;8X8#i2FR9Zr^`{cHdYB`ZfJr9J4hG$_j_W%s0x+?!W}NcD*_D?chuYDVC5iMOmP}OpDz%?Eps(ECJm&O{tdde0X8M035Ksi!*`Sn-)TS<aE9?1yXG<z_(m)TGVb1<mH`)X3M>)vbZTkxEFw#p(P7YnG7HOwm@sFznSX@o5^(W)EUa^H|PW8^HON&-2_JdXb*bsIS}dNPOkbI!^<<vAr<Q@_hdpI_Q58|$%H8o_WA<!qd}x!dK0i2dj)1<{pqFIKx1~pyPS5=%GUu*g7(4}tUv2J3GSw5gH64$Fm+rn_@Fxt+Ts0!;xT1dJF+{xJgUR8bvA?JYBe~S8_SmYkAg~ZmN{Q-Hl~nkbUeiUl?cCbW{_?5)Zx?REtTT`HLEo|eP%$PUUI3l?YJ&R@I2#)TWQKFSTVjo9PM?ENR+1&N!f1EUcCRA&|jmHPuXo{IkN3PxIsd=HhGdF4OXfD{OGxxjKgOry~XP)Z7X|RvUM%DIQahPYVld$yh=W0_a)mNd46Q;vh9)AHrcqzUJF~hX+d3L2(RLn!qHV6SXrx*1t*t)#fb^f7oU}GbCW8y$8lhD(i@*~KChYy3w&FFM(kKfFBnkCf45X~aL4D44Vz*i*DHy%!)J=gVJ+ldo3h8!*Ju)Xiudq?Mm-_2ocinMkm-2uT{>k*rG5DxHH7ha&pfv7YI4M08;tONIAdg%-1E3N!~@8caiRrf=4^5&3&v73AauAAd))XWx$!Cv6z5E*A2Zs(j=)lqs??m7e(w)ak-2nA`UG<N`Zdu$@FZtXjD|GD5PFwsu(19C(B^0t7$a7NhAkG6kP|2A!!RG{-03wrwCW1A4K*PPOM8QCeMPn#JCD!8&1Zk7fvJ62XM9dSt8GYM<+X?9cTN$5C>^-$H~^Xlq|um(xir-8Y_U2%`>Z@S6zrF`Ctpod8MR*|l06nzMMsD}?7)aybJ14SHsJ4mlq@VXg&aQv7^`rYit^gAQ3a1ANr7!aL&Xj3^TWZgNd|rH^qR2qKS)}$Owxa3cb0Iv51aCMv}D?-J2bCvOLn0&nQR^B40ARgqtC<!VBgb@=}mHGAL1ku=i4@L{-_QFf3<=hU7r!f3*m4jFjmr`Pkp8{M+LsBX|b_I#-Pw~F3~Hk$Na|lv!sBZlz)#Ub%%y1aY%0#eEW$gZo?hg)8jfll&8l|?6+kLF12PVyM3rqu`^8C<-|-zYLaUb8_<OaXtSb;%gcfF*{T=y;h4S?)4cqge2rc~uJ%xXiK+q2Raplz_<2!A#-M$ujPmbbm}dTPdwL-OR6kFkVJEiIE{T&QozCUcCK%5FN$#wB(-v%MoEMRev+Vh*+h3bRWu?NX%#R{1msOJdpcO=S#IQ<zNu$S*Vz;;BnR3k7V%crWu2{iN?i5vMWe7`d42G!uOrkSCo)$icVUGCxrEfHv%KDVuR+jS&+pmW0kHGdDSr2B8Y%jTM8YbzA{l^RY&pGTrKU#Ni>vnk?-Ptdx(zde4C0p0%Jc3@C<_v8Yw**<Avip*4k32uJb=mgFYnyD`WUqxIDq|&f8`lw)*rCv;wGN$Q7sY(`ej~Fk3;@yi{UqQ{y-L^AN9S(LwoMqh9i;;MU;EK(?gL@`u=PaNe;faw#VKW@Sv#L7npNh@q7pB<E!onP$-6dXk42@|OZv2*9vQngwh}+lNyHZ7v;Xg|?<(!9Q%(uliO=l6B1CN5b{k0pd>;R{AwlkW<nK2t<_F80gQ-`|+~I%M96x@3ob3Fxin(&_%IC{f%qNnaH&iiSg`YR$_dm=jsF+VsF`p>0d@e!790J*SH^a*3-i#}s^IS2Xxw^T`|I~5%SLQbJ_pR;|x<JEdYbt7CLv0p%LZaA{dZ>DkD?dWPsc4<(xlXf6%VqhpoUY~b6PH>#u#mHT*rq&lx1`x+WbfueIw?LBtn98)+2yi)S&oy;oUP>Yx#45W@3l+EzY}RYhtimFM<hd*Y7vW>zM}M1k0g>^ZODg9ta#<h8AKA=fV6$$M0>+DGNWjMWSWjUYY}3>TuNR`T9(gO{MJp0MA}+IpAF7X&u*5eTWN3RckCXKoi_&MeX&E%9x~~)E^8riDR%f71Y=gMpnb_*S~C6s>AFIbZJc2Pd8hV~1}hqY%TIfl)h7g874u22_PLOJoRU3vHDEzQkK(P5o|ESKIV8nx8GKfLO^#_Spb9QI(4%(<`0C83@2)R}tG|QbcfYmd-ldzggXk;~Sxlww(@LqVe|dgn+bO%PY#U_T?3sC#t~(#criRa@5%=wYO)+2%>Xgt;>IQT}L^5j^kwBJh{y^ucgu#{t86>@vJ?MoPfuiCLQry3o9yN~!YFI$cn{q5Paw<{K3IZ6R!+PajrJB|bP+$KBSv6jnoyPmqxcY~QxuGiLbPzMTzl2_Ox-XeGus@Ua-xB?Vp&!M6_>pZV!)>qOwwbuC1lw>7+rY35vTgq7ygq)uTg7~{ig|DT<#T>U{zHc#wOoH-eEGHUSEw&2m@ASrA8VR;DIC`7a`b_l0X;CVGZZT~CO_|Z5ZSsseu?FAy7Rm~tW$Pmu9kafQO6M0s_j*}f%aj$<L!#;ohl=;b$R@7nR$vCG8zc{wecfen<TZ0mpHC`BzYU~L3HALSF$4Qh@@R^ThcN&+wBQn8+Js8_)N4Ti}BhpeOZ^rl+W2~YilBjFQ2pbbHFW;)h26pxN!$~=yX`LD6Kc-ZYd+ObE4wPuMH9QIfq5cAld6ud=UGV&Hu;Vo5xeN{qg_C%2*VdXq0B`Z3t(tg$9+9BqfzJ6B-PeiHf9BhDs!rNCRicc=p~&D5W&d6U~}*8&!Pw*$1B%mHWM)`~CcWe>jiFeVlup=YGG}`?cTez1P}%uXWv|VG)yC#!Np+G1>$MGX3Nble_v?2huy4-1TH~QBJ)V&HG^?+`;q{9rr>iis>iEn4InrmP4g5IX$204~J~m0;xNDpq$)Wu-7UWs51Hei0K<Qt_mq@rf)zdzt8)*fcnT=i2a}K{Cw=s?fm)JpWE|u`^}l0ORQP|B^|f1xmD=s3k_#41EPL%cw-;9faxcROzuX=&7!pvBR~O@i`Clm=*dhjZeaS!7yEwnY^I;|V{-bDV<gp!$!R5~KU@#(0uM6%!Hmi8;*=ZU!zcr|oaq}gwB%`Drf-~J^7}7?8X}3w@1O1eZ0F}=0n^TSrkxT@JAXd*=l1;EewU^-t<CGeo7c83`)Q4vXVbd2=6yGQ-g9G9(;geG*O2w!;@7f<Y)nRjWbCRp>>9G6RrMWf$U4@Lb*v%lSVPvahOA=^S;rc(jx}T*Ysfm*kaesf>sUk9v4*T;4Ozz;vW_)m9c#!s){u3qA?sK}*0F}HV+~oy8nTWxWF2eBI@XYNtRd@IL)Ni|tYZyX#~QMZHDn!Y$U4@Lb*v%#!y2+x+r?|hst)~U4cWdJzm7Fz9c#!s){u3qA?sK}*0F}HV+~oy8nTWxWF2eBI@XYNtRd@IL)Ni|tYZyX#~QMZHDn!Y$o@ZFLw0wH#Q)_d4E}e2`)`l`?>?e?J+an)zg-XKZ6m%f)ZXXeGhY@XjW;&E!yZQex}D;G{}0EE7X%PC|MuS=|KI8%3S!^*-|KO=UVM+Ny^rlzU&)`_%(VAJQ@c-y+aC6Fo5Pyp$?e5!e&;gmKgWf^^TL=TKWju9cQO*&6Mr}B<$tme6Y0U&5&m0x=>0*D<cH!MYVQ*(vzE1CaXpl6|0@GkqFIlonAkX&zmv?r=ZZMde^*H!0h@N#8RUee6o|O7$C7Z-Zz2xOhfbpZow>x~=hC^N-x!9~!`z~~!O^5>$Ul%R{I~jx#2@rH{9Js^YVWg3L99pk4{@B_Z_2_}IUd&Zch1k;o~gwkr|STG=ZT?(B2Fos#z#L7g%>~E75++(X?LY_MZYl&t4C?~IR@{a7Q!tH70G`qKWycHuSX*#zK`7AXP;`59@XMFyXSvI`av_t{9P1hSTnb+)`ryzs=vw?eI3b_#E&I}!im83m-(UkO*&Wf8^f@ANZyGv*gZ_eTrg${`ETV%|3B!lCRaTEYVXs(yI7BX_2M`mPqO3uDnFRN3xDQ>*Sdk9Ih|=)`mPH?q4b@tztrPngLJOwH-=&L=rttK0K<EmckdJNujnDFmFCOV{#Flp14;JzLc1Rwu?@XN`+>(XUE)F18n$h9$;X7r#i4L1)|ZX%MgAIkz&R=5N#1@|hXExxbRFnSC{J?*qImp|=n}n$hJh`6*0S-YFvm_`x>MNY-T<~9l~zg#!G*Q-=~ZFuvwwm)Hconi`7V>=)W4xed!Ha&l6}_F-shcxSdX0zvVb|4#v9HDvu#VBF)3GEPdnwY>&?yw)1q@N;za+Q&?_B&D)S`L=E|~qT<>(wNPR^h5wt#(Zq5(0MT2rLa4)j;_~s>zT+wd~!|L0>9Gm@lg*lwN{ePl|#zaZ>xl_CSWvN&XILn$k`Xm6(T5!gUeZRFl0M@Lu2Dgnrv;AZ_@m(m==-oqcyi6_S?^l`%7JYv?Mih_e*zWVKDa<X4J^lrA?BR<6&}!&q_V})K(I=*_yv~VxA?|Mi=GYLXez+fB@~_p?eQSJw+g}`9B-v+h?dC^su^!)Y0${ZFG_VH0JBH2YMboHHNda)k>g#MhDOsFlwsQDo;fAJo7TbugFU@r~v@sO+vW_q_8S>tM&8O)RMG?&39vl?1<CU3jFPxZ?+2@30Q|xX(RN#-fisn%V=KX7Wtonl<=0K8t9@(xQqs4lN&add3$qDLL*B4Q){LCFyaBi-(EQd3H?|D4Tti0*?)_Q0##}{s}HO+21e#ZKIsGF#0-tS-3!~73=to=*$F3sN<pH6MA$!*#cmYHR1+G@;Ai~lAekuBDv<@(ZE4+~Fg(6F<rCH@{^QD%OPq4btPQ`qBIJy$gc7ZlA`W-kAq)Q`<mB-wkh?T*KieSfYW^u*|v`az3w;*{ZM&adi+NCc*SP+{(`o9hQXgsC5fF5>!ek2yB;VgM}|dYSX9`Vq?<E2<xr|6=`kC-?jDN5v9J_P%txv)XKNeq7!f2;aXZsDm;21)~4Hc$)zC`~u+z_v>su9OA*&4^DIaSR}3={O0`VQ6EanOqna(T<XZqe|A{U<#c2ImLPqN{qOPLDo$wB*|yXVmmezhccy+g9Q^nC%gI0JA+VHW@6oq=eNpiJxqgKGR6mgMSX2E7Ypx%n{*u!*twoPp4+3b@XS0O8SBZ~rt;dcP*3?{0-J&B+$A4ItPb;P<nD<Ek_w<PUgC4b?#m_6+%?}gb-_;L^ruu=*-<#{l5k?PYKG$-6X|2ZsrhZ&}_R*AypFQ3|;K(sx^qHd}t{-YoRZqTsC~vNR_<vJB)~iag&u-eSABtj4KMoI|XG;wOgZ6h}uOqJBIusEcMDMQ4W&8iJD0#rt4<p}n@px_B<18wETVFWQWFkitk0o>L6w^SuJmV}o-U7_As>6p;{Tnv2{Y`hrR>M-C^F(}p2&+#5bL=W+zW=u8zc=5n()oRUxV{tLA8qfGBP+I1?;OImseI8>veorAtm*H>&s>~-4^e-?#Wf@h?s%&Ml){5(hv!Q<ztTgq?=Fs=ud#63J8?Z$mo<bldzyf;mTCXqc<c{)yqA__pXs$bzf~98&<GEL-G>ham)07y@$7xe<_EmoYZn{OhL=l)W%V05yWffHskDtX6lozIlZC-LueE%pR?<0vI|UN#yIEoM{=&!D)ASs+ejLAZ#PF{v3k<(DiuKqx*8w(I^rj6RqW`_|*dO%h`$7DCvt2!UA~wQ)agQ&o)qOzrzgItUVC(6sBZJv{r>)~{tp}qQF1ofbda>iz<BT4H?fJCPCqw#wQ9r(@OR~>;+Z~?{`%nE)lNv@X{ZIV>qWU58Bv<&W`muqjA936I(z~t1^<xBc>{O<Hd}yj49OhVQCf}}n6|d!PT|ag($GS81<FDTTV*MER`~0}~LHyji-SMd|Qa{3esvi>V)sN<RorsHJ`j;|OKi)kR*N@hE?9tdIbY$vBY*YPEku{{pG4<pAzga*2pvTfKlI%MG?e-T8sUOr&^#e5356(~ZL)ctDq?r1_TyI<I$CU*G3`F%qduJS5KRPl09{OpMU|`x5w!b({TyFTF#JQzE?VRgCOIh}&Km42Z;}3eoei6S9(e8K*Z?O^9s(!StCG>w7#&@gw(K_DNdN6tkMD>Hwi>)8gj2<VL`eE}wsviqe#qWmvgXd9U==aZc^gr#^OtqzFxUZwV2FlXEe;)Pw=P_-M|9|`0e@y(I%>OGr;_Y4Ff1^j+<Nx1&L|T|15x;A{;MuSHmFTzTckTag-tqpY?=gvNbg_yP_WRt8HeNkhsQKCsdJj_usZ~qq$`J(kkFH_xSKMg__rJxRdVPcXJ6z|T6!<$_Z?#wC-{HcC#Qd%HB{`Y=El$wW{cp8zmQrpTPNemZ4V~zz=I<%_jh*Nv=I_DDt{G%kK^!k>X94*$+ikrZZM3qM>dSmjU0DludhH}_pEqEybO6G62Xv-C^nFK#G2aTB?*Qg7nXEEBgr}%s#LwKWYmJn)ngpHAeCw%6z|vzvVcx}ccuM0ngnMrE7qwlz6Wzdk?^W*vt@rokL~j@%FdLzR#4^VFIn|?Fp0;7W2bRmj1y0Y4vfVVWI**<RH*?Y>Dvz&3yE5OW`ARV7HHSP8j_}pr4M+S6^nXxBizMlD%(vwtNmwMDO`czTjb}D14vBZBi!^Pe{FQQHz85He1r7o0$ZdWDaV3WvWNWQEX&fY<ONSErkH-lcGeh7~wJboLQzm7{Cl&n6UQjwmc000;7&J|bIM6u+e)^gT-WS@Dok#9QIJX8hN>bmK7~Ojp(OEeN4m)!gM2#9u>QyEn+#;V@RBiB7LPg1j@Y~}L>t&9BCGsk!5rVA<xBBryifgJuq;?-eoHOu;XYefGcS@Fs<!wQ@Q7YQh$Bd7hK~bhf6;VOZ$|)0Mu3bg!>5lr{b0C~FIW>TAt`w4<x*_m!(joAAZW2)?+=FnpG<R}1lco~7>5k;7@uBeZ*DO#z?u}XIU&#nZsz?ZZuFNO2>@>;s?xFB*X%^Txe~0jBGRldxR(Cu^_V2l$@ZK9st``KulsUz~@9MKLv%Cfu{>&~{x=hwqClgAW39>OI7|vw4mr}|m&~y;OB`Ec#y5z?Z(eEA2)Lnz%N`_02n`lDM?1ykmTpTHXiS2~URb`^Pqdx>r#b89uCKLRs2Ew^mE~a*yMG;e__7FG6_`$Aoi$UbNttRWe)e+9)#9(UjrS(K=Rvd9;bs$t=>~Ynnnb;lELbwY?-sHEd(S(kE6vw(Y1lBqh1D)WMF(H~m5bnm1435R??L<+C4q4tK6b3VKY}`QSJ<Ucrx9}H5wp)^ju01138$k&C%5a)v*Bft9)G2JO_1oJqJdKk+ocp!wu(U6sFs{}HKHlG5ko3Ja|7Z5oE?-_xhI1>4;E6cV^Vjm;(guk=ULhRM#SEXvaO39t@I~BKhMOsj#B-OU_O5<d!Fx^naPmuz@b@uXJj40<&B61nP<#3=1ipu{XUALE;&)C)zTm+fl;aM&!h68jGtH=D<49n*4PDjn0dG+*_sb8=lChWl*oN2KKD!h?e@YU{rEd(zA;VqzY>tb#9SoPbJC;|NjB<v33-I|2XMcYqcVCmeWhaO0KNy3?E7E%Uor@gJ>-UAGn*{|2E{DQn*Sdif-%D}BvYGhL?9&?ogf^T`Zl#ZM4)hO&#SE9S{s%we`f!B%a=$CsajS+*eC<t^FA9VO45u%A%Zrtwct3LoayNjRwcV)5>M-)t3on?#a0;&V{F#Z@5iWFIBv@j9m3-<nmn>KHf#(@6boEoblXVXRB(FVg6P6q6gnzwWM9gPuToJ>$Mww${%8wzO<MZR>^4h+_T_xd!U1SJ6$#7kI|InX1G7RDFo6n}Aj9!`MG^&Ep5o;lr;U1k^h7T^_BJqN!N>pgNbkRGFcLbmVVIIR>Clv$_s*Md=Yt4xd=Pal(gVF2f3N_D<f|g}CbnWR(C<<JWYs$ToE*#D+UD&I0n9%l%37pAr6@8qESKCo8IeNR9Q{)>^z%Lf|ShWxu72&Y+FlXXmSCl*GX>X=1^8md5bVevOXDK|Ik3;R@&cr!MlzSYq)$GB{*PzRUQ^HXv7eWQbo(yAek21=+rbTl+jVxedinY+~jt;z3io;v#&cyo?l!HvXE=;@!nRxp)#k+)wSG!fb!qJD#q>Dd;C?is6^OrN+D`eu;aV8Raq8#a3WA;k5Gdxqw6?R(W2yfFk^wM@FIJ&NF#vw#q55o7pF)Y*TO(yKshnIS#!$}pML`JwQ!rHtuB22>dpq9xfvj533u#({x6nhf0R-xR;K`)7qi@pP^D|x~lg>zx1S~|?o_9VRJQEq}y8KETc0DP9z5vHpzf!n&LLl<37qW?P?gj-RG5o0Ib1jBW<36B&kgS{E<(`HX1{29u{jGsVG^HPS}vc?cEJKIC9MmpSC?MVz7jQXvf5k@M%*M~s`5d^i{h>hb(nJ01E8|7-|pCr=W%R=?3TZ&Y!&W1bG(_!EkPa;|!<zkhBiOshaVbT3$;iG;tVG!f@YoI5=9U$9A>n(O8XxCa3`mEv&vda4{*|@}8kgYTo4ErX2j@|sv<+Q={h$0Kx#5Ik~6mZEC$@>g;GhAjp%6*xjN)LE9ogVA%BAnUtxzKT848D`$E?U$g++I^TIxTD#T`lWNC?7b;S-T=uu#Mpa-BEi3-YU>m-|cDdR4(!Itrnr88e(vS;VN#T_JVc0(FWKIIwWQcQM=5FBe?Q_zn9@m_Mx1_jvlnt&1rPv#-ZelnDJzn`9};y+{AR0+wLVtyQWW~yOge_c3GNGDMl1e#J!!1a?(Xz=n8Lw9%+7vN*x+PJy9T7U9L<*;}B_mdPoZ86Eu>POD+csB<8~fHpM`pQjY)pgOHE(sTqmXzESmN4+h=_UAsC#_E~!476B2Sj?}MX0duJrqeg>b-LK#;TYKojaF?%s#78C!Ksbf6`($j~RS>cL3rMTCgi9E1sQWrx$=X|hj6*&>BQ$PFz@@M2z|L1jtY4*va}17m>Vnk66Entv)v5czF}+Vf-o+Y<p4A0hio(Z!)k5s$UG)HV0wG7k_Z*1QUj$tl?o7&hUbvGH(!P1|;l$~?J1OVFS>VPK57uw;(|rE6=M9M8`8*Hmnhr^oejo`_YYFsWxV?G(1+s!pCt7PQIIn?i${0wSb?0IM+2bKk!5PLS)#3-nmLtC3ZuH`BJU@^g^kXkBvwb||Gu*)k>-hz{qmgrRKlr%5!@I8Zw=c2yLH-OVdLCD4vxMiPP>8gVt00N3bvQ|F%bSfiKo5AB;fV7t0^jdz5p70v%ESs4f~ZX<Z?TeoelUmO;=7&XKPZbp?CpMr^G`?Ir`{ya<xSbP6pEgE9SpyROBPsgf95tH?aIAeGlZTKmZu-q&k|-a+~R>-c-gyL5WgSiPvEURDMMS&?8kdqGl%6uLUQ>%bUGoNL%kfo=g#v~;eL$kb<~ZC%Nh0^JyMXc^n#$Z*82Gv?dMfZ98Iex4&24qHkvu=@-1!S5Oz^IMw<_KN*LQdB`%*8bE~s`_=f#=AX+~VV020E8bWbW4Pkc+gU1-|qo1Xq&H=qH2jpV(nX}b|;+JaT^=&b?>QpI~Ag_RO2QfO*<|(l@_$l%H493PhEBFL%dZ_|wpVC&0o|y8I7*P3=@a2i^SsyPJ@M8`m_O6Fwbn4XCglx!bf^!gq84PzXq?3TylZbF`_c3}?*=?fNU$==TQaGD4-3-_Bt!Bs>{>+_M!s*z%Rm8~7mxyT?4i7NgmH=P=>-@V|YppAjheD$x1U+pjpBTCG4AU9B1<6WNsaX-Pk$MyRWH3A*W=03>><6Y+^rK{mfd)AY2UAc^K~)`^EWzjzBldz%>td*euOSA987_Ji$~Ag-hLv51(YNPZ2K@!cs52&e`56pnbM+O%*?N8lU$>8<r9vJ6WljdQ-t(Ek9){CMNA3Ak{{Sg|W9g5pkAp|UR#DG-D;OMPIAY=(wEj<#fRD=TX|0vkVEHNqs^Si2AbL-tp$O%OG<mpf;|zNK{?Ejm17&2b9!J1jcc^8p+9!zb4a2a>^l58mK2x7ezSk_n^BC@S`I|QN`ue_NYGz;xh}++jK5RV*Iy3bq%j_23JMA6P_m-+Jqh3+IK-;Pt-RG7C%h|Xx12u=w$auKS{wOh9S*~!})5BDss8w(o!!1eeXRznAJd*z*UCcn&nsBPZ=owi(Z8e<FaG!&c45n<VMDlCqqS3%>?IB7+rdYU3*%P`k+<=8!3>xpDeLiFQNYJz7FEa6gB6UI9kG<~f*HbeXFIk7w!n#ByaQ>tn*lAu&MJ`{=a_KME3(hP~K;jU<d%`>KpM&TMIocx75H4l7)UMisAGhm}I3Ar!p{hsMgB{jNwETx&(23#HJVW`#g;bmI^(D_)yt>IUv_V~-zU&Amw(ku&euVEoVjyBOHs%ud=J4-S<OK{LdwxF4?X|SQmn=|5Vj0}elt1KcC8g{<4jbX(#@3o?>+1Oa6)A{sm*Zpj)l5E*8aEmLR<(-lqr+!Wg25LtM8j1lj`D7v5mMa-)pDIbd9XFaOg90)aZ>@&#&iF1EGq5?HORY+e|3qteaER|_>_#}Nc%?Z_^LlKO^()<QNcNPX0e>U<0k&M@-2wpfpG(QPQ!Omk3Z&O7d^e%_MNqxh9~i|5M4g@pM^b=_o7}|&&H=``$5tBW-s2K<GaP|HTXrnh5<~y&PuB$_czt+XyGCJb=QN4&7d?4eyx5=D5^dqUzCdLp;?(T?^X{163gTa43ejw5IgFgkOk*3*0;=hMFXwTvk~8K_!zXDTtg)L)sW6NF?fjK-g)gcu&PW&`jZ~hzF?+(nN0gc*AvmT$1_KOKT#WN(3<mMxPwic=o<fUL|1{Xsvi=EC|-YtJKhwpNW)#X=i~3R4&klstz|fFlRXg^TT;pgk1`Q^eGX%A#_}hG+Ri6rbR`B4GThF}k=Q<oZT!|+lZzGTGuEn{TRIN(%DujXn)*TNxxo;q>$?uQCz!TcgI?w_SQxr-Dh-w=5(}1=QriOi!_*sx5zgU_0k!|OJejd5kp4Wyorrh1M;&^l3QIEOsh_#3Pj<j#@JV5>1p)NX+#JrXxC_)6lg_Z;9(#mK(y{}Qiie6)6Z~njqHLn>NIB(Q_X9kc=8SM-7We0<_u&#wTLbCZ(>h?U-eKxmU}reqqCdiEIP{_ehHnQGfic}V&;`^KE~YkzsKL#HrXX<?c=x0uE#io(%Ts6_y^NyC<&l)_-hr@B=p5wU>(?&h={Gl@5ySHP)7BTNgs-b&srME-@Z+iyPHU|v`S|nt7sgWG=Sk~ZZu5cL^?N|C8<+WZ+}%i@YLMQ6DU<!^?Q{C!TNEe4Z47tvfPi=4;$S4dg6rk^$DQTqvd>3(QnJi^i{T!v?<25M+=k>0{Q&Aun2<tweRan}Q@x<**|CLn6941Q&Ing^Cl-G{x(}`F62`MnvS!=&!FvF{^&Ld~=1XkAy;hpgjbFlfqeAppF2c8~V5C+Y!tFn`7JvAC8GTGOg|8fX6Nvm~`Z)8}&qJ>#rVS+Ka+gp3=!@~or9xpm!=0ZTi*NLoL^zw6YV3Qrz0~XADvTQ<jzh_Mxqj-7q&8zd<ATpbZ1xWFwD62@o=Y$ky<hSnNg3yN+Kkv7?x75h`q+V>rQt=>f2@VF4EID@%V5_8wAQE&iz9?DyA#ir4kxSLhd@b&tJB2s=f(Djh6YFdi2FLvDY?fI)Fek2*f`Z1>U*EU1QF@F$h{(~X&`&!c=GMwkL1A!Ki032d5XafV;Zqn$z2HGk$S>31yGk2yrAegcgm3ym?aU7jJ>*)#DM4LRj5-ovnbOg9#Deeysvo~lpnH3v>CRg6X>>S4)tlwK4ED2S~iZbH&^+q6P_aRuG?~mD4EcQwDf<<Nx2uo+Otd0F^Il9yp7hdQ5x4w9|?QQ#Nf|l2`GAZ<<R);Jg=r_&n;shn`_*N5<OVI(uhBIlnGqJaFg|Q4OX_|lGm1TweGvpvW^s14y!29vuu+XZQQ#RH+Rrh{Zw0Bcv1H_ekjHix-;CesmVOgR$Sk$d-ye_qu?x)gV?t3X3&S>hTS*ETKheAeFy%)41-(Z$KfU=#!%!p-0yqKGxX+vHokJh{*p0-6<kZM=v!pZ_kKFQrWKcW|2g*q^ZW8GS6F@p!+nV_=e3R_EK*Hh^n2i=SeEl+IQ_Ywd9D45w7#M~ii{~4NqP+orN<6iM*3bnO1-I6rk&4qLGGR0oM%K0l{+R>ob5+X3Duz{hHarn#;DL~OZp((lWnH-jn9jznL{*bxo&op=Nb>n+eVEZ;V}c@PDS>mx4g2V-fIEcaNAglu8p9MC-$HfgX0h`OHfa3^Y1~el69czh^bVJTqJd2P;c5ZEE(aJE!L$mnOZW|+<>;vQ>UmyhbZH@1L#|mJQ2?B`v&TmVJ302#)q!>3Q4IukEjd9eQ5uH7f2oUw2lNnHc5iLZ@lQ3v@{}c$~h{1Lk~L6%L}Q)X~VvOr|w;d8|UWJszYCKZdje9o<HqIXP+BKwAR|QAfMm&K0yokr!kYf5o}$07&lB1RDUEJnJcXNq;ECUfVSEmhQ~iNWb0w4Dgl34(lDg2ELS+nb2#Bd7lS?A!o|`oH|kC)Z+&b6(wFrL`|9_}+d<9KOU49aePArZt(UOj4VLPQ^wId36WGTBufo1PH}JkTitA?jna=q3hSlhNaKTahbb2rPrOzPVv63lpE5nT@6L5aqKE!W73kg0aQiraqiQ-4^uwbtzIiU%-_5%aNZ~5V99$|lqS{|;!SGwuWwr@kODu3Cq&d6Bv&0Dn`P;igBYB-6fd~zw=!f;YpXM@rS!ZzcOzkoe8#D5!QSY`trOmk!F+_EM9ShaH&qT%zb<<#)~cPWMb`XGIf6a3E9*TdPqc(Iuy5_3|}0O9qFA=KJ?he6Ig54O%_Jz9nDzsf;;d%cPV>nHR9rk9>@qKv%QI$TAl;DkSk)M2L{{lOcPlN>j*V$S*#LGT;ncde8W_e(j2v@h*=H)7_@8HB&VEy7hPgtd45Z7u(Ty8vll&f5XReEWT1EoUX6;Jt=zpHbinzU+V$#BVP+gR^&`G&S1N41jQd_BwW=FW|2qH4nXaQ8SR5xfds?@Tp*);aZkkwWt$sRpE*@T06Xu<Eh;m1>YZi$GiB_jIDDzo{Rad>(`hOKKx)UJve=*6W5`_1g>Pbt0fXVk5=3iqods6`g-v6AZ0u)UtHH!EW7ht*Vln>d*H{ujDQ9!2>!MG1ZxkP$Q!h-b1R<B!yY`=feZaBd5#tq?DZt5D78(!4p6D$J<Ayem+^D3XU8DxSK*8f-@O(0yh}eGscsATiQ#;XrW*8RxViJT;BKwB@V90BU5%sQs-@D{kaDpuMFRIW_5Yo^4E}iFD0o8iEH8ft2YNBw-aQihU)AgPOg%sS!=JA>PC_8M7M$!ciuSi!b5bJv2r@41;Ql-AS6ch842OTmW(W6w$BEv>s(LPd7whE9|9`xT)%0#@Sgnj9`yS*srv0;pqrj}~SLioZ==(PvTnE>|{qHzY-+a;7NHOI=wDyT+`Z!!YOp|?|ee(X{$XL3A>)<-L{~aglo2eCrX6qHslhIw45e?HqV8WLyu)XfRfu%3{t|u9qWR|V_mQ2c7M)bcH3@txr0e;aP!#k7FcRlCdU2Qg!mZRjRE+@P!g5i^oS->jG+{n9TC&KM`ILeHc>Py|ext#Fb83bcKWP!n5qKz^pBqE$*!Zp)9FGo@LY+Z@UJAsh%R~D#y#xuGT8jo;E{=-ZyBp^leU5U|Zfv~=g;p%T0nWyeRIO)eR1@|u4P^pq`1TiH5-g=h>bOtmSc^Aea+!5Z^!s#`0sm>fX!gRAgGmpyxA$nbn&1=zjQ5#QW6gufFpmZm?5nJc_!z6}#@Kn_}BXH|KvM2raU}1QLGj(Hv8&T%s5BD+LgkV);^C*-{992|^aTihpr@IknjQycI!)07iGxi?z&-`|)=v8n<Fojb6;!3<+762O<UFt`DH8O8Nb$O8yZ5n^im~xD9CH&k1Vd(oTV7m3JQHDnx!p+g^Yi3LJr$GI3;_<Q|h%x7vTO4oXeFc5@cFCpHW^$+GsD4boI5PS2l*yNXdFDpSkI{UoO5ST$mH(0)p1h2>@+25~GWoY;X1QU87n*;6H=i#IC^x&il_GOWmJ)CLLtxLZS>Q@95AL12DCd8w)GST;B)Rh3Qo_wA1a@Y)5%p7XpGzoL@r*Ki8B<PPty@Y=o*x2f#&6i>{RYarlm1crM16BpWRcK?BLEwg*ARZUJm9Ad#b9?OoySl7fwocpoKR!IQNZn6Ltxe`VQoY)cy@MIp4Aj7<Q>?n$+v~|yn{e3p@vXQT?HSkD+b+<FUcF2B#UrW+AoETaw(v{!DHgngVpeSXfYT`^v=u8>w<6<(O-pXCJ8{x=`rE0;RREIi^0*GyK`5SD<B*-ONO-Rz6C6Ht0A^b^@etV#Xx`NNrSvI$_Q6bs6gIzTMt$gKO_!C`M@iF#bBcwX>|Rp3c?BQsgk+_eZeB?4l%gW7xwWj1_SN^;};itAe?VRFY?sbS)gcWDRKRwA6&h<80a3%Hs0gd3*ijrYm!r@nF9BxS;VA$0r0JRF>ncbZ*0(A6X7m?&?03w4F}k!y+l8+K<MSh*jw0TOt-t}JI;OUwa5tb0bqI8oy78If$-~crhRE$$85NTzJtA2p%3{~c^FXB-9;EB1VW6l_x*IIF`0@eH>*mOoRnw@_N+}K<eUQFXiw(6Ex2WDIjAqf4YTY*cJp5h)@2<hcCYq>bG?ee^Q+s8pTwc>bVt7YA}k%S0cf8j3HZhbeq?m^t+{99mxk)>HR6%*Q{_%@p!7U3SiuY0g%pF}n>%yeugW9b^NZ(%UxsIbgeSL%u#u}^d3Z5s7!i=SVW<qk{oQ;%VJ#3Qt}Xz?if2Szg*&u~ECwsR?&X;s|AKJ5UMGa~y?hY)^(mqB!X4@`+~=&@c?A9y;rjF|5PE(+0XEW)37IetxME{5cxzgbcc}JT!9QuAsBb?15r=<ya}3<q3^@^2whAi0vw^Pfb@A206Ue*JaVqbyj|^9kx|ZdX8165<cl<sDD5oGj2>ZZr+)th@_T2mm`HOF$T+w_N)}D2d*l$;c8@_P^e!ddr2F{M)ePTGP>)|c-XpN)T?HH6B`Y>NlWKXVUO^dzkIR|)q&Y|2fisp&9(+vSF_SOx2q(3eT<xb0oaYb>&)%&oV3d5}^b;LW@p<GSjV%A>cskJSBOKaC*BTHJ@d(Ov1aSXWS*P>t6%tO32Nhntw-;L$YJ&bPQ226d4n-`-TSP;kk%y2n(0$cnJbv}WQ<e{AXE*pJO99iz`TH<}OM2M}vgmQ=Wr(q&{MXy3z{H9NF!!H-2+}$N-c_MrBGs0WA5TkYYFfPiS?Q6*u#j)ITRf~RhdOEzI*C=<SERXeTnZB+i4vt|eeytScu0%!QB737}N3__BR^#G}Tj~6F^ZBb{e_V9l$fZkL;yCUrE0DU6a=R~@>5JO;ZQQyRPV-egR#lC1>&_qH|HW{nZ-P#&Yl>s3ssJw~|5^J)ebctGp6nf&P312g01h>{LBX|dpy%MOe3c((kY^r;bY-d9gIr3cSPf)fbYtJKpU~rp{uEpV;fC)~q`-0krFx+Y=oBvIa+L^7dSn!Ge?4J*ck1ebJZizakHlUbH<&A0Q=qkrm!V{g+}AI>-IG!oP)HT{-y&45y0Z4xY}3WMOQLHOcJ|by!hw*=wkL_SWLI|01F!l%=J;j{;&)?JKZ=@8QsMdgiGrQ3@C3u%EuV(Pz+w8x*z#F_>gY!yHRNqHaq*5T<TKphPBz@x3u2IXFZb(9-5ps#4SyC+%%~CTcd6%i-aPwagey<&PMOZhqsZKF;!WZTwtXkkH|ig)K8(cCcu<L&`SAqR=c+$3Zjd|7XSj-m>R9!iJqYJlCr2qeajA#6KM_5|gXJ#2oP*8Z<$%O7^;s9{!<B4mx^@&1=j_4iC%;bxyY`+$>>WNSN3C+?Qo)DQ2(^puY`hblwRvwXdL#PH4w0u8-R4r%!xCcSL~;9y2Mp8eC2<jncg}7(s@pCum2vR}G2zMz$YVI}j?KLL)maE<5!Z>TxSvDyG3pHF`>bHww~njCtBRFI{LYe-qKf5ns4IKbfwA5SR_8h!J$}B^M)chWCZGE-`JC5IJ~v2zBKI%LrUu>A2A0p<*m$iKwK2VneMtUk#eO2KR%BBPoU}pt3voVY>6+`G^57zV?+{<f*Fo8o`;&nn`;!|RuT5h(Z{H*h#P8qjZ=$|=!*~K`%BUfb)Y&2wW?I6GQXD#IITPLe(6tt&g0Y;yiP})!ZmUpp#aOtN;o=ys%m?L;OIvWx+YN@3Vz&t|n~Z~N815d!&G14wdk8qZnS+=djTNRTj)yZDPNSbQad8#OSwxs|PR$tzN2~7;uD>%L8Z+D^hO_fTxhXHkaPn6SfGfwx2{ZRjfa(mF#&BO(pj_XrMx3A>{h`sUc%ie66|6sv!>bH;-nG>ll+6Yl#asPg089`Lud#xU815OvNiIh@hX#xjIH?~jdY>TNJ#!*F$8ZY$or(QRQEtscea?VtEokkPD2&UP2!#wchT&3|pj<^?ea@miTCmf(L}BoQiIB^16Bw>;G0NrU>Ty>1YQZmG5{2pCC&FV4H;duoT5;xFJ&wJ%7IgfUD70>v2y+<Dn&Gy$;v$swIl~gQVDP0xVM@(Jc%0$P8SY6dPW7@r=lNYNc*H+Zc;d)JNHN?9hTGCA4s%AAAV!zWCSA@moGinow9;icX~6mVvLCb^mLR-#Rjjin!x^;F`F?L>PT$i0&`%>?7^`gsC7APjm^mLa+)(bz3lmON`2Z-a+9Bl4m;eVeTp+_;az{CCBZo7zW*{89e7n$Y#dtV@;RZ0=;gu*S&@<<(=sXyD)NK=9^&7{Gop4yq<a5Frl;cew%hA==hA%_63g1PIg_{{Jn&CWJ^^d>X-$Z>g@P0Cpz7T`14^qjuYYpMmUg>afxhEm?Mc3wxPf8{<p6bF*3lEY;myOvqL30^yPph>}RnPViU5rP;AlJiW*kn_9lHp{|coKE4C>Ks95tmmEhb0$|lXw{i9%s033}?Cs<$es<O;jGyf%6v%$fS`FiuU<5tMDXJ=AxYD=$(W~XKiR(TR=)Jvw*%Lzvn%PISwc{M<IcDvv(kj)TYV21!LiChI>)zN#xqLTFdo*2hk(8KRi)VLeiba!!ZoE{-P(*X$s1fXm2O(e%6A<tINq5RukYLh8uOslbAmd<uZ%65!IJ8;YRHWvUs=^RAjidmpzFK<58~PsjbAJE}C#_r}JdRX(k65F6@dY@pWt~zhkx%L8*P=44F#uRF8?UzGpgo$Z)+{ad-5#5|O+6!o#YS<fL8`;a7&sVK}8$+{5EriPEoqq4V(bWdABF*uZc)4ELlJHz<1>Ayc9WFHfu>M;nRbn8(C%s8t*n8C~u%x)6*mc1^md|3Vk-VR3|PNq_ieS_$cRe;hPtIEv9ZsFlw9b|et#qX)s!FG%uZttE6}xDe*N$=IRX=x;lTK8i!&_EUM}*BEmc!Eo3)Poi)(%6;ydM0_4O3^v+v$&`l#+|O`cXFUmn1t@2_XAiM?(Mb5IA&XomX9k4~hckKlX&K5bk4z>KBXr?=jdZeplz8pmbW#6UiE@9pzlr*$$HJa;inA$wXXRXAP`QzE+@g!8DNP0SW6(8Uo=x0shSO)bjd^+aL57QyK)GgncNq?2I2g8#<xanQ`CCq8Z--?OCR%HzY4mG*_ZUu(;Uo>K@N|ZodHv;YIg!0cc?<ji!wvZOYwmuNy#Ti}Eaz7JdrqXw({qP;B3(KUs{NziF3ZhX{qmH4tzR>DwK0R`&VP9MOYTmSe#H;F8;Igv^W^vK!x`?eswF1ci+8G4?H}!F%zwgiThxCahsa*oGZ{?OzN3wQ)^E_ERk+ArtZMBa+gChRT~EaMb!*EdH|2py=cv>!tS*|}+s2XHlz)bzyxLo-FXEnk`*r);%jcgt(fQC=Gl4z7gC_sl@13SNwC_yCQyEUNds};Zo7y*u;a<7+<BROQd-c!SC+eF$21QVfGquRk(Hn@HJYROLV|CXNe7mp}ddPjonM<gW-SVV7zLC&9<HxQ|#4eX&29viT+?mF)l%e_?q2`rHqH{t3%keXG@R6ggB5M&IE+0sp`B)~DJhO@L?HR;!yPj9$1C8>LaraVz1Z8KHDlGHdOa$8nvuhY*77@G}m$S&cs!@p|y))c}hX=<HoyUc+_C{-+#CMk7N4Ru#Us57iU8rQWnYi&Lgk6iX@?JIn?D;@s{<Zf=wUE1GdC@m4ig214%5t}gyBLIiOU*&%bLywe*4Z34JMOfMuuc$jS4QsQue<*W@q7Q+5024zM~?eRLt;!)D68{ZDO>EG&q+iV-tI|+O8GX<9;_en$UBsc!{6frUt^syVsCR`0O5b)07oG~g&=Lj_7-u+;r<@RNE}}b#t}Vgws2G;Rfv#vV(znI71qmZB*MMtKR25#=*0;iqeaA2i2WM-aByt58RGZFZ9pCot|)x?QlGdVAm;XmDj9tEB!iqc=T5iD`)|ezgYHivvW!Alzw~KEd~}Kz;<x0;FzR{FBH@$9g+y{gAZxE4v&Am&e2&;V_-sBUJzSc+_1d4f%JFBpiB>%f>O&tQ?fbj=+?ctUQhhUwTt8+L@zB?Y<rL0(;hSm?BIl!LUNklF&`@&2#VF!qk`H@+Yqf6Rww-n(^U~OF8>mah1Id(<jYOWc*zdjIO?;uEkoQm8C+eGbRcDO$^Epr2$ExM*$`4`J32o_~%fD@7g0R2h(h?FdBU?qRJ<j{wao)6Pow@CC4@ZpP2doz8x5stqmBKd}(6e1G?l#6B9yqyOzf;Z*;%CdL=(fiVDt*J#a#YW0kIU$)&VQ{K)~??dN@=_emCAqAS+pLjYU@*e^$_Zh_C)KQ^pkt@b(h%dw`Y$Z{*)gWu%%u7u0BZQSJ`i9H;%vD-tikQXN_);qi1d5KTJ?>*YD8YLwWACwe9+y=XHjE_pK`49`|?iIpj<Ze{R68cDZH4?f6DfaqV&vR;oOUOUw2CN&7^7GxWUyz4D@I4);|icd3RUdydzsrsIV((ff0)Ig!n-b$#%vbF==)iPo)0ODb?ZlD1*(aSf01dGKU7)}CK`4GJImvWE93P83HE{WHAf`%JO+xF3pHyg8!+x$XJwo21F}uI$yWUy0(ayqT^C+l|Ac5X*k3QK8$OJ+B+faAD##{XcP{J{M#b$MZM-)^7VAzgUJReL2>yE{b~2{MDB>bK5&_8;08BX;n@+?Zq2;buvGyKAYQ~->X5Y*xux=?dm7V+kt;5P~*2}@9*TZh|@ZG7@x5`g4dqkka1G{`+Jr7?VXPU91J@wJ^3HCPt-SE7WRZ0%=p)F<y^{DG*5eKESf)13ud5e<(s)#O<Z=vF3~udS~(r%7PjJMN>3Jz&#4c_zqU7%;g*Z$H`dQE(c1f3|6kj)Z?adbD=nHIQtRfR+yaKnWH>v9b7Q!_zD^X)bE#!^zxF%5$)2``9~-a6{9kjko9vOM@qCfJZ&API?3=hc+KDe}pH=jq<2XD$pVcqb@%Mg3x*Qzj$QAj0wy>?eLqD}I>5&<$i{p~tb0T|#ErwyD^AWxH&*x3Q`Vo8H)@lD)zwc3f*}V{4CjZ{Ah#M!F!|G?h2j!ew>1<TX#YAz8S^DeqJGaT6>cdQyt5E%Q`~Ge|+coL3DSjr)<!$&i=hW1`LbG`Gym{fj)^8faEn~RbeIj^l{w@4x?PKSg?(2bi{c_=o;4m<`)RUdhN!X`h%JdSXer!lw2CVv73orVt153`YV#oUl1+n~78?ur5cKgG4pldxs$lDqY7H?d`+8e{&j$d(DjM%GJ9Slq}<%LgDBfzvzUaY+=ej>Mg8oKr?x|<X*_q|rMdfj@U+24n?cX8S!-m8W~h~JI9P7<3-l8P=UYygM4__FrOUOncY?@l4t8`a%@#Pe=5ii8>)K;j8scK#5le1NwlY&|+QzgffC`dYCFhO7q@My+Kzt9(nG%85tD9{PTdik=!q6!~k00@qqG=b&XI@IH+p_6p59lbNIM6jgm%11zG&ds8HQNx)A?#v$A!Nqh2!X>HMk=^o(F5iw`0n$CMV&I;ko-iMPx6)%dM@414o96xs6^5jxIuJP>~!r@Ui<fC1$iX0@}z}jl@eFDEPLj2u_Dnu8f5h}vF$A1)kbMOEKQsVjC`_u*uT5LkDa}ImWh~uS-!qV5P!Sc0YdvcHO=`WX5Mb2Ar*ll9>nf}7+OTIwjtuMP*$e|<?{8LF5;`iR8;lO8|iSV3dAjtRfX3uZ_E{cDD$y<cm@p1t;<uF4SPlbTteyiENoof17;||@>>+9dm=X+hZfI4X};fohxz|YTv)$iKt8NB?Edc@w*-Z7y5(Q2X2{V?FR*Mp6>$Co9T^LiWPI=9Cr5?r6?E*!f*46L8+$#T;Jm*c6VJnx^hkA2r&%2Cgs8UH@q_8DuQkAThU_^UAri0{^%=-R$ibr?QppbqvYPGm21a~xOdh%C1~?%_}l-&!e9zdgUod1m<JV(XmtxbpQ~ah1_p?b=)5aEcc<;l`-;xR`}F?)#~WetU6rRzAj$U6|Fb-}LLt`JvgT+tp>ziF>@0eS5cSZwX(HU#b(`u04-rZ#>}Gq;|OzNpmoXuRH$eSG3>B-k3<<t(;7~_P9oAMLaDkLa#l)W*fq=odyZ*#v9Nx8dE#7zuoikck)@p8ElN>XFl%AZ!eBp3l8x&+jVW%-qe>h+$o0@`TwMSP4i7yRLaK=J)yAX`NQzFLcC<Id+YH&!ij9Y+j$mWeK3aiCr&h;4zIAm-vodBqdk$|s$&BUE<OJ7M^3blkMW~x`1ViJ`0cgttwIt0)gteY?GtJ3Fh~)fo^gxUo;^8>bNJ{lo7%OPocs}=9r-Dzy*M^0_cW*|S8F#8_3OC7pq1CT?Qxd`BMtc1{%lW_)BcyA;Mb2D@Y>^6#zf&?cMajUcYd=Ds^UkiR<)ZiJG}GozTrvj>c^iKhkqRPx?QfW>rPzQ$(7e$Uj5yC{&7&=;9O#OyZNj%p#kU5S%kHx-<6&d@SVAfv47G&_FeZ>6*`mqoOr#>i2f0M7RY}-OWjpbg`fLIB75^JRPIJE?el}!uQi75e5DMi=ANO3D67It5(f}&i<~n3TDuFF_11*Wn{XN^4=bn4<W-@0HjQw#d<D8pQw3~(Y)0EYEe633rIc<bRrtgCF2c>zlcOCxdx1$c1Z}~k!5-@ps;`tP9Bcap;cWMJqIW9|0ESl}ZFq<TMtzH^-VG`+bD<ovj<#}?488J=4v61tK^vA70f}mgnozF->o=+)+@{Zxw3_5-@L7K>eOk5<EH4sLr#`8`8Ljp<D!Cy^8+X<Nx^u_Uudo8}*-%Km`ltf?7Y#(XXI-S}=X_l-In$EPe3<`Rd$m&1boL&75M5?T_xX_rs>cZ_8HTI$M(v${AVu3w!-3KVOWMRJ4-}m*q9!nYUmO~YaGGOe=<dP#V5qt!J={O<w|*aRI@6t;Mu4YZAiY+$00{MH>MWznklOADxAmwzeV7~wq?Zu1?6M*tuXKv4XjFm052O(;{=E`?*RUr@EHb5o{3x*V@@cA{tSWRYuR+e+#n;{Fx<N|7VB{EDVtX+-H?*8GW%8w*kLJt7nC|qwq0(T-14G(O{WO?TeTGV9@^97JRD=tgrAAvW|3n-&HK4ypmi;!LJ&vi-Q28|hDsfuXvK;VTDyVVYRiQ+QAHu!N>p|a|^n!@<F`xrFp8-}56_h5!=?w`)_v+H`PAjHABWku8(y3WzKti7i%3f6!Uf2|Y?s0WPg<cf%n79{cL@NdVzQ2k3rdHHF{aR;9x=Y|J41YG4-Mi$ouruCUejajfdO}8T-o?!_biv0-c$KshyEll?;FbRQ^wCIv{~#Ek_rka{eQ~iYuDrmJwRdgB1pcx;XVLdiZ8Pyue>r-|>J7Yv53^VflgY-q4(g5gmG)T43tFf^cLQErQ0Tz!U7~tpFuwlMNQ6_V`;K`@Dbd~Drefa?&t&bze$vAoXImk4u$xm3K7N@py^#BbcS&f^as_sLe5z>%VsCEASB#g^mDYZ!kCU}C*m#qoCSZw?NeK7mb_Oqq+l|(;E!D5m6?5xOad{TIqmX!4Bn{yk<f+j9{U2emfp+X3SNBT>;eTD<jM!^18p6G^UzJWU+Q`kkF`bQ9-6@KvTJ4LT55uYa8`HYeZd1Ns^X5-y<A@(Lo<ID%CBmim8G=tp>P`zU)a$(*CboBRU>Wv9;}W9Fpdb?eJg+;wdHW2WyS><+@~1=CsmCJ_dwEA@@F#k!(yOJ0;v>I^?HN?A*AIPr1c~>OOC2wS(M39NF7E=Pi-?;wF@(RaJGw{A{>cfvbpgt>*N@NGyKH+_zuwLf{KB_V2$wtCoPT+`B7Obt0&K9B1Iyi7*2r@x;34g^yfKi=(U+$KddlH?q2jzcX@3y2eThDgbdfQ|C#rOz8y^^8<~PNAtc5L<$1erOBjXdv8D{+JFJ$SVRZdvo=sB!ky@{K+U5e5%bbW~%_KGV_3pKl7y&|1hzdqkb<M&qgLHsISSb=?G`dfA2YTods{<dkJHRh5ei?{BZ2e1#+EID8BfagHbN2jxU=R7}YiwhE-9Y_8@CN}S=JDd<;7&DVvXKBaoIU{uA@HDsVL^MptUs0E9!@)@#d#doI9cwRV!4UrVM{5!8oy>D;){2cFx62G_)Selvy#n{|`07kFw?;gxq53Cn0`8^LsX3GFSuV{*5BC`Egyfd9#6xO>cMN#F#g_7WYtQaE_I{!fZm5ku!(O@eF17yCR^a10jq(Ve$+piUQx<z#G9B^z%Ig*-(|0>KGtY+F_iZM-7n^$0C0siV-B<38<_&7!!yUkU<y7j*3<uWU%h!YO5D5!}o91ziI_4M;==dp=VvLyUaXwEUJKGuIu(4OE!O;mI>Z&!h{ip-$ckn76#^DS{_Z~WUh1zVF2tH1>re5Sbu)6FYavKkk-i_!Im2ibR8kY!qsaaF=1P-h&d&ULh5f$o)y~^HKshgt`L7t5@^*l{%Z_ne+{7ZkCqwf@$T%(3I#)C@bDb(2Y;yC1ne#4Yx)}i+@zFenvKiUCG7EYxe%@cEv4v)c8o+cnVUyi>;Wp|4OhrZcRHR@u&7nVribw!rQJaTBCd(@A&F~E1dEv1+uK5v^loyI>LJC3w()TBpLbu5!FpQlrn-|Sg#N~#2YdT1#6?#tyT6fLt6=vmL87Ma_#I!Eo(#M4525M3I4UQ$u>!a<|IJvB5yoTmrAC>o43_eMAciMP~Pr(m!+VkTwH7w7#z*?oBO@~sGWhVvH{-OU$tTIN9cFttr|KIWus$G$$WZ8N5IxfzLz_H>Jnkl>5HMSBY20k_-iwIoUHXE|4uM68+n(o+p<y{FJ5<sCfHJ}tYPHSiBjT&6>BgVuZA`PHvr?Jdq8!WVI(y^)Swnc0SmyR?xf;*RG3rPu8DY#)g>d(S-vuh~87F7|{iew}uOwc(!s<;41ZVW7m?746;hwDTdnHRsZ~GpmQ$%(xcrfXT)-dnjdI$YI+h%bmq?qBy4OUEsCWZ`|loRyRk*O;~fhWt|n<>_NqSxsh#OLBMz3hbF(8j~=&)<KdF4tUb;7l~{AU_GNF{*mE7@&Dy&%K@V%T7qcm)O}wY~S+RQsmJXiOVz2+jRd{QAg#KSFx0$<)wJX{qi`=K#MwdxvqS@a~dQ0QY=k0psLA};o_6#pvlrM8-CbQ>FWbfo|+ctY?>GslN&&SrQb}fEwrciC#H)mV~dmgr*-O>_==h7E#xcSM_n8@!D^`MsXF~|9J8~x@sE?{-OShuUiuj}~>ZQ{Mf-_P31++xdXjw5e=e499odv|B!NRIBz#w*&J>yYF?LF;@G_02^=$-vh10?}}DJhYcpgzHozs7~_Qbhnx4_;>U1L@?~(bK+B`6--zl4?C5uqXHGR>HYS*5w7>jSaAQ4BxqPQ35Mi#fpufUDAlgoG+lfI;VwMg1kSlCgOI^YeNpQSujm9*q>45z)0;xL;pPz_esOQ0l|KbGZj^;TBK#@O9@@0Rql*YvWaAI4t+YY)02{cguM8xGK2#44ZTe)|Lxh_U?+&Q4(SYMP4L*D-1r_U8Q%SwF=}B+jB3!5J1t2on2t0AHg+8ApVUepR^{S6HUAOoL!rdh1fmM=Y!0Z%TxUy6dQd>PJzrNb^Hz0w`zovC@20{A`0XNVV-fsN8J!jGhL~k|%_Sv>@`g=*Jm*GJzW9&7mqW10u%?1JwV=&>aE!^~45~fwVQ;!*L-Wo}STeN8|kUWTkp&M=C5@)I3`jsyZ0k+qT!M(bvaFL-DED2sijb(IkeDNCL+~s4y=rn!M`-nAss45LVIIg8WscF+jhi@Sqxo`(4<Mjv2%qGKYFJ<6ilOSrfsy1zW`4qz4uZjb``pSVJdnUnAeL6#KQ@-pwjpmEatVAHxswE;TCc@H^E-?K4I%*h`f5A#S5$?8WGH{Z<NsP9$f}c;y{WhQJs=eUm+6%;8p9%13pggpTjG%hTY14-nh9O*T;65;vf0?K|H69LnCJ*-oL{J#RZ61p54ZY4h4McvtO_-XDhb2G(_B4&4avAQE%m#$}=8z2H$}bY87staTJ$~Qc*!7!bd-(SXyb8Zw7{&e47|M=+Gc7p0qQhp$wb-sijki13tMJQ(1=!anZsP0#c+v}WkKx@*w6MZLuR`Lvkk`!Zi~owbznO$^*L(7K%XnUeot`{k?TN<wQ?0`Irxs)AxO-baZ1oAR!Y8Lz={MW!w=aU{!+U{@0p8kR{BA{Fg-az=_|0)tMOxs4bybkD)-}EhZ(yER;pBdwS$m@SLr=F}I59#W8S^OY>!yE|i9>Ue9KYFb<-r7Oet8bUEwZTOc`@zlKd_eD%sKDW;$1aI=OcaGck>LHcmpm1yqSAset>t};TRHc{LmQQebTG&-Glut`hgJ}`Q!345x-I9%Xxg}ygj(9(!!-xH1ZVILS!r`rMm#X%y7DA3$W&RH-%irGI(bZU7pUU#FYIw<gzmoyu?EB`B0HOmvd<{dOv*Xy=dO7x(%ea`+8oEm)P%{IU~65a|;oUdq)TFdVc{~XgiWWJVk78-MKZq3FpHQE+b<Hwj{4uc;sab_t<N(JueAm{);<(5&d@Ee9MaycojmGgDv@YkvTWR)EbaD?%W9C4P<nlNM*NhQi4VNg@>~dohyz+V!N61W;xNTg*)40iT>=wG{jzRjT-NZcW+^M>`cD;4{;m|cBk^p?z<pyoR)jQ?SE#EurZ-GHspiYo-AnKj!Bk9;^^J;8Sliktir|_m-XK_osZtff_PGqscq()TZ7L4pqERm>X8mMtaf6@$)_io@HE2F`G{~!DUfq0AU^L)1Fr>h*>U%6?HRljr%i~>@iR|>+?PV)_?*3<?9)7UJYCk`jvrHf0^x>!puofnCyAT0_JCC*omqS3^G9;ejh~D7mDyYbvYwwN`sM5bCDsdAdnaG-*B7`AL2^*fEFY{HdY1Scm<TkiU081G$6NaIwcjCrbBCM&W5OzluUF#1_yG%9zmrDz;6u^_5e*lw;(@Z7ON2I4*P}`ovi4L~hhavT8e;FdSq`{wd7Y4rj01CZ7qQ%WFDd>r2lN^C!tKYwc%$3I)4g#Z-*XY`H|A+Kd_p7otiXQJF%Y--9x>`<94Ji`bK_;N^F9sBL;qhwf}>!;`3J;jsd$iiK<xKm_-?*N=46Bm*>n^L-5wDUq4B_EyV%~G{>O0X?&Hw4)v{Tj%tZ=pkKF~XmMvuSAoI46uX9xfIUnQp9R@0t9H8{~fKyHS<*mu(-^#v(#2cJ@1e6)cg0uR`z)<MIa`Jlz3a*bCiR_)r|C$Y|y1yeX)a(K0Jj9&Gqhb7IvdPFZD782Nn6l<JaZx21+`77e<t*lN`R}fx&vVb(<pV15Dq&HR0xn#4W_9^=O297{#v;0ytrddfsg;Ds(Nti6YCb#PoMRWq@Aobc;oL``1Szx66C(!g2fa4UV|7_G#h)Kjg637X{1Px}|2e|G@F18nc`mzW?@*axyomP_$lkt{SI&S#QhCHj)pRh+$%$>>u~WBr@k%oKt^4LVr!DOG+;!Dpe)D|kb6o#6dz0I~IK=*r)y;0<9%YucnYYB0on`G6e=%#BXY{#W(PkWfdY6!$r^P=S*kbRzzEqpN-EEYYvE%(mYPWgK<NYx@-`kAiM+g*HJ5@P;E%r2}ue50&|5JZ<etXM~#G2=k#hc{?tvS<>OYHAm9VJ@k!_{LH+V~x*GK`%!)KrXa@%ysaqRo7ypI$uM{xdw;7F|+qTD0-Y`QE_#wHq>`#jnHC2E6q=+^R$zXO2@wi{Ig$zqgrJQK!1HegzZK^qc)!C}p-eALF}gvGd66l*249ng?w;q{(gFzO>VG*!k3l9p7`B{YI?TY!in?k2=;)w|9p8=6PQf9@Hj|2Qr~-ye3N%c+K%X>i&|~x_zDNkFi`~Xl~1V4g}}5iR1o;9V{2_Ij%()wfcfK+=*A>`M-u=P0Rg+lr@*z%<J_`p0mG?JHMUZ%(=aK+J^IYy~x_@nJ&J6BI2GqOl@=j<mS8qtewjJTUyR<S@-fb=S|c%&3g3&k!q*OBabJMX&HfVV%kHRZ+ML!UU!T}+IXcWICrs_Ono$hd`SjEg&%ila$8$2V`ndrF`p(Ml_`*CE(OBx$8XRZAGYQ8%<m1l-WHNlhp!1=J_>{%FI=J1hhA^P`SkAt*32m+zbC{ClV1nIE8bOf!Sc3T!?ix3@Jk-K=;$cnl@Ec?J-?Ekm)4e>?bjEWUpPT(IIb<y_!0<{x##E+SK4y*5}M%H2R<1)P1ekV;RwG9nr>*zMY?K&M;mygcG@+wA)f=`h`|+f?%*43xWNKVP!P-|OH=xBmVXR{;Wibtqty+1WGn9fG+w76QG{U}z<0K_jIxsL@~o7!EV_uyZQtjeHV{+G?y`gI(9PN%)3nl+uI(-;HHeCG$;zD3T+McMb)2>=O*$;iEyNBJg%GtQp+mPZf@t5szW;chdHys15A(jo3tFp;e0tFWWlfh3XP>DSvZG12i1LP*?MD9A=~c>x&pOQXRtt-CHer&3tCo7hkZ2=sxx8BWSaf;fz1@29`3xVu;CQK#584>4%oq1gzx(*tyvYu(+3W>db{6ovLVT3r;vJ9Zr-FUGt>0Y2Lu>w-OGB3@B={8Z)`?cdJ~8KanJ)x;V?A+i*)mT^jWF@9mi*F7;=F_}bwbE_(#4+j0ON;ZzH`=wvi;({k}#W)wxpiOMQ?V8siso?MVq;-s#S+G46lV(gUR<BT<ZoUSIYTiT@*jmro+D74Z?)`FNs_-JH~=N$;w+*{`^3z4x7T>2-{Q1?_T-4Z)`t(>+5Dnqa4k3%7u~@D)8yufs?<<Xw~&vp~rp8Uv^uoH>zvoz$7R@5~ac8r84>$OFBzo07^4*aKkVQA}cg_h3^1bl}|d^qdBNz9|xWx0g&UT#p?A^I^{O$vWiql70N;QARqA1YjG||(F!x^G?K+=^C%ALrn^IWy%z5tPSGbVq<bE?2DLS@P%f84hBt%Vj!Nhxo^)?pqL8bJg^f-w5D>}W78eOk-6h@U3u>fDXTg89GZY_Y@TO2ZR@@_9Osodg>|ufZDg)0f1}{I=i|1F6F0U{SB}TJwAy*0sW(H4e?!f~e)(Un9R~H+P9;{=bV=o15RtA@7dT>}}t?=E!afjm39MS336zK0UxLetSpHz~2pLNEeF<V)1Jt&2a;y?3#IJop|4cfb#1;3{<FkEABtGpK<c|f{$F_)nK$VELO3Z+<B7=FnGt}JKpyloPCOgZTo+e*~b&ccDxkuYu;gM+6~bXN(Pi_^`8XmkJvH)ngnCzBROv{Ur+J7g}M^OR_&ngeIOA0#i;;<W`*I<J7->%4dx8hMoii|-7W+@!%<69>>I^2mFGE{;cf8wWY(g2aCo4Q2vmbW9HE`ue62`&Qgf-+ls{U_3M"""


def get_embedded_cono_r3d_bytes():
    return zlib.decompress(
        base64.b85decode(
            EMBEDDED_CONO_R3D_B85.encode("ascii")
        )
    )


def get_embedded_conopatas_r3d_bytes():
    return zlib.decompress(
        base64.b85decode(
            EMBEDDED_CONOPATAS_R3D_B85.encode("ascii")
        )
    )


def parse_gameplay_vec3_list(data: bytes, offset: int, limit: int, max_count=4096):
    if offset < 0 or offset + 4 > limit:
        return None

    count = read_u32(data, offset)
    if not (1 <= count <= max_count):
        return None

    end = offset + 4 + count * 12
    if end > limit:
        return None

    points = []
    for index in range(count):
        point = struct.unpack_from("<3f", data, offset + 4 + index * 12)
        if not all(math.isfinite(v) and abs(v) < 1e8 for v in point):
            return None
        points.append(list(point))

    return {
        "offset": offset,
        "end_offset": end,
        "count": int(count),
        "points": points,
    }


def find_fixed_count_table_candidates_ending_at(
    data: bytes,
    end_offset: int,
    stride: int,
    max_count: int,
):
    # A raw "count + count*stride ends exactly here" test can have accidental
    # false positives in arbitrary binary payloads. Return ALL matches and let
    # the caller validate the surrounding serialized structure.
    matches = []

    for count in range(max_count + 1):
        offset = end_offset - 4 - count * stride
        if offset < 0:
            break

        if read_u32(data, offset) == count:
            matches.append({
                "offset": offset,
                "end_offset": end_offset,
                "count": count,
                "stride": stride,
            })

    return matches


def find_fixed_count_table_ending_at(
    data: bytes,
    end_offset: int,
    stride: int,
    max_count: int,
):
    # Legacy convenience helper for callers that still expect one table.
    # Prefer the candidate closest to end_offset, not the numerically largest
    # count, because serialized tables immediately precede the next block.
    matches = find_fixed_count_table_candidates_ending_at(
        data,
        end_offset,
        stride,
        max_count,
    )

    if not matches:
        return None

    return max(
        matches,
        key=lambda item: item["offset"],
    )


def find_gameplay_vec_prefix(data: bytes, camera_end: int):
    candidates = []
    search_start = max(0, camera_end - 0x40000)

    for offset in range(search_start, camera_end):
        way = parse_gameplay_vec3_list(data, offset, camera_end)
        if not way:
            continue

        lap = parse_gameplay_vec3_list(data, way["end_offset"], camera_end)
        if not lap:
            continue

        conos = parse_gameplay_vec3_list(data, lap["end_offset"], camera_end)
        if not conos:
            continue

        cur = conos["end_offset"]
        if cur + 4 > camera_end:
            continue

        camera_count = read_u32(data, cur)
        if camera_count > 128:
            continue

        cur += 4
        camera_groups = []
        valid = True

        for index in range(camera_count):
            group = parse_gameplay_vec3_list(data, cur, camera_end)
            if not group:
                valid = False
                break
            group["index"] = index
            camera_groups.append(group)
            cur = group["end_offset"]

        if not valid or cur != camera_end:
            continue

        if way["count"] < 2:
            continue

        candidates.append({
            "offset": offset,
            "end_offset": camera_end,
            "way": way,
            "lap": lap,
            "conos": conos,
            "camera_groups": camera_groups,
        })

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda item: (
            item["way"]["count"],
            item["lap"]["count"],
            item["conos"]["count"],
        ),
    )


def decode_gameplay_track_data(data: bytes, prop_scene):
    if not prop_scene:
        return None

    scene_offset = prop_scene["offset"]

    # Reverse the exact serialization chain:
    #
    #   Way / Lap / Conos / ConoPata groups
    #   fixed 0x280
    #   Sorpresa: count + count*Vec3
    #   count + count*0x1C
    #   count + count*0xB4
    #   prop scene
    #
    # Important: a single fixed-stride table test can match random uint32
    # values elsewhere in the file. Therefore enumerate candidates and accept
    # only a chain whose ALL preceding gameplay structures validate.
    valid_chains = []

    b4_candidates = (
        find_fixed_count_table_candidates_ending_at(
            data,
            scene_offset,
            0xB4,
            20000,
        )
    )

    for table_b4 in b4_candidates:
        table_1c_candidates = (
            find_fixed_count_table_candidates_ending_at(
                data,
                table_b4["offset"],
                0x1C,
                20000,
            )
        )

        for table_1c in table_1c_candidates:
            sorpresa_candidates = (
                find_fixed_count_table_candidates_ending_at(
                    data,
                    table_1c["offset"],
                    0x0C,
                    4096,
                )
            )

            for sorpresa_table in sorpresa_candidates:
                # Sorpresa points must be finite plausible Vec3 values.
                sorpresa_points = []
                valid_points = True

                for index in range(
                    sorpresa_table["count"]
                ):
                    point = struct.unpack_from(
                        "<3f",
                        data,
                        (
                            sorpresa_table["offset"]
                            + 4
                            + index * 12
                        ),
                    )

                    if not all(
                        math.isfinite(v)
                        and abs(v) < 1e8
                        for v in point
                    ):
                        valid_points = False
                        break

                    sorpresa_points.append(
                        list(point)
                    )

                if not valid_points:
                    continue

                camera_end = (
                    sorpresa_table["offset"]
                    - GAMEPLAY_FIXED_BLOCK_SIZE
                )

                if camera_end < 0:
                    continue

                prefix = find_gameplay_vec_prefix(
                    data,
                    camera_end,
                )
                if not prefix:
                    continue

                # Strong continuity checks: every decoded block must touch the
                # next serialized block exactly.
                if prefix["end_offset"] != camera_end:
                    continue

                if (
                    camera_end
                    + GAMEPLAY_FIXED_BLOCK_SIZE
                    != sorpresa_table["offset"]
                ):
                    continue

                if (
                    sorpresa_table["end_offset"]
                    != table_1c["offset"]
                ):
                    continue

                if (
                    table_1c["end_offset"]
                    != table_b4["offset"]
                ):
                    continue

                if (
                    table_b4["end_offset"]
                    != scene_offset
                ):
                    continue

                valid_chains.append({
                    "prefix": prefix,
                    "sorpresa_table": sorpresa_table,
                    "sorpresa_points": sorpresa_points,
                    "camera_end": camera_end,
                    "table_1c": table_1c,
                    "table_b4": table_b4,
                })

    if not valid_chains:
        return None

    # Usually only one full chain survives. If several do, prefer the one
    # closest to the prop scene and then the richer path data. This avoids
    # selecting a huge accidental table far earlier in the binary.
    selected = max(
        valid_chains,
        key=lambda chain: (
            chain["table_b4"]["offset"],
            chain["prefix"]["way"]["count"],
            chain["prefix"]["lap"]["count"],
            chain["prefix"]["conos"]["count"],
        ),
    )

    prefix = selected["prefix"]
    sorpresa_table = selected["sorpresa_table"]
    sorpresa_points = selected["sorpresa_points"]
    camera_end = selected["camera_end"]
    table_1c = selected["table_1c"]
    table_b4 = selected["table_b4"]

    way = prefix["way"]
    segments = []
    cumulative = 0.0

    for index, start in enumerate(
        way["points"]
    ):
        next_index = (
            index + 1
        ) % way["count"]
        end = way["points"][next_index]

        dx = end[0] - start[0]
        dy = end[1] - start[1]
        dz = end[2] - start[2]
        length = math.sqrt(
            dx * dx
            + dy * dy
            + dz * dz
        )

        direction = (
            [
                dx / length,
                dy / length,
                dz / length,
            ]
            if length > 1e-12
            else [0.0, 0.0, 0.0]
        )

        segments.append({
            "index": index,
            "next_index": next_index,
            "start": start,
            "end": end,
            "direction": direction,
            "length": length,
            "cumulative_start": cumulative,
            "cumulative_end": (
                cumulative + length
            ),
        })
        cumulative += length

    return {
        "offset": prefix["offset"],
        "end_offset": scene_offset,
        "way": {
            **way,
            "closed_loop": True,
            "runtime_builder": "0x446230",
            "driver_attachment": (
                "0x416953 / 0x4176A3"
            ),
            "segments": segments,
            "total_polyline_length": cumulative,
        },
        "lap": prefix["lap"],
        "conos": prefix["conos"],
        "moving_cone_paths": [
            {
                **group,
                "runtime_role": "ConoPata",
            }
            for group in prefix[
                "camera_groups"
            ]
        ],
        "camera_groups": prefix[
            "camera_groups"
        ],
        "sorpresa": {
            "offset": sorpresa_table["offset"],
            "end_offset": sorpresa_table[
                "end_offset"
            ],
            "count": sorpresa_table["count"],
            "points": sorpresa_points,
            "record_size": 12,
            "runtime_spawn": "0x414880",
        },
        "fixed_0x280": {
            "offset": camera_end,
            "end_offset": sorpresa_table[
                "offset"
            ],
        },
        "following_table_1c": table_1c,
        "following_table_b4": table_b4,
        "gameplay_chain_candidate_count": len(
            valid_chains
        ),
        "gameplay_chain_validation": (
            "full_serialization_chain"
        ),
    }



def find_filename_references(data: bytes):
    ext_pattern = "|".join(FILE_EXTS)
    pattern = re.compile(
        rb'([A-Za-z0-9_./\\ -]{1,180}\.(?:' + ext_pattern.encode() + rb'))\x00',
        re.I,
    )
    refs = []
    for m in pattern.finditer(data):
        refs.append({
            "offset": m.start(1),
            "value": m.group(1).decode("latin1", errors="replace").strip(),
        })
    return refs


def merge_ranges(ranges):
    ranges = sorted((max(0,a), min(b, 1<<63)) for a,b in ranges if b > a)
    if not ranges:
        return []

    out = [list(ranges[0])]
    for a,b in ranges[1:]:
        if a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a,b])
    return [tuple(x) for x in out]


def export_unknown_chunks(data: bytes, known_ranges, root: Path):
    root.mkdir(parents=True, exist_ok=True)
    merged = merge_ranges(known_ranges)
    chunks = []
    cursor = 0

    for a,b in merged + [(len(data), len(data))]:
        if cursor < a:
            raw = data[cursor:a]
            name = f"{len(chunks):03d}_0x{cursor:X}-0x{a:X}_{len(raw)}bytes.bin"
            (root / name).write_bytes(raw)
            chunks.append({
                "offset": cursor,
                "end_offset": a,
                "size": len(raw),
                "filename": name,
                "sha256": sha256(raw),
            })
        cursor = max(cursor, b)

    return chunks


def parse_code_guided_top_level(data: bytes):
    """
    Parse the verified top-level prefix exactly in loader order.
    """
    bank = try_texture_bank(data, 0)
    if not bank:
        raise ValueError("Plik nie zaczyna się rozpoznanym bankiem tekstur R3D.")

    offset = bank["end_offset"]

    materials, offset = parse_material_records(data, offset)
    animations, offset = parse_animation_records(data, offset, materials)

    main_mesh = try_mesh(data, offset, full_validate=True)
    if not main_mesh:
        raise ValueError(
            f"Po tabeli animacji pod 0x{offset:X} nie znaleziono mesha "
            "zgodnego z loaderem 0x460880."
        )

    return {
        "primary_texture_bank": bank,
        "materials": materials,
        "animations": animations,
        "main_mesh": main_mesh,
        "confirmed_prefix_end": main_mesh["end_offset"],
    }



def detect_skybox_bank(texture_banks_manifest):
    """
    Detect ToonCar's classic six-face cubemap bank by the filename suffixes
    UP, DN, FR, BK, LF and RT.
    """
    required = ("UP", "DN", "FR", "BK", "LF", "RT")

    for bank in texture_banks_manifest:
        faces = {}

        for tex in bank.get("entries", []):
            stem = Path(tex["name"]).stem.upper()

            for face in required:
                if stem.endswith("_" + face) or stem.endswith(face):
                    faces[face] = tex
                    break

        if all(face in faces for face in required):
            return {
                "bank_offset": bank["offset"],
                "bank_directory": bank["directory"],
                "faces": {
                    face: {
                        "source_name": faces[face]["name"],
                        "png": faces[face]["png"],
                    }
                    for face in required
                },
            }

    return None



def find_sibling_r3d_case_insensitive(source_path: Path, target_name: str):
    target = target_name.lower()
    try:
        for candidate in source_path.parent.iterdir():
            if candidate.is_file() and candidate.name.lower() == target:
                return candidate.resolve()
    except Exception:
        pass
    return None


def export_simple_objectmesh_asset(
    asset_path: Path,
    output_dir: Path,
    scale: float,
    asset_name=None,
    relative_prefix=None,
    export_raw_data=False,
):
    """
    Decode the compact standalone R3D layout used by Sorpresa.r3d:

        TextureBank @ 0
        uint32 serialized_mesh_size
        ObjectMesh (0x46FAC0 layout)

    This deliberately validates the whole file so an unrelated R3D cannot be
    silently misinterpreted as the pickup model.
    """
    asset_path = Path(asset_path).resolve()
    data = asset_path.read_bytes()

    asset_name = (
        str(asset_name).strip()
        if asset_name
        else asset_path.stem
    )
    safe_asset_name = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        asset_name,
    ) or "asset"

    prefix_path = (
        Path(relative_prefix)
        if relative_prefix is not None
        else Path(".")
    )

    bank = try_texture_bank(data, 0)
    if not bank:
        return None

    size_offset = bank["end_offset"]
    if size_offset + 4 > len(data):
        return None

    serialized_mesh_size = read_u32(data, size_offset)
    mesh_offset = size_offset + 4

    mesh = try_object_mesh(data, mesh_offset)
    if not mesh:
        return None

    if mesh["size"] != serialized_mesh_size:
        return None

    if mesh["end_offset"] != len(data):
        return None

    output_dir.mkdir(parents=True, exist_ok=True)

    texture_root = output_dir / "textures"
    texture_manifest = export_texture_banks(
        data,
        [bank],
        texture_root,
        export_raw_data=export_raw_data,
    )[0]

    obj_path = output_dir / f"{safe_asset_name}.obj"
    mtl_path = output_dir / f"{safe_asset_name}.mtl"

    # Standalone pickup files do not have the track's 0x60 material table.
    # For Sorpresa.r3d the ObjectMesh material id directly selects the sole
    # embedded texture. Keep this generic for multi-texture compact assets too
    # whenever the group material id is in the texture-bank range.
    with mtl_path.open("w", encoding="utf-8", newline="\n") as mtl:
        for group in mesh["groups"]:
            material_id = int(group["material_id"])
            mtl.write(f"newmtl mat_{material_id:03d}\n")
            mtl.write("Ka 0 0 0\n")
            mtl.write("Kd 1 1 1\n")
            mtl.write("Ks 0 0 0\n")
            mtl.write("illum 1\n")

            if 0 <= material_id < len(texture_manifest["entries"]):
                tex = texture_manifest["entries"][material_id]
                mtl.write(
                    f"map_Kd textures/{texture_manifest['directory']}/"
                    f"{tex['png']}\n"
                )

            mtl.write("\n")

    with obj_path.open("w", encoding="utf-8", newline="\n") as obj:
        obj.write("# ToonCar standalone ObjectMesh asset v102\n")
        obj.write(f"mtllib {mtl_path.name}\n\n")
        obj.write(f"o {safe_asset_name}\n")

        for vi in range(mesh["vertex_count"]):
            voff = mesh["vertex_start"] + vi * OBJECT_VERTEX_STRIDE

            x, y, z = struct.unpack_from("<3f", data, voff)
            nx, ny, nz = struct.unpack_from("<3f", data, voff + 0x0C)
            u, v = struct.unpack_from("<2f", data, voff + 0x28)

            # Same verified source -> OBJ conversion used by all ToonCar meshes.
            obj.write(
                f"v {x*scale:.9g} {y*scale:.9g} {-z*scale:.9g}\n"
            )
            obj.write(f"vt {u:.9g} {1.0-v:.9g}\n")
            obj.write(f"vn {nx:.9g} {ny:.9g} {-nz:.9g}\n")

        for group in mesh["groups"]:
            obj.write(f"usemtl mat_{group['material_id']:03d}\n")

            for fi in range(
                group["face_start"],
                group["face_start"] + group["face_count"],
            ):
                a, b, c = struct.unpack_from(
                    "<3H",
                    data,
                    mesh["face_start"] + fi * OBJECT_FACE_STRIDE,
                )

                a = group["vertex_start"] + a + 1
                b = group["vertex_start"] + b + 1
                c = group["vertex_start"] + c + 1

                # Mirror-Z export requires reversed winding.
                obj.write(
                    f"f {a}/{a}/{a} {c}/{c}/{c} {b}/{b}/{b}\n"
                )

    details = {
        "source": str(asset_path),
        "layout": "TextureBank + uint32 mesh_size + ObjectMesh",
        "texture_count": bank["count"],
        "textures": [
            {
                "index": int(entry["index"]),
                "source_name": entry["name"],
                "png": entry["png"],
                "has_alpha": bool(entry.get("has_alpha")),
                "alpha": entry.get("alpha") or {},
            }
            for entry in texture_manifest["entries"]
        ],
        "mesh": {
            "offset": mesh["offset"],
            "size": mesh["size"],
            "group_count": mesh["group_count"],
            "vertex_count": mesh["vertex_count"],
            "face_count": mesh["face_count"],
        },
        "asset_name": asset_name,
        "obj": str(prefix_path / obj_path.name),
        "mtl": str(prefix_path / mtl_path.name),
        "texture_directory": str(
            prefix_path
            / "textures"
            / texture_manifest["directory"]
        ),
        "status": "decoded",
        "gameplay_render": {
            "source_loader": "0x451640 (%s%s.r3d)",
            "source_runtime_init": "0x4143A0",
            "source_runtime_update": "0x415080",
            "idle_instance_scale": 0.75,
            "idle_rotation_degrees_per_tick": 2.0,
            "idle_bob_phase_degrees_per_tick": 5.0,
            "idle_bob_delta_source_units": 0.05,
            "game_tick_hz": 55,
            "rotation_axis_source": "Y",
            "rotation_axis_blender": "Z",
            "bob_axis_source": "Y",
            "bob_axis_blender": "Z",
            "idle_cycle_ticks": 360
        },
    }

    (output_dir / "asset.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return details







def parse_simple_metadata_object_r3d_layout(data: bytes):
    """
    Simple object layout verified on Mina.r3d, Napal.r3d and Cono.r3d:

        TextureBank @ 0
        ObjectMesh
        fixed 0x64-byte Object metadata tail

    The 0x64-byte tail is preserved as metadata. Its exact runtime semantics
    are not needed to reconstruct the visible mesh and texture.
    """
    bank = try_texture_bank(data, 0)
    if not bank:
        return None

    mesh = try_object_mesh(
        data,
        bank["end_offset"],
    )
    if not mesh:
        return None

    tail_size = len(data) - mesh["end_offset"]

    # Mina/Napal use a fixed 0x64-byte descriptor after the visible mesh.
    # Requiring this exact size avoids misclassifying arbitrary R3Ds.
    if tail_size != 0x64:
        return None

    tail_offset = mesh["end_offset"]
    tail = data[tail_offset:]

    # Known object descriptor starts with enabled/count = 1.
    if read_u32(tail, 0) != 1:
        return None

    return {
        "texture_bank": bank,
        "mesh": mesh,
        "tail_offset": tail_offset,
        "tail_size": tail_size,
        "tail_hex": tail.hex(),
        "end_offset": len(data),
    }


def export_simple_metadata_object_r3d(
    src_path,
    out_path=None,
    scale=0.1,
    export_raw_data=False,
    log=print,
):
    src = Path(src_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(src)
    if src.suffix.lower() != ".r3d":
        raise ValueError("Wybierz plik .r3d")

    data = src.read_bytes()
    layout = parse_simple_metadata_object_r3d_layout(data)

    if not layout:
        raise ValueError(
            "Plik nie pasuje do obsługiwanego układu Static Object."
        )

    out = (
        Path(out_path).resolve()
        if out_path
        else src.parent / f"{src.stem}_unpacked_v102"
    )
    out.mkdir(parents=True, exist_ok=True)

    asset_dir = out / "asset"
    asset_dir.mkdir(exist_ok=True)

    bank = layout["texture_bank"]
    mesh = layout["mesh"]

    texture_manifest = export_texture_banks(
        data,
        [bank],
        asset_dir / "textures",
        export_raw_data=export_raw_data,
    )[0]

    safe_name = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        src.stem,
    ) or "asset"

    obj_path = asset_dir / f"{safe_name}.obj"
    mtl_path = asset_dir / f"{safe_name}.mtl"

    with mtl_path.open("w", encoding="utf-8", newline="\n") as mtl:
        material_ids = sorted({
            int(group["material_id"])
            for group in mesh["groups"]
        })

        for material_id in material_ids:
            mtl.write(f"newmtl mat_{material_id:03d}\n")
            mtl.write("Ka 0 0 0\n")
            mtl.write("Kd 1 1 1\n")
            mtl.write("Ks 0 0 0\n")
            mtl.write("illum 1\n")

            if texture_manifest["entries"]:
                tex_index = (
                    material_id
                    if 0 <= material_id < len(texture_manifest["entries"])
                    else 0
                )
                tex = texture_manifest["entries"][tex_index]
                mtl.write(
                    f"map_Kd textures/{texture_manifest['directory']}/"
                    f"{tex['png']}\n"
                )

            mtl.write("\n")

    with obj_path.open("w", encoding="utf-8", newline="\n") as obj:
        obj.write("# ToonCar Static ObjectMesh asset v102\n")
        obj.write(f"mtllib {mtl_path.name}\n\n")
        obj.write(f"o {safe_name}\n")

        for vi in range(mesh["vertex_count"]):
            voff = (
                mesh["vertex_start"]
                + vi * OBJECT_VERTEX_STRIDE
            )

            x, y, z = struct.unpack_from("<3f", data, voff)
            nx, ny, nz = struct.unpack_from(
                "<3f",
                data,
                voff + 0x0C,
            )
            u, v = struct.unpack_from(
                "<2f",
                data,
                voff + 0x28,
            )

            obj.write(
                f"v {x*scale:.9g} "
                f"{y*scale:.9g} "
                f"{-z*scale:.9g}\n"
            )
            obj.write(
                f"vt {u:.9g} {1.0-v:.9g}\n"
            )
            obj.write(
                f"vn {nx:.9g} {ny:.9g} {-nz:.9g}\n"
            )

        for group in mesh["groups"]:
            obj.write(
                f"usemtl mat_{int(group['material_id']):03d}\n"
            )

            for fi in range(
                group["face_start"],
                group["face_start"] + group["face_count"],
            ):
                a, b, c = struct.unpack_from(
                    "<3H",
                    data,
                    mesh["face_start"]
                    + fi * OBJECT_FACE_STRIDE,
                )

                a = group["vertex_start"] + a + 1
                b = group["vertex_start"] + b + 1
                c = group["vertex_start"] + c + 1

                obj.write(
                    f"f {a}/{a}/{a} "
                    f"{c}/{c}/{c} "
                    f"{b}/{b}/{b}\n"
                )

    textures = [
        {
            "index": int(entry["index"]),
            "source_name": entry["name"],
            "png": entry["png"],
            "has_alpha": bool(entry.get("has_alpha")),
            "alpha": entry.get("alpha") or {},
        }
        for entry in texture_manifest["entries"]
    ]

    manifest = {
        "version": 102,
        "asset_type": "simple_metadata_object",
        "source": {
            "filename": src.name,
            "path": str(src),
            "size_bytes": len(data),
        },
        "export_scale": scale,
        "asset": {
            "asset_name": src.stem,
            "layout": "TextureBank + ObjectMesh + 0x64 Object metadata",
            "obj": str(Path("asset") / obj_path.name),
            "mtl": str(Path("asset") / mtl_path.name),
            "texture_directory": str(
                Path("asset")
                / "textures"
                / texture_manifest["directory"]
            ),
            "textures": textures,
            "selected_texture_index": (
                0 if textures else None
            ),
            "mesh": {
                "offset": int(mesh["offset"]),
                "size": int(mesh["size"]),
                "group_count": int(mesh["group_count"]),
                "vertex_count": int(mesh["vertex_count"]),
                "face_count": int(mesh["face_count"]),
            },
            "object_metadata": {
                "offset": int(layout["tail_offset"]),
                "size": int(layout["tail_size"]),
                "raw_hex": layout["tail_hex"],
            },
        },
    }

    (out / "manifest.json").write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log(f"Plik: {src.name}")
    log("Typ: Static Object")
    log(
        f"Geometria: {mesh['vertex_count']} vertexów, "
        f"{mesh['face_count']} trójkątów"
    )
    log(
        "Tekstury: "
        + ", ".join(
            entry["name"]
            for entry in bank["entries"]
        )
    )
    log("Metadata tail: 0x64 bajty")
    log(f"Rozpakowano prosty asset: {out}")

    return out




def parse_animation_set_relaxed(data: bytes, offset: int):
    """
    Parse one serialized ToonCar AnimationSet without requiring it to end at EOF.
    This is needed for assets that serialize multiple AnimationSets sequentially.
    """
    if offset + ANIM_SET_HEADER_SIZE + 4 > len(data):
        return None

    header = list(
        struct.unpack_from("<9I", data, offset)
    )
    track_count = int(header[0])

    if not (1 <= track_count <= 256):
        return None

    cursor = offset + ANIM_SET_HEADER_SIZE
    mapping_count = read_u32(data, cursor)
    cursor += 4

    if (
        mapping_count > 4096
        or cursor + mapping_count * 4 > len(data)
    ):
        return None

    mapping_raw = list(
        struct.unpack_from(
            f"<{mapping_count}I",
            data,
            cursor,
        )
    ) if mapping_count else []
    cursor += mapping_count * 4

    mapping = [
        -1 if value == 0xFFFFFFFF else int(value)
        for value in mapping_raw
    ]

    for value in mapping:
        if value >= track_count:
            return None

    tracks = []

    for track_index in range(track_count):
        if cursor + 4 > len(data):
            return None

        key_count = read_u32(data, cursor)
        track_offset = cursor
        cursor += 4

        if not (1 <= key_count <= 100000):
            return None

        keys_end = cursor + key_count * ANIM_KEY_SIZE
        if keys_end > len(data):
            return None

        keys = []

        for key_index in range(key_count):
            key_offset = cursor + key_index * ANIM_KEY_SIZE
            values = struct.unpack_from(
                "<10f",
                data,
                key_offset,
            )

            if not all(
                math.isfinite(v) and abs(v) < 1e12
                for v in values
            ):
                return None

            quaternion = list(values[0:4])
            q_len = math.sqrt(
                sum(v * v for v in quaternion)
            )
            if q_len < 1e-6 or q_len > 10.0:
                return None

            keys.append({
                "index": key_index,
                "quaternion_xyzw": quaternion,
                "translation": list(values[4:7]),
                "scale": list(values[7:10]),
            })

        cursor = keys_end

        tracks.append({
            "index": track_index,
            "key_count": int(key_count),
            "keys": keys,
            "offset": track_offset,
        })

    return {
        "offset": offset,
        "end_offset": cursor,
        "header_dwords": header,
        "track_count": track_count,
        "mapping_count": int(mapping_count),
        "mapping": mapping,
        "tracks": tracks,
    }


def parse_rigged_object_r3d_layout(data: bytes):
    """
    Rigged/skinned object layout verified on ConoPatas.r3d.

    Layout:
      TextureBank @ 0
      material table
      node maps / attachment lists
      0x5C skinned-mesh container
      one or more ObjectMesh + skin payload LODs
      recursive 0x110 node skeleton
      zero or more sequential AnimationSets
    """
    bank = try_texture_bank(data, 0)
    if not bank:
        return None

    try:
        materials, cursor = parse_material_records(
            data,
            bank["end_offset"],
        )
    except Exception:
        return None

    # Same model reference package used by Character assets.
    if cursor + 4 > len(data):
        return None

    direct_mesh_count = read_u32(data, cursor)
    cursor += 4

    direct_meshes = []
    for _ in range(direct_mesh_count):
        mesh = try_object_mesh(data, cursor)
        if not mesh:
            return None
        direct_meshes.append(mesh)
        cursor = mesh["end_offset"]

    if cursor + 4 > len(data):
        return None

    direct_map_count = read_u32(data, cursor)
    cursor += 4

    if (
        direct_map_count > 4096
        or cursor + direct_map_count * 4 > len(data)
    ):
        return None

    direct_map = list(
        struct.unpack_from(
            f"<{direct_map_count}i",
            data,
            cursor,
        )
    ) if direct_map_count else []
    cursor += direct_map_count * 4

    if cursor + 4 > len(data):
        return None

    vec_list_count = read_u32(data, cursor)
    cursor += 4

    for _ in range(vec_list_count):
        if cursor + 4 > len(data):
            return None

        count = read_u32(data, cursor)
        cursor += 4 + count * 0x0C

        if cursor > len(data):
            return None

    if cursor + 4 > len(data):
        return None

    vec_map_count = read_u32(data, cursor)
    cursor += 4

    if (
        vec_map_count > 4096
        or cursor + vec_map_count * 4 > len(data)
    ):
        return None

    vec_map = list(
        struct.unpack_from(
            f"<{vec_map_count}i",
            data,
            cursor,
        )
    ) if vec_map_count else []
    cursor += vec_map_count * 4

    if cursor + 4 > len(data):
        return None

    skin_container_marker = read_u32(data, cursor)
    cursor += 4

    if not skin_container_marker:
        return None

    if cursor + 0x5C > len(data):
        return None

    container_offset = cursor
    slot_markers = [
        read_u32(data, cursor + 0x40 + i * 4)
        for i in range(3)
    ]
    cursor += 0x5C

    lods = []

    for slot_index, marker in enumerate(slot_markers):
        if not marker:
            continue

        mesh = try_object_mesh(data, cursor)
        if not mesh:
            return None

        cursor = mesh["end_offset"]

        skin = parse_character_skin_payload(
            data,
            cursor,
        )
        if not skin:
            return None

        # Strong integrity check: the skin partition should cover the mesh.
        skinned_vertex_count = sum(
            record["vertex_count"]
            for record in skin["records"]
        )
        if skinned_vertex_count != mesh["vertex_count"]:
            return None

        cursor = skin["end_offset"]

        lods.append({
            "slot_index": int(slot_index),
            "mesh": mesh,
            "skin": skin,
        })

    if not lods:
        return None

    try:
        skeleton = parse_character_node_tree(
            data,
            cursor,
        )
    except Exception:
        return None

    cursor = skeleton["end_offset"]

    animations = []

    while cursor < len(data):
        animation = parse_animation_set_relaxed(
            data,
            cursor,
        )
        if not animation:
            return None

        animations.append(animation)
        cursor = animation["end_offset"]

    if cursor != len(data):
        return None

    lods = sorted(
        lods,
        key=lambda lod: lod["mesh"]["face_count"],
        reverse=True,
    )

    return {
        "texture_bank": bank,
        "materials": materials,
        "direct_meshes": direct_meshes,
        "direct_map": direct_map,
        "vec_map": vec_map,
        "skin_container_offset": container_offset,
        "slot_markers": slot_markers,
        "lods": lods,
        "skeleton": skeleton,
        "animations": animations,
        "end_offset": cursor,
    }


def export_rigged_object_r3d(
    src_path,
    out_path=None,
    scale=0.1,
    export_raw_data=False,
    log=print,
):
    src = Path(src_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(src)

    data = src.read_bytes()
    layout = parse_rigged_object_r3d_layout(
        data
    )

    if not layout:
        raise ValueError(
            "Plik nie pasuje do obsługiwanego układu Rigged Object R3D."
        )

    out = (
        Path(out_path).resolve()
        if out_path
        else src.parent / f"{src.stem}_unpacked_v102"
    )
    out.mkdir(parents=True, exist_ok=True)

    asset_dir = out / "asset"
    asset_dir.mkdir(exist_ok=True)

    texture_manifest = export_texture_banks(
        data,
        [layout["texture_bank"]],
        asset_dir / "textures",
        export_raw_data=export_raw_data,
    )[0]

    textures = [
        {
            "index": int(entry["index"]),
            "source_name": entry["name"],
            "png": entry["png"],
            "has_alpha": bool(entry.get("has_alpha")),
            "alpha": entry.get("alpha") or {},
        }
        for entry in texture_manifest["entries"]
    ]

    safe_name = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        src.stem,
    ) or "rigged_asset"

    obj_path = asset_dir / f"{safe_name}.obj"
    mtl_path = asset_dir / f"{safe_name}.mtl"

    used_material_ids = sorted({
        int(group["material_id"])
        for lod in layout["lods"]
        for group in lod["mesh"]["groups"]
    })

    with mtl_path.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as mtl:
        for material_id in used_material_ids:
            mtl.write(
                f"newmtl mat_{material_id:03d}\n"
            )
            mtl.write("Ka 0 0 0\n")
            mtl.write("Kd 1 1 1\n")
            mtl.write("Ks 0 0 0\n")
            mtl.write("illum 1\n")

            if texture_manifest["entries"]:
                tex = texture_manifest["entries"][0]
                mtl.write(
                    f"map_Kd textures/{texture_manifest['directory']}/"
                    f"{tex['png']}\n"
                )
            mtl.write("\n")

    with obj_path.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as obj:
        obj.write(
            "# ToonCar Rigged Object LOD export v102\n"
        )
        obj.write(
            f"mtllib {mtl_path.name}\n\n"
        )

        vertex_base = 0

        for lod_index, lod in enumerate(
            layout["lods"]
        ):
            mesh = lod["mesh"]
            obj.write(
                f"o CharacterLOD_{lod_index:02d}\n"
            )

            for vi in range(mesh["vertex_count"]):
                voff = (
                    mesh["vertex_start"]
                    + vi * OBJECT_VERTEX_STRIDE
                )

                x, y, z = struct.unpack_from(
                    "<3f",
                    data,
                    voff,
                )
                nx, ny, nz = struct.unpack_from(
                    "<3f",
                    data,
                    voff + 0x0C,
                )
                u, v = struct.unpack_from(
                    "<2f",
                    data,
                    voff + 0x28,
                )

                obj.write(
                    f"v {x*scale:.9g} "
                    f"{y*scale:.9g} "
                    f"{-z*scale:.9g}\n"
                )
                obj.write(
                    f"vt {u:.9g} {1.0-v:.9g}\n"
                )
                obj.write(
                    f"vn {nx:.9g} {ny:.9g} {-nz:.9g}\n"
                )

            for group in mesh["groups"]:
                obj.write(
                    f"usemtl mat_{int(group['material_id']):03d}\n"
                )

                for fi in range(
                    group["face_start"],
                    group["face_start"]
                    + group["face_count"],
                ):
                    a, b, c = struct.unpack_from(
                        "<3H",
                        data,
                        mesh["face_start"]
                        + fi * OBJECT_FACE_STRIDE,
                    )

                    a = (
                        vertex_base
                        + group["vertex_start"]
                        + a + 1
                    )
                    b = (
                        vertex_base
                        + group["vertex_start"]
                        + b + 1
                    )
                    c = (
                        vertex_base
                        + group["vertex_start"]
                        + c + 1
                    )

                    obj.write(
                        f"f {a}/{a}/{a} "
                        f"{c}/{c}/{c} "
                        f"{b}/{b}/{b}\n"
                    )

            vertex_base += mesh["vertex_count"]
            obj.write("\n")

    skeleton_nodes = []

    for node in layout["skeleton"]["nodes"]:
        skeleton_nodes.append({
            "index": int(node["index"]),
            "parent_index": (
                int(node["parent_index"])
                if node["parent_index"] is not None
                else None
            ),
            "child_indices": [
                int(x)
                for x in node["child_indices"]
            ],
            "local_matrix": [
                float(x)
                for x in node["local_matrix"]
            ],
            "global_matrix": [
                float(x)
                for x in node["global_matrix"]
            ],
        })

    lod_manifests = []

    for lod_index, lod in enumerate(
        layout["lods"]
    ):
        skin = lod["skin"]
        groups = {}

        for record_index, record in enumerate(
            skin["records"]
        ):
            bone_index = (
                skin["record_to_bone"].get(
                    record_index
                )
            )
            if bone_index is None:
                continue

            groups[str(bone_index)] = [
                int(v)
                for v in record["vertex_indices"]
            ]

        lod_manifests.append({
            "lod_index": lod_index,
            "source_slot": int(
                lod["slot_index"]
            ),
            "vertex_count": int(
                lod["mesh"]["vertex_count"]
            ),
            "face_count": int(
                lod["mesh"]["face_count"]
            ),
            "bone_vertex_groups": groups,
            "skin_bone_map_count": int(
                skin["bone_map_count"]
            ),
        })

    animation_manifests = []

    for anim_index, animation in enumerate(
        layout["animations"]
    ):
        animation_manifests.append({
            "index": anim_index,
            "track_count": int(
                animation["track_count"]
            ),
            "mapping_count": int(
                animation["mapping_count"]
            ),
            "mapping": [
                int(x)
                for x in animation["mapping"]
            ],
            "track_key_counts": [
                int(track["key_count"])
                for track in animation["tracks"]
            ],
            "tracks": [
                {
                    "index": int(track["index"]),
                    "keys": [
                        {
                            "quaternion_xyzw": [
                                float(x)
                                for x in key["quaternion_xyzw"]
                            ],
                            "translation": [
                                float(x)
                                for x in key["translation"]
                            ],
                            "scale": [
                                float(x)
                                for x in key["scale"]
                            ],
                        }
                        for key in track["keys"]
                    ],
                }
                for track in animation["tracks"]
            ],
        })

    manifest = {
        "version": 102,
        "asset_type": "rigged_object",
        "source": {
            "filename": src.name,
            "path": str(src),
            "size_bytes": len(data),
        },
        "export_scale": scale,
        "asset": {
            "asset_name": src.stem,
            "obj": str(
                Path("asset") / obj_path.name
            ),
            "mtl": str(
                Path("asset") / mtl_path.name
            ),
            "texture_directory": str(
                Path("asset")
                / "textures"
                / texture_manifest["directory"]
            ),
            "textures": textures,
            "selected_texture_index": (
                0 if textures else None
            ),
            "lods": lod_manifests,
            "skeleton": {
                "node_count": len(
                    skeleton_nodes
                ),
                "nodes": skeleton_nodes,
                "source_node_size": 0x110,
                "global_matrix_offset": 0x70,
                "local_matrix_offset": 0x30,
            },
            "animations": animation_manifests,
        },
    }

    (out / "manifest.json").write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log(f"Plik: {src.name}")
    log("Typ: Rigged Object")
    log(
        f"LOD-y: {len(layout['lods'])}; "
        f"rig: {len(skeleton_nodes)} node'ów; "
        f"animacje: {len(layout['animations'])}"
    )

    for index, animation in enumerate(
        layout["animations"]
    ):
        counts = [
            track["key_count"]
            for track in animation["tracks"]
        ]
        summary = (
            str(counts[0])
            if counts
            and len(set(counts)) == 1
            else str(counts)
        )
        log(
            f"Animacja {index}: "
            f"{animation['track_count']} tracków, "
            f"klatki/track: {summary}"
        )

    log(f"Rozpakowano rigowany asset: {out}")
    return out



def parse_character_skin_payload(data: bytes, offset: int):
    """
    Parse the skin block read by ToonCar.exe 0x47B610.

    Serialized form:
      0x24-byte header
      header[2] * int32 bone->skin-record map
      header[4] * Vec3 auxiliary entries
      header[0] skin records:
        uint32 vertex_count
        vertex_count * Vec3 bind positions
        vertex_count * Vec3 bind normals
        vertex_count * uint32 vertex indices
    """
    start = offset

    if offset + 0x24 > len(data):
        return None

    header = list(struct.unpack_from("<9I", data, offset))
    record_count = int(header[0])
    bone_map_count = int(header[2])
    aux_vec_count = int(header[4])

    if not (0 <= record_count <= 4096):
        return None
    if not (0 <= bone_map_count <= 4096):
        return None
    if not (0 <= aux_vec_count <= 100000):
        return None

    cursor = offset + 0x24

    map_bytes = bone_map_count * 4
    if cursor + map_bytes > len(data):
        return None

    bone_to_record = list(
        struct.unpack_from(
            f"<{bone_map_count}i",
            data,
            cursor,
        )
    )
    cursor += map_bytes

    aux_bytes = aux_vec_count * 0x0C
    if cursor + aux_bytes > len(data):
        return None
    cursor += aux_bytes

    records = []

    for record_index in range(record_count):
        if cursor + 4 > len(data):
            return None

        vertex_count = read_u32(data, cursor)
        cursor += 4

        if vertex_count > 1000000:
            return None

        positions_offset = cursor
        cursor += vertex_count * 0x0C

        normals_offset = cursor
        cursor += vertex_count * 0x0C

        indices_offset = cursor
        cursor += vertex_count * 4

        if cursor > len(data):
            return None

        indices = list(
            struct.unpack_from(
                f"<{vertex_count}I",
                data,
                indices_offset,
            )
        ) if vertex_count else []

        records.append({
            "index": record_index,
            "vertex_count": int(vertex_count),
            "positions_offset": positions_offset,
            "normals_offset": normals_offset,
            "indices_offset": indices_offset,
            "vertex_indices": indices,
        })

    record_to_bone = {}
    for bone_index, record_index in enumerate(bone_to_record):
        if record_index >= 0:
            record_to_bone[int(record_index)] = int(bone_index)

    return {
        "offset": start,
        "end_offset": cursor,
        "size": cursor - start,
        "header": header,
        "record_count": record_count,
        "bone_map_count": bone_map_count,
        "bone_to_record": bone_to_record,
        "record_to_bone": record_to_bone,
        "records": records,
    }


def parse_character_node_tree(data: bytes, offset: int):
    """
    Parse the recursive 0x110-byte node hierarchy loaded by 0x4751A0.

    Chatty's skeleton nodes use the plain node form (no optional flag payloads).
    The two 4x4 matrices at +0x30 and +0x70 behave as local and global bind
    transforms respectively.
    """
    nodes = []

    def parse_node(cursor, parent_index):
        if cursor + 0x110 > len(data):
            raise ValueError("Niepełny node szkieletu postaci.")

        raw_offset = cursor
        child_count = read_u32(data, cursor + 0xF0)
        flags = read_u32(data, cursor + 0xF8)
        optional_payload = read_u32(data, cursor + 0x2C)

        if child_count > 256:
            raise ValueError("Nieprawidłowa liczba dzieci node.")
        if flags != 0 or optional_payload != 0:
            raise ValueError(
                "Node postaci używa jeszcze nieobsługiwanego "
                "opcjonalnego payloadu."
            )

        local_matrix = list(
            struct.unpack_from("<16f", data, cursor + 0x30)
        )
        global_matrix = list(
            struct.unpack_from("<16f", data, cursor + 0x70)
        )

        index = len(nodes)
        nodes.append({
            "index": index,
            "offset": raw_offset,
            "parent_index": parent_index,
            "child_count": int(child_count),
            "flags": int(flags),
            "local_matrix": local_matrix,
            "global_matrix": global_matrix,
        })

        cursor += 0x110

        child_indices = []
        for _ in range(child_count):
            child_index = len(nodes)
            cursor = parse_node(cursor, index)
            child_indices.append(child_index)

        nodes[index]["child_indices"] = child_indices
        return cursor

    end_offset = parse_node(offset, None)

    return {
        "offset": offset,
        "end_offset": end_offset,
        "size": end_offset - offset,
        "nodes": nodes,
    }


def parse_character_r3d_layout(data: bytes):
    # Character loader reads a fixed 0x1C8 header, then the embedded texture
    # bank and a model/skin package.
    bank = try_texture_bank(data, 0x1C8)
    if not bank:
        return None

    try:
        materials, cursor = parse_material_records(
            data,
            bank["end_offset"],
        )
    except Exception:
        return None

    # ObjectMesh reference list (0x471870). Chatty stores no meshes directly
    # in this list, but retains a 26-entry node mapping.
    if cursor + 4 > len(data):
        return None

    direct_mesh_count = read_u32(data, cursor)
    cursor += 4
    direct_meshes = []

    for _ in range(direct_mesh_count):
        mesh = try_object_mesh(data, cursor)
        if not mesh:
            return None
        direct_meshes.append(mesh)
        cursor = mesh["end_offset"]

    if cursor + 4 > len(data):
        return None

    direct_map_count = read_u32(data, cursor)
    cursor += 4
    if direct_map_count > 4096 or cursor + direct_map_count * 4 > len(data):
        return None
    direct_map = list(
        struct.unpack_from(
            f"<{direct_map_count}i",
            data,
            cursor,
        )
    ) if direct_map_count else []
    cursor += direct_map_count * 4

    # Vec3-list reference package (0x475DE0). Same pattern: a list followed
    # by a node-index mapping.
    if cursor + 4 > len(data):
        return None

    vec_list_count = read_u32(data, cursor)
    cursor += 4

    for _ in range(vec_list_count):
        if cursor + 4 > len(data):
            return None
        count = read_u32(data, cursor)
        cursor += 4 + count * 0x0C
        if cursor > len(data):
            return None

    if cursor + 4 > len(data):
        return None

    vec_map_count = read_u32(data, cursor)
    cursor += 4

    if vec_map_count > 4096 or cursor + vec_map_count * 4 > len(data):
        return None

    vec_map = list(
        struct.unpack_from(
            f"<{vec_map_count}i",
            data,
            cursor,
        )
    ) if vec_map_count else []
    cursor += vec_map_count * 4

    # Optional 0x5C skinned-mesh container.
    if cursor + 4 > len(data):
        return None

    skin_container_marker = read_u32(data, cursor)
    cursor += 4

    lods = []

    if skin_container_marker:
        if cursor + 0x5C > len(data):
            return None

        container_offset = cursor
        container_raw = data[cursor:cursor + 0x5C]
        slot_markers = [
            struct.unpack_from("<I", container_raw, 0x40 + i * 4)[0]
            for i in range(3)
        ]
        cursor += 0x5C

        for slot_index, marker in enumerate(slot_markers):
            if not marker:
                continue

            mesh = try_object_mesh(data, cursor)
            if not mesh:
                return None

            cursor = mesh["end_offset"]

            skin = parse_character_skin_payload(
                data,
                cursor,
            )
            if not skin:
                return None

            cursor = skin["end_offset"]

            lods.append({
                "slot_index": slot_index,
                "mesh": mesh,
                "skin": skin,
            })
    else:
        container_offset = None
        slot_markers = []

    # Recursive skeleton/root node follows the skinned LOD packages.
    try:
        skeleton = parse_character_node_tree(
            data,
            cursor,
        )
    except Exception:
        return None

    cursor = skeleton["end_offset"]

    if cursor != len(data):
        return None

    # Highest-detail LOD first. Slot order already appears high->low for
    # Chatty, but sort explicitly by triangle count as a validation-safe rule.
    lods = sorted(
        lods,
        key=lambda lod: lod["mesh"]["face_count"],
        reverse=True,
    )

    return {
        "header_size": 0x1C8,
        "texture_bank": bank,
        "materials": materials,
        "direct_meshes": direct_meshes,
        "direct_map": direct_map,
        "vec_map": vec_map,
        "skin_container_offset": container_offset,
        "slot_markers": slot_markers,
        "lods": lods,
        "skeleton": skeleton,
        "end_offset": cursor,
    }


def export_character_r3d(
    src_path,
    out_path=None,
    scale=0.1,
    export_raw_data=False,
    log=print,
):
    src = Path(src_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(src)
    if src.suffix.lower() != ".r3d":
        raise ValueError("Wybierz plik .r3d")

    data = src.read_bytes()
    layout = parse_character_r3d_layout(data)

    if not layout:
        raise ValueError(
            "Plik nie pasuje do obsługiwanego układu ToonCar Character R3D."
        )

    out = (
        Path(out_path).resolve()
        if out_path
        else src.parent / f"{src.stem}_unpacked_v102"
    )
    out.mkdir(parents=True, exist_ok=True)

    asset_dir = out / "asset"
    asset_dir.mkdir(exist_ok=True)

    texture_manifest = export_texture_banks(
        data,
        [layout["texture_bank"]],
        asset_dir / "textures",
        export_raw_data=export_raw_data,
    )[0]

    textures = [
        {
            "index": int(entry["index"]),
            "source_name": entry["name"],
            "png": entry["png"],
            "has_alpha": bool(entry.get("has_alpha")),
            "alpha": entry.get("alpha") or {},
        }
        for entry in texture_manifest["entries"]
    ]

    obj_path = asset_dir / f"{src.stem}.obj"
    mtl_path = asset_dir / f"{src.stem}.mtl"

    material_ids = sorted({
        int(group["material_id"])
        for lod in layout["lods"]
        for group in lod["mesh"]["groups"]
    })

    selected_texture = (
        texture_manifest["entries"][0]
        if texture_manifest["entries"]
        else None
    )

    with mtl_path.open("w", encoding="utf-8", newline="\n") as mtl:
        for material_id in material_ids:
            mtl.write(f"newmtl mat_{material_id:03d}\n")
            mtl.write("Ka 0 0 0\n")
            mtl.write("Kd 1 1 1\n")
            mtl.write("Ks 0 0 0\n")
            mtl.write("illum 1\n")

            if selected_texture:
                mtl.write(
                    f"map_Kd textures/{texture_manifest['directory']}/"
                    f"{selected_texture['png']}\n"
                )

            mtl.write("\n")

    with obj_path.open("w", encoding="utf-8", newline="\n") as obj:
        obj.write("# ToonCar Character LOD export v102\n")
        obj.write(f"mtllib {mtl_path.name}\n\n")

        vertex_base = 0

        for lod_index, lod in enumerate(layout["lods"]):
            mesh = lod["mesh"]
            obj.write(f"o CharacterLOD_{lod_index:02d}\n")

            for vi in range(mesh["vertex_count"]):
                voff = (
                    mesh["vertex_start"]
                    + vi * OBJECT_VERTEX_STRIDE
                )

                x, y, z = struct.unpack_from("<3f", data, voff)
                nx, ny, nz = struct.unpack_from(
                    "<3f",
                    data,
                    voff + 0x0C,
                )
                u, v = struct.unpack_from(
                    "<2f",
                    data,
                    voff + 0x28,
                )

                obj.write(
                    f"v {x*scale:.9g} "
                    f"{y*scale:.9g} "
                    f"{-z*scale:.9g}\n"
                )
                obj.write(f"vt {u:.9g} {1.0-v:.9g}\n")
                obj.write(
                    f"vn {nx:.9g} {ny:.9g} {-nz:.9g}\n"
                )

            for group in mesh["groups"]:
                obj.write(
                    f"usemtl mat_{int(group['material_id']):03d}\n"
                )

                for fi in range(
                    group["face_start"],
                    group["face_start"] + group["face_count"],
                ):
                    a, b, c = struct.unpack_from(
                        "<3H",
                        data,
                        mesh["face_start"]
                        + fi * OBJECT_FACE_STRIDE,
                    )

                    a = vertex_base + group["vertex_start"] + a + 1
                    b = vertex_base + group["vertex_start"] + b + 1
                    c = vertex_base + group["vertex_start"] + c + 1

                    obj.write(
                        f"f {a}/{a}/{a} "
                        f"{c}/{c}/{c} "
                        f"{b}/{b}/{b}\n"
                    )

            vertex_base += mesh["vertex_count"]
            obj.write("\n")

    skeleton_nodes = []
    for node in layout["skeleton"]["nodes"]:
        skeleton_nodes.append({
            "index": int(node["index"]),
            "parent_index": (
                int(node["parent_index"])
                if node["parent_index"] is not None
                else None
            ),
            "child_indices": [
                int(x) for x in node["child_indices"]
            ],
            "local_matrix": [
                float(x) for x in node["local_matrix"]
            ],
            "global_matrix": [
                float(x) for x in node["global_matrix"]
            ],
        })

    lod_manifests = []

    for lod_index, lod in enumerate(layout["lods"]):
        skin = lod["skin"]
        bone_vertex_groups = {}

        for record_index, record in enumerate(skin["records"]):
            bone_index = skin["record_to_bone"].get(
                record_index
            )
            if bone_index is None:
                continue

            bone_vertex_groups[str(bone_index)] = [
                int(v) for v in record["vertex_indices"]
            ]

        lod_manifests.append({
            "lod_index": lod_index,
            "source_slot": int(lod["slot_index"]),
            "vertex_count": int(lod["mesh"]["vertex_count"]),
            "face_count": int(lod["mesh"]["face_count"]),
            "bone_vertex_groups": bone_vertex_groups,
            "skin_bone_map_count": int(
                skin["bone_map_count"]
            ),
        })

    manifest = {
        "version": 102,
        "asset_type": "character",
        "source": {
            "filename": src.name,
            "path": str(src),
            "size_bytes": len(data),
        },
        "export_scale": scale,
        "asset": {
            "asset_name": src.stem,
            "obj": str(Path("asset") / obj_path.name),
            "mtl": str(Path("asset") / mtl_path.name),
            "texture_directory": str(
                Path("asset")
                / "textures"
                / texture_manifest["directory"]
            ),
            "textures": textures,
            # Character uses a single embedded atlas for all material slots.
            "selected_texture_index": 0 if textures else None,
            "lods": lod_manifests,
            "skeleton": {
                "node_count": len(skeleton_nodes),
                "nodes": skeleton_nodes,
                "source_node_size": 0x110,
                "global_matrix_offset": 0x70,
                "local_matrix_offset": 0x30,
            },
        },
    }

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"Plik: {src.name}")
    log("Typ: Character")
    log(
        f"LOD-y: {len(layout['lods'])}; "
        f"szkielet: {len(skeleton_nodes)} kości/node'ów"
    )

    for lod_index, lod in enumerate(layout["lods"]):
        log(
            f"LOD {lod_index}: "
            f"{lod['mesh']['vertex_count']} vertexów, "
            f"{lod['mesh']['face_count']} trójkątów, "
            f"{len(lod['skin']['record_to_bone'])} użytych kości"
        )

    log(f"Rozpakowano postać: {out}")
    return out



def parse_car_r3d_layout(data: bytes):
    # ToonCar car files have a fixed 0x23C-byte car header, followed by a
    # TextureBank, material table and consecutive ObjectMesh records.
    #
    # Some valid cars contain a short metadata trailer after the final mesh.
    # Older versions required the last ObjectMesh to end exactly at EOF, which
    # rejected Seiscientos.r3d (8 meshes + 0x144-byte trailer).
    if len(data) < 0x23C:
        return None

    bank = try_texture_bank(data, 0x23C)
    if not bank:
        return None

    try:
        materials, cursor = parse_material_records(
            data,
            bank["end_offset"],
        )
    except Exception:
        return None

    if not (1 <= len(materials) <= 64):
        return None

    meshes = []
    while cursor < len(data):
        mesh = try_object_mesh(data, cursor)
        if not mesh:
            break
        meshes.append(mesh)
        cursor = mesh["end_offset"]

        if len(meshes) > 128:
            return None

    if len(meshes) < 4:
        return None

    trailer_offset = cursor
    trailer_size = len(data) - cursor

    # Allow only a small optional metadata tail. A large unparsed remainder
    # means this is probably not the supported car layout.
    if trailer_size < 0 or trailer_size > 0x1000:
        return None

    wheel_positions = [
        struct.unpack_from("<3f", data, 0x24 + i * 0x0C)
        for i in range(4)
    ]
    wheel_radii = [
        struct.unpack_from("<f", data, 0x54 + i * 4)[0]
        for i in range(4)
    ]

    flat_wheel_values = [
        value
        for point in wheel_positions
        for value in point
    ]

    if not all(
        math.isfinite(value) and abs(value) < 10000.0
        for value in flat_wheel_values
    ):
        return None

    if not all(
        math.isfinite(radius) and 0.01 <= radius <= 10.0
        for radius in wheel_radii
    ):
        return None

    if len({
        tuple(round(float(v), 5) for v in point)
        for point in wheel_positions
    }) < 2:
        return None

    def mesh_bbox(mesh):
        xs = []
        ys = []
        zs = []

        for vi in range(mesh["vertex_count"]):
            voff = (
                mesh["vertex_start"]
                + vi * OBJECT_VERTEX_STRIDE
            )
            x, y, z = struct.unpack_from("<3f", data, voff)
            xs.append(x)
            ys.append(y)
            zs.append(z)

        return {
            "min": (min(xs), min(ys), min(zs)),
            "max": (max(xs), max(ys), max(zs)),
            "extent": (
                max(xs) - min(xs),
                max(ys) - min(ys),
                max(zs) - min(zs),
            ),
        }

    mesh_info = []
    for index, mesh in enumerate(meshes):
        bbox = mesh_bbox(mesh)
        ex, ey, ez = bbox["extent"]

        mesh_info.append({
            "index": index,
            "bbox": bbox,
            "vertex_count": mesh["vertex_count"],
            "face_count": mesh["face_count"],
            "is_wheel_shaped": (
                # Wheel meshes are authored around local origin. Use the
                # physical wheel diameter stored in the car header instead of
                # a hard-coded dimension threshold; different cars vary
                # slightly in tire width/shape.
                min(ex, ey, ez)
                >= max(0.10, (sum(wheel_radii) / len(wheel_radii)) * 2.0 * 0.40)
                and max(ex, ey, ez)
                <= max(0.50, (sum(wheel_radii) / len(wheel_radii)) * 2.0 * 1.45)
            ),
            "is_full_car_sized": (
                ex >= 2.0
                and ez >= 3.0
            ),
            "is_flat": min(ex, ey, ez) <= 0.001,
        })

    wheel_mesh_indices = [
        info["index"]
        for info in mesh_info
        if info["is_wheel_shaped"]
    ]

    flat_mesh_indices = [
        info["index"]
        for info in mesh_info
        if info["is_flat"]
    ]

    full_car_mesh_indices = [
        info["index"]
        for info in mesh_info
        if info["is_full_car_sized"]
    ]

    if not full_car_mesh_indices:
        return None

    body_mesh_index = max(
        full_car_mesh_indices,
        key=lambda idx: meshes[idx]["face_count"],
    )

    collision_mesh_indices = [
        idx
        for idx in full_car_mesh_indices
        if idx != body_mesh_index
        and meshes[idx]["face_count"] <= 24
        and not mesh_info[idx]["is_flat"]
    ]

    shadow_mesh_indices = [
        idx
        for idx in flat_mesh_indices
        if meshes[idx]["face_count"] <= 4
    ]

    helper_set = set(
        collision_mesh_indices
        + shadow_mesh_indices
    )

    # Car-sized render meshes form body LODs. Highest triangle count = LOD 0.
    body_lod_mesh_indices = sorted(
        [
            idx
            for idx in full_car_mesh_indices
            if idx not in helper_set
        ],
        key=lambda idx: meshes[idx]["face_count"],
        reverse=True,
    )

    if body_mesh_index in body_lod_mesh_indices:
        body_lod_mesh_indices.remove(body_mesh_index)
    body_lod_mesh_indices.insert(0, body_mesh_index)

    # Wheel-shaped meshes are wheel LODs ordered by detail.
    wheel_lod_levels = sorted(
        wheel_mesh_indices,
        key=lambda idx: meshes[idx]["face_count"],
        reverse=True,
    )

    primary_wheel_mesh_index = (
        wheel_lod_levels[0]
        if wheel_lod_levels
        else None
    )

    wheel_lod_mesh_indices = wheel_lod_levels[1:]

    known_render_set = set(
        body_lod_mesh_indices
        + wheel_lod_levels
    )

    alternate_mesh_indices = [
        idx
        for idx in range(len(meshes))
        if idx not in known_render_set
        and idx not in helper_set
    ]

    return {
        "header_size": 0x23C,
        "texture_bank": bank,
        "materials": materials,
        "object_meshes": meshes,
        "mesh_info": mesh_info,
        "wheel_positions": wheel_positions,
        "wheel_radii": wheel_radii,
        "body_mesh_index": body_mesh_index,
        "body_lod_mesh_indices": body_lod_mesh_indices,
        "wheel_mesh_indices": wheel_mesh_indices,
        "wheel_lod_levels": wheel_lod_levels,
        "primary_wheel_mesh_index": primary_wheel_mesh_index,
        "wheel_lod_mesh_indices": wheel_lod_mesh_indices,
        "collision_mesh_indices": collision_mesh_indices,
        "shadow_mesh_indices": shadow_mesh_indices,
        "alternate_mesh_indices": alternate_mesh_indices,
        "mesh_data_end_offset": cursor,
        "trailer_offset": trailer_offset,
        "trailer_size": trailer_size,
        "trailer_sha256": (
            sha256(
                data[
                    trailer_offset:
                    trailer_offset + trailer_size
                ]
            )
            if trailer_size > 0
            else None
        ),
        "end_offset": len(data),
    }


def export_car_r3d(
    src_path,
    out_path=None,
    scale=0.1,
    texture_variant_index=0,
    export_raw_data=False,
    log=print,
):
    src = Path(src_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(src)
    if src.suffix.lower() != ".r3d":
        raise ValueError("Wybierz plik .r3d")

    data = src.read_bytes()
    layout = parse_car_r3d_layout(data)
    if not layout:
        raise ValueError(
            "Plik nie pasuje do rozpoznanego układu ToonCar Car R3D."
        )

    out = (
        Path(out_path).resolve()
        if out_path
        else src.parent / f"{src.stem}_unpacked_v102"
    )
    out.mkdir(parents=True, exist_ok=True)

    asset_dir = out / "asset"
    asset_dir.mkdir(exist_ok=True)

    texture_manifest = export_texture_banks(
        data,
        [layout["texture_bank"]],
        asset_dir / "textures",
        export_raw_data=export_raw_data,
    )[0]

    entries = texture_manifest["entries"]
    if not entries:
        raise ValueError("Samochód nie zawiera tekstur.")

    texture_variant_index = max(
        0,
        min(int(texture_variant_index), len(entries) - 1),
    )
    selected_texture = entries[texture_variant_index]

    obj_path = asset_dir / f"{src.stem}.obj"
    mtl_path = asset_dir / f"{src.stem}.mtl"

    material_ids = sorted({
        int(group["material_id"])
        for mesh in layout["object_meshes"]
        for group in mesh["groups"]
    })

    with mtl_path.open("w", encoding="utf-8", newline="\n") as mtl:
        for material_id in material_ids:
            mtl.write(f"newmtl mat_{material_id:03d}\n")
            mtl.write("Ka 0 0 0\n")
            mtl.write("Kd 1 1 1\n")
            mtl.write("Ks 0 0 0\n")
            mtl.write("illum 1\n")
            mtl.write(
                f"map_Kd textures/{texture_manifest['directory']}/"
                f"{selected_texture['png']}\n\n"
            )

    with obj_path.open("w", encoding="utf-8", newline="\n") as obj:
        obj.write("# ToonCar Car ObjectMesh export v102\n")
        obj.write(f"mtllib {mtl_path.name}\n\n")

        vertex_base = 0

        body_index = layout["body_mesh_index"]
        wheel_indices = set(layout["wheel_mesh_indices"])
        collision_indices = set(
            layout["collision_mesh_indices"]
        )
        shadow_indices = set(layout["shadow_mesh_indices"])

        for mesh_index, mesh in enumerate(layout["object_meshes"]):
            if mesh_index == body_index:
                role = "Body"
            elif mesh_index == layout["primary_wheel_mesh_index"]:
                role = "Wheel"
            elif mesh_index in set(layout["wheel_lod_mesh_indices"]):
                role = "WheelLOD"
            elif mesh_index in collision_indices:
                role = "Collision"
            elif mesh_index in shadow_indices:
                role = "Shadow"
            else:
                role = "Alternate"

            obj.write(
                f"o CarMesh_{mesh_index:02d}_{role}\n"
            )

            for vi in range(mesh["vertex_count"]):
                voff = (
                    mesh["vertex_start"]
                    + vi * OBJECT_VERTEX_STRIDE
                )

                x, y, z = struct.unpack_from("<3f", data, voff)
                nx, ny, nz = struct.unpack_from(
                    "<3f",
                    data,
                    voff + 0x0C,
                )
                u, v = struct.unpack_from(
                    "<2f",
                    data,
                    voff + 0x28,
                )

                obj.write(
                    f"v {x*scale:.9g} "
                    f"{y*scale:.9g} "
                    f"{-z*scale:.9g}\n"
                )
                obj.write(f"vt {u:.9g} {1.0-v:.9g}\n")
                obj.write(
                    f"vn {nx:.9g} {ny:.9g} {-nz:.9g}\n"
                )

            for group in mesh["groups"]:
                obj.write(
                    f"usemtl mat_{int(group['material_id']):03d}\n"
                )

                for fi in range(
                    group["face_start"],
                    group["face_start"] + group["face_count"],
                ):
                    a, b, c = struct.unpack_from(
                        "<3H",
                        data,
                        mesh["face_start"]
                        + fi * OBJECT_FACE_STRIDE,
                    )

                    a = (
                        vertex_base
                        + group["vertex_start"]
                        + a + 1
                    )
                    b = (
                        vertex_base
                        + group["vertex_start"]
                        + b + 1
                    )
                    c = (
                        vertex_base
                        + group["vertex_start"]
                        + c + 1
                    )

                    obj.write(
                        f"f {a}/{a}/{a} "
                        f"{c}/{c}/{c} "
                        f"{b}/{b}/{b}\n"
                    )

            vertex_base += mesh["vertex_count"]
            obj.write("\n")

    textures = [
        {
            "index": int(entry["index"]),
            "source_name": entry["name"],
            "png": entry["png"],
            "has_alpha": bool(entry.get("has_alpha")),
            "alpha": entry.get("alpha") or {},
        }
        for entry in entries
    ]

    manifest = {
        "version": 102,
        "asset_type": "car",
        "source": {
            "filename": src.name,
            "path": str(src),
            "size_bytes": len(data),
        },
        "export_scale": scale,
        "asset": {
            "asset_name": src.stem,
            "layout": (
                "0x23C header + TextureBank + material table + "
                "consecutive ObjectMesh[]"
            ),
            "obj": str(Path("asset") / obj_path.name),
            "mtl": str(Path("asset") / mtl_path.name),
            "texture_directory": str(
                Path("asset")
                / "textures"
                / texture_manifest["directory"]
            ),
            "textures": textures,
            "selected_texture_index": texture_variant_index,
            "selected_texture_name": selected_texture["name"],
            "material_count": len(layout["materials"]),
            "mesh_count": len(layout["object_meshes"]),
            "trailer": {
                "offset": layout.get("trailer_offset"),
                "size": layout.get("trailer_size", 0),
                "sha256": layout.get("trailer_sha256"),
            },
            "assembly": {
                "body_mesh_index": int(
                    layout["body_mesh_index"]
                ),
                "body_lod_mesh_indices": [
                    int(x)
                    for x in layout["body_lod_mesh_indices"]
                ],
                "wheel_mesh_indices": [
                    int(x)
                    for x in layout["wheel_mesh_indices"]
                ],
                "primary_wheel_mesh_index": (
                    int(layout["primary_wheel_mesh_index"])
                    if layout["primary_wheel_mesh_index"] is not None
                    else None
                ),
                "wheel_lod_mesh_indices": [
                    int(x)
                    for x in layout["wheel_lod_mesh_indices"]
                ],
                "wheel_lod_levels": [
                    int(x)
                    for x in layout["wheel_lod_levels"]
                ],
                "collision_mesh_indices": [
                    int(x)
                    for x in layout["collision_mesh_indices"]
                ],
                "shadow_mesh_indices": [
                    int(x)
                    for x in layout["shadow_mesh_indices"]
                ],
                "alternate_mesh_indices": [
                    int(x)
                    for x in layout["alternate_mesh_indices"]
                ],
                "wheel_positions_source": [
                    [float(v) for v in point]
                    for point in layout["wheel_positions"]
                ],
                "wheel_radii_source": [
                    float(v)
                    for v in layout["wheel_radii"]
                ],
            },
            "meshes": [
                {
                    "index": index,
                    "offset": mesh["offset"],
                    "group_count": mesh["group_count"],
                    "vertex_count": mesh["vertex_count"],
                    "face_count": mesh["face_count"],
                }
                for index, mesh in enumerate(layout["object_meshes"])
            ],
        },
    }

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"Plik: {src.name}")
    log("Typ: Samochód / Car")
    log(f"Części ObjectMesh: {len(layout['object_meshes'])}")
    if layout.get("trailer_size", 0):
        log(
            "Dodatkowy trailer auta: "
            f"{layout['trailer_size']} B "
            f"@ 0x{layout['trailer_offset']:X}"
        )
    log(
        f"Body LODs: {layout['body_lod_mesh_indices']}; "
        f"wheel LODs: {layout['wheel_lod_levels']}; "
        f"collision: {layout['collision_mesh_indices']}; "
        f"shadow: {layout['shadow_mesh_indices']}; "
        f"alternate: {layout['alternate_mesh_indices']}"
    )
    log(
        "Geometria łącznie: "
        f"{sum(m['vertex_count'] for m in layout['object_meshes'])} vertexów, "
        f"{sum(m['face_count'] for m in layout['object_meshes'])} trójkątów"
    )
    log(
        f"Wariant tekstury: {selected_texture['name']} "
        f"({texture_variant_index + 1}/{len(entries)})"
    )
    log(f"Rozpakowano samochód: {out}")

    return out



def unpack_standalone_object_r3d(
    src_path,
    out_path=None,
    scale=0.1,
    export_raw_data=False,
    log=print,
):
    src = Path(src_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(src)
    if src.suffix.lower() != ".r3d":
        raise ValueError("Wybierz plik .r3d")

    out = (
        Path(out_path).resolve()
        if out_path
        else src.parent / f"{src.stem}_unpacked_v102"
    )
    out.mkdir(parents=True, exist_ok=True)

    log(f"Plik: {src.name}")
    log("Typ: Standalone ObjectMesh")
    log(f"Skala eksportu: {scale:g}")

    asset_dir = out / "asset"

    details = export_simple_objectmesh_asset(
        src,
        asset_dir,
        scale,
        asset_name=src.stem,
        relative_prefix=Path("asset"),
        export_raw_data=export_raw_data,
    )

    if not details:
        raise ValueError(
            "Plik nie pasuje do układu Standalone ObjectMesh: "
            "TextureBank + uint32 mesh_size + ObjectMesh."
        )

    log(
        f"Geometria: {details['mesh']['vertex_count']} vertexów, "
        f"{details['mesh']['face_count']} trójkątów"
    )
    log(f"Tekstury: {details['texture_count']}")

    manifest = {
        "version": 102,
        "asset_type": "standalone_object",
        "source": {
            "filename": src.name,
            "path": str(src),
            "size_bytes": src.stat().st_size,
        },
        "export_scale": scale,
        "asset": details,
    }

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"Rozpakowano asset: {out}")
    return out




def export_embedded_cone_assets(
    track_out,
    scale,
    export_raw_data=False,
    log=print,
):
    track_out = Path(track_out).resolve()
    cone_root = track_out / "gameplay_assets" / "cones"
    static_out = cone_root / "static"
    moving_out = cone_root / "moving"

    for package in (static_out, moving_out):
        if package.exists():
            shutil.rmtree(package, ignore_errors=True)

    result = {"static": None, "moving": None}

    def convert_nested_manifest(
        package_root,
        manifest,
        preferred_object_name=None,
    ):
        asset = manifest["asset"]
        package_rel = package_root.relative_to(track_out)

        converted = {
            "asset_type": manifest.get("asset_type"),
            "obj": str(package_rel / asset["obj"]).replace("\\", "/"),
            "mtl": str(package_rel / asset["mtl"]).replace("\\", "/"),
            "texture_directory": str(
                package_rel / asset["texture_directory"]
            ).replace("\\", "/"),
            "textures": asset.get("textures") or [],
            "preferred_object_name": preferred_object_name,
            "source_mode": "embedded",
        }

        if "mesh" in asset:
            converted["mesh"] = asset["mesh"]
        if "lods" in asset:
            converted["lods"] = asset["lods"]

        return converted

    with tempfile.TemporaryDirectory(prefix="tooncar_cones_") as temp_text:
        temp_dir = Path(temp_text)
        static_src = temp_dir / "Cono.r3d"
        moving_src = temp_dir / "ConoPatas.r3d"
        static_src.write_bytes(get_embedded_cono_r3d_bytes())
        moving_src.write_bytes(get_embedded_conopatas_r3d_bytes())

        try:
            export_simple_metadata_object_r3d(
                static_src,
                static_out,
                scale,
                export_raw_data=export_raw_data,
                log=lambda *_: None,
            )
            static_manifest = json.loads(
                (static_out / "manifest.json").read_text(encoding="utf-8")
            )
            result["static"] = convert_nested_manifest(
                static_out,
                static_manifest,
            )
            log("Cono.r3d: wbudowany model zwykłego pachołka przygotowany.")
        except Exception as exc:
            log(f"Cono.r3d: nie udało się przygotować modelu ({exc})")

        try:
            export_rigged_object_r3d(
                moving_src,
                moving_out,
                scale,
                export_raw_data=export_raw_data,
                log=lambda *_: None,
            )
            moving_manifest = json.loads(
                (moving_out / "manifest.json").read_text(encoding="utf-8")
            )
            result["moving"] = convert_nested_manifest(
                moving_out,
                moving_manifest,
                preferred_object_name="CharacterLOD_00",
            )
            log("ConoPatas.r3d: wbudowany model ruchomego pachołka przygotowany.")
        except Exception as exc:
            log(f"ConoPatas.r3d: nie udało się przygotować modelu ({exc})")

    return result



def unpack_r3d(
    src_path,
    out_path=None,
    scale=0.1,
    export_raw_data=False,
    log=print,
):
    src = Path(src_path).resolve()
    if not src.exists():
        raise FileNotFoundError(src)
    if src.suffix.lower() != ".r3d":
        raise ValueError("Wybierz plik .r3d")

    data = src.read_bytes()
    out = Path(out_path).resolve() if out_path else src.parent / f"{src.stem}_unpacked_v102"
    out.mkdir(parents=True, exist_ok=True)

    log(f"Plik: {src.name}")
    log(f"Rozmiar: {len(data):,} B")
    log("Parser: code-guided (ToonCar.exe)")

    sorpresa_asset_manifest = None

    sorpresa_asset_path = (
        materialize_embedded_sorpresa_r3d(
            out
            / "gameplay_assets"
            / "_embedded_source"
        )
    )

    log(
        "Sorpresa: używam modelu z gry wbudowanego w skrypt."
    )

    try:
        sorpresa_asset_manifest = export_simple_objectmesh_asset(
            sorpresa_asset_path,
            out / "gameplay_assets" / "sorpresa",
            scale,
            asset_name="Sorpresa",
            relative_prefix=(
                Path("gameplay_assets")
                / "sorpresa"
            ),
            export_raw_data=export_raw_data,
        )
    except Exception as exc:
        log(
            "Sorpresa.r3d: nie udało się rozpakować "
            f"({exc})"
        )
        sorpresa_asset_manifest = None

    if not export_raw_data:
        try:
            sorpresa_asset_path.unlink(
                missing_ok=True
            )
            embedded_source_dir = (
                sorpresa_asset_path.parent
            )
            if embedded_source_dir.is_dir():
                try:
                    embedded_source_dir.rmdir()
                except OSError:
                    pass
        except Exception:
            pass

    if sorpresa_asset_manifest:
        sorpresa_asset_manifest[
            "source_mode"
        ] = "embedded"
        log(
            "Sorpresa.r3d: wbudowany model skrzynki rozpakowany "
            f"({sorpresa_asset_manifest['mesh']['vertex_count']} vertexów, "
            f"{sorpresa_asset_manifest['mesh']['face_count']} trójkątów)"
        )
    else:
        log(
            "Sorpresa.r3d: wbudowany model nie został rozpoznany — "
            "Blender użyje placeholderów."
        )

    top = parse_code_guided_top_level(data)

    log(f"Główny bank tekstur: {top['primary_texture_bank']['count']}")
    log(f"Materiały 0x60 B: {len(top['materials'])}")
    log(f"Animacje 0x1A8 B: {len(top['animations'])}")
    mm = top["main_mesh"]
    log(
        f"Main mesh: {mm['vertex_count']} vertexów, "
        f"{mm['face_count']} trójkątów, {mm['material_count']} slotów"
    )

    # Save exact material/animation interpretation.
    meta_dir = out / "metadata"
    meta_dir.mkdir(exist_ok=True)

    (meta_dir / "materials.json").write_text(
        json.dumps(top["materials"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (meta_dir / "texture_animations.json").write_text(
        json.dumps(top["animations"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Optional exact raw top-level records for reverse engineering.
    material_raw_start = (
        top["primary_texture_bank"]["end_offset"]
        + 4
    )
    material_raw_end = (
        material_raw_start
        + len(top["materials"])
        * MATERIAL_RECORD_SIZE
    )

    anim_count_off = material_raw_end
    anim_raw_start = anim_count_off + 4
    anim_raw_end = (
        anim_raw_start
        + len(top["animations"])
        * ANIMATION_RECORD_SIZE
    )

    material_raw_path = (
        meta_dir / "materials.raw.bin"
    )
    animation_raw_path = (
        meta_dir
        / "texture_animations.raw.bin"
    )

    if export_raw_data:
        material_raw_path.write_bytes(
            data[
                material_raw_start:
                material_raw_end
            ]
        )
        animation_raw_path.write_bytes(
            data[
                anim_raw_start:
                anim_raw_end
            ]
        )
    else:
        material_raw_path.unlink(
            missing_ok=True
        )
        animation_raw_path.unlink(
            missing_ok=True
        )

    # Extract every texture bank, including unaligned trailing sky banks.
    banks = find_all_texture_banks(data)
    tex_manifest = export_texture_banks(
        data,
        banks,
        out / "textures",
        export_raw_data=export_raw_data,
    )
    log(f"Wszystkie rozpoznane banki tekstur: {len(banks)}")
    log(f"Wszystkie rozpoznane tekstury: {sum(b['count'] for b in banks)}")

    skybox_manifest = detect_skybox_bank(tex_manifest)
    if skybox_manifest:
        log("Skybox: znaleziono komplet UP/DN/FR/BK/LF/RT")
    else:
        log("Skybox: nie znaleziono kompletnego zestawu 6 ścian")

    # Build Blender-ready mapping using the exact hash function and
    # material field used by ToonCar.exe.
    primary_bank_manifest = next(
        (b for b in tex_manifest if b["offset"] == 0),
        tex_manifest[0] if tex_manifest else None,
    )

    if primary_bank_manifest:
        (
            main_mapping_status,
            main_texture_mapping,
            unresolved_material_slots,
            texture_hash_collisions,
        ) = build_exact_main_mesh_mapping(
            mm,
            top["materials"],
            primary_bank_manifest,
            top["animations"],
        )
    else:
        main_mapping_status = "no_primary_texture_bank"
        main_texture_mapping = []
        unresolved_material_slots = list(range(mm["material_count"]))
        texture_hash_collisions = {}

    (meta_dir / "main_mesh_material_mapping.json").write_text(
        json.dumps(
            {
                "status": main_mapping_status,
                "method": (
                    "material slot -> material record -> DWORD +0x38 -> "
                    "ToonCar.exe 0x47C6E0 filename hash -> embedded texture"
                ),
                "material_count": mm["material_count"],
                "resolved_count": (
                    mm["material_count"] - len(unresolved_material_slots)
                ),
                "unresolved_material_slots": unresolved_material_slots,
                "hash_collisions": texture_hash_collisions,
                "mapping": main_texture_mapping,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log(
        f"Main mesh materiały → tekstury: {main_mapping_status} "
        f"({mm['material_count'] - len(unresolved_material_slots)}/"
        f"{mm['material_count']} rozwiązanych)"
    )

    # Extract every mesh matching the exact loader layout.
    meshes = find_all_meshes(data)
    mesh_manifest = export_meshes(
        data,
        meshes,
        out / "meshes",
        scale,
        main_mesh_offset=mm["offset"],
        main_texture_mapping=main_texture_mapping,
        main_texture_bank_dir=(
            primary_bank_manifest["directory"]
            if primary_bank_manifest
            else None
        ),
        export_raw_data=export_raw_data,
    )
    log(f"Wszystkie rozpoznane meshe TrackMesh: {len(meshes)}")

    # Decode the second geometry format used by cars, characters and track props.
    object_meshes = find_all_object_meshes(data)
    log(f"Wszystkie rozpoznane ObjectMesh (0x46FAC0): {len(object_meshes)}")

    prop_scene = find_prop_scene_table(data, object_meshes)
    placed_props_manifest = None
    animated_props_manifest = None
    gameplay_data = decode_gameplay_track_data(
        data,
        prop_scene,
    )
    cone_assets_manifest = None

    if gameplay_data:
        static_cone_count = int(
            (gameplay_data.get("conos") or {}).get("count", 0)
        )
        moving_cone_count = len(
            gameplay_data.get("moving_cone_paths") or []
        )

        if static_cone_count > 0 or moving_cone_count > 0:
            cone_assets_manifest = export_embedded_cone_assets(
                out,
                scale,
                export_raw_data=export_raw_data,
                log=log,
            )
        (meta_dir / "gameplay_track_data.json").write_text(
            json.dumps(gameplay_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(
            "Gameplay mapy: dane odczytane."
        )
    else:
        log("Gameplay: nie rozpoznano Way/Sorpresa")

    if prop_scene and primary_bank_manifest:
        log(
            f"Scena propów: {prop_scene['root_count']} definicje / "
            f"{prop_scene['instance_count']} instancji "
            f"@ 0x{prop_scene['offset']:X}"
        )
        log(
            f"Parser propów: "
            f"{prop_scene.get('parser_mode', 'unknown')}"
        )

        placed_props_manifest = export_placed_props_obj(
            data,
            prop_scene,
            out / "props",
            scale,
            top["materials"],
            primary_bank_manifest,
        )

        log(
            f"Umieszczone części propów: "
            f"{placed_props_manifest['exported_part_instances']}"
        )

        animated_props_manifest = export_animated_props_obj(
            data,
            prop_scene,
            out / "animated_props",
            scale,
            top["materials"],
            primary_bank_manifest,
        )

        if animated_props_manifest:
            log(
                "Animowane propy 3D: "
                f"{animated_props_manifest['definition_count']} definicje, "
                f"{animated_props_manifest['animated_instance_count']} instancji, "
                f"{animated_props_manifest['template_mesh_count']} meshe "
                "(TRS animation decoded)"
            )
    else:
        log(
            "Scena propów: nie rozpoznano kompletnej tabeli "
            "Root + 0x44 Instance; ObjectMesh zachowano diagnostycznie."
        )

    refs = find_filename_references(data)
    (out / "filename_references.txt").write_text(
        "".join(f"0x{x['offset']:08X}  {x['value']}\n" for x in refs),
        encoding="utf-8",
    )

    # Mark confirmed/extracted regions. Anything else is preserved.
    known_ranges = []
    for b in banks:
        known_ranges.append((b["offset"], b["end_offset"]))
    for m in meshes:
        known_ranges.append((m["offset"], m["end_offset"]))

    for m in object_meshes:
        known_ranges.append((m["offset"], m["end_offset"]))

    if prop_scene:
        known_ranges.append((prop_scene["offset"], prop_scene["end_offset"]))

    if gameplay_data:
        known_ranges.append((
            gameplay_data["offset"],
            gameplay_data["end_offset"],
        ))

    # The material and animation prefix is semantically known even though it is
    # separate from texture/mesh extraction.
    known_ranges.append((
        top["primary_texture_bank"]["end_offset"],
        mm["offset"],
    ))

    unknown_raw_dir = (
        out / "unknown_raw"
    )

    if export_raw_data:
        unknown = export_unknown_chunks(
            data,
            known_ranges,
            unknown_raw_dir,
        )
        log(
            "Nierozpoznane zakresy zachowane jako raw: "
            f"{len(unknown)}"
        )
    else:
        unknown = []
        if unknown_raw_dir.exists():
            shutil.rmtree(
                unknown_raw_dir,
                ignore_errors=True,
            )
        log(
            "Surowe dane diagnostyczne: pominięte."
        )

    manifest = {
        "format": "ToonCar R3D Code-Guided Unpacker",
        "version": 102,
        "source": {
            "filename": src.name,
            "size": len(data),
            "sha256": sha256(data),
        },
        "export_raw_data": bool(
            export_raw_data
        ),
        "verified_from_exe": {
            "texture_loader": "0x4541A0",
            "mesh_loader": "0x460880",
            "track_loader": "0x447630",
            "resource_name_hash": "0x47C6E0",
            "material_texture_hash_offset": "0x38",
            "prop_root_loader": "0x449860",
            "prop_part_loader": "0x449810",
            "object_mesh_loader": "0x46FAC0",
            "spatial_loader": "0x45B530",
            "prop_instance_record_size": "0x44",
            "animated_model_loader": "0x452840",
            "animated_node_loader": "0x4751A0",
            "animation_set_loader": "0x4734E0",
            "animation_track_sampler": "0x473020",
            "map_animation_setup": "0x401993 -> 0x474620",
            "map_animation_phase_step": "1/6",
            "mesh_animation_key_size": "0x28",
            "way_script_command": "Way",
            "way_vector_serializer": "0x475C50",
            "way_runtime_builder": "0x446230",
            "way_driver_attachment": "0x416953 / 0x4176A3",
            "sorpresa_script_command": "Sorpresa",
            "sorpresa_runtime_spawn": "0x414880",
            "sorpresa_record_size": "0x0C",
            "material_record_size": MATERIAL_RECORD_SIZE,
            "animation_record_size": ANIMATION_RECORD_SIZE,
            "mesh_header_size": MESH_HEADER_SIZE,
            "vertex_stride": VERTEX_STRIDE,
            "face_stride": FACE_STRIDE,
        },
        "export_scale": scale,
        "confirmed_top_level": {
            "primary_texture_bank_offset": 0,
            "material_count": len(top["materials"]),
            "animation_count": len(top["animations"]),
            "main_mesh_offset": mm["offset"],
            "confirmed_prefix_end": top["confirmed_prefix_end"],
        },
        "texture_banks": tex_manifest,
        "skybox": skybox_manifest,
        "materials": top["materials"],
        "texture_animations": top["animations"],
        "main_mesh_material_mapping": {
            "status": main_mapping_status,
            "method": "material+0x38 -> ToonCar filename hash",
            "resolved_count": (
                mm["material_count"] - len(unresolved_material_slots)
            ),
            "unresolved_material_slots": unresolved_material_slots,
            "hash_collisions": texture_hash_collisions,
            "mapping": main_texture_mapping,
        },
        "meshes": mesh_manifest,
        "object_meshes": object_meshes,
        "placed_props": placed_props_manifest,
        "animated_props": animated_props_manifest,
        "gameplay_data": gameplay_data,
        "gameplay_assets": {
            "sorpresa": sorpresa_asset_manifest,
            "cones": cone_assets_manifest,
        },
        "filename_references": refs,
        "unknown_raw_chunks": unknown,
    }

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log("")
    log("=" * 60)
    log("GOTOWE")
    log(f"Folder wynikowy: {out}")
    log("=" * 60)

    return out



def find_blender_executable():
    """
    Find Blender on Windows without requiring configuration in the common case.
    """
    candidates = []

    env_path = os.environ.get("BLENDER_PATH")
    if env_path:
        candidates.append(Path(env_path))

    which = shutil.which("blender")
    if which:
        candidates.append(Path(which))

    if os.name == "nt":
        program_files = [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramW6432"),
            os.environ.get("ProgramFiles(x86)"),
        ]

        for base in [x for x in program_files if x]:
            root = Path(base)

            foundation = root / "Blender Foundation"
            if foundation.exists():
                try:
                    for d in sorted(
                        foundation.glob("Blender *"),
                        reverse=True,
                    ):
                        candidates.append(d / "blender.exe")
                except OSError:
                    pass

            candidates.append(
                root / "Steam" / "steamapps" / "common" / "Blender" / "blender.exe"
            )

        # Typical Steam default when Steam is under Program Files (x86).
        candidates.append(
            Path(r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe")
        )

    seen = set()
    for candidate in candidates:
        try:
            candidate = candidate.expanduser()
            key = str(candidate).lower()
        except Exception:
            continue

        if key in seen:
            continue
        seen.add(key)

        if candidate.is_file():
            return candidate.resolve()

    return None


def write_blender_builder_script(path: Path):
    """
    Generate a self-contained script executed by Blender's bundled Python.
    It imports all OBJ meshes produced by the unpacker, arranges them in
    collections, then saves a .blend file.
    """
    code = r"""
import bpy
import math
import numpy as np
import json
import sys
import shutil
import re
from pathlib import Path
from mathutils import Matrix, Quaternion, Vector


def args_after_double_dash():
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1:]


def import_obj(filepath: Path):
    before = set(bpy.data.objects)

    # Blender 4.x
    try:
        bpy.ops.wm.obj_import(
            filepath=str(filepath),
            forward_axis='NEGATIVE_Z',
            up_axis='Y',
        )
    except Exception:
        # Blender 3.x fallback when legacy OBJ importer is available.
        bpy.ops.import_scene.obj(
            filepath=str(filepath),
            axis_forward='-Z',
            axis_up='Y',
        )

    return [obj for obj in bpy.data.objects if obj not in before]


def move_objects_to_collection(objects, collection):
    for obj in objects:
        for old_collection in list(obj.users_collection):
            old_collection.objects.unlink(obj)
        if obj.name not in collection.objects:
            collection.objects.link(obj)





def source_row_matrix_to_blender(values, length_scale=0.1):
    # ToonCar matrices are Direct3D row-vector matrices:
    #   [x y z 1] * M
    # with translation at 12/13/14.
    #
    # Convert to a column-vector matrix first (transpose), then apply the
    # coordinate basis used by the verified OBJ path:
    #   ToonCar (X,Y,Z) -> Blender (X,Z,Y)
    source_row = Matrix((
        values[0:4],
        values[4:8],
        values[8:12],
        values[12:16],
    ))

    source_column = source_row.transposed()

    basis = Matrix((
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    ))

    result = basis @ source_column @ basis

    result[0][3] *= length_scale
    result[1][3] *= length_scale
    result[2][3] *= length_scale

    return result


def source_trs_key_to_blender_matrix(key, length_scale=0.1):
    qx, qy, qz, qw = key["quaternion_xyzw"]
    tx, ty, tz = key["translation"]
    sx, sy, sz = key["scale"]

    q = Quaternion((qw, qx, qy, qz))
    q.normalize()

    # The game's stored node matrix for key 0 matches Quaternion.to_matrix()
    # in the serialized row-matrix upper 3x3. Scale is represented as a
    # row-vector pre-scale (S * R). Current Luna/Castilla tracks use 1,1,1.
    rotation = q.to_matrix()

    source_row = Matrix.Identity(4)

    for r in range(3):
        row_scale = (sx, sy, sz)[r]
        for c in range(3):
            source_row[r][c] = rotation[r][c] * row_scale

    source_row[3][0] = tx
    source_row[3][1] = ty
    source_row[3][2] = tz

    flat = [
        source_row[r][c]
        for r in range(4)
        for c in range(4)
    ]

    return source_row_matrix_to_blender(
        flat,
        length_scale=length_scale,
    )


def interpolate_tooncar_key(key_a, key_b, factor):
    qa = Quaternion((
        key_a["quaternion_xyzw"][3],
        key_a["quaternion_xyzw"][0],
        key_a["quaternion_xyzw"][1],
        key_a["quaternion_xyzw"][2],
    ))
    qb = Quaternion((
        key_b["quaternion_xyzw"][3],
        key_b["quaternion_xyzw"][0],
        key_b["quaternion_xyzw"][1],
        key_b["quaternion_xyzw"][2],
    ))

    qa.normalize()
    qb.normalize()

    # mathutils.slerp uses shortest-arc quaternion interpolation, matching
    # the sign-corrected SLERP routine at ToonCar.exe 0x46A060.
    q = qa.slerp(qb, factor)

    ta = Vector(key_a["translation"])
    tb = Vector(key_b["translation"])
    translation = ta.lerp(tb, factor)

    sa = Vector(key_a["scale"])
    sb = Vector(key_b["scale"])
    scale = sa.lerp(sb, factor)

    return {
        "quaternion_xyzw": [
            q.x,
            q.y,
            q.z,
            q.w,
        ],
        "translation": list(translation),
        "scale": list(scale),
    }


def apply_local_matrix_as_trs(obj, matrix):
    location, rotation, scale = matrix.decompose()

    obj.location = location
    obj.rotation_mode = 'QUATERNION'
    obj.rotation_quaternion = rotation
    obj.scale = scale


def keyframe_local_trs(obj, frame):
    obj.keyframe_insert(
        data_path="location",
        frame=frame,
        group="ToonCar Transform",
    )
    obj.keyframe_insert(
        data_path="rotation_quaternion",
        frame=frame,
        group="ToonCar Transform",
    )
    obj.keyframe_insert(
        data_path="scale",
        frame=frame,
        group="ToonCar Transform",
    )


def iter_object_action_fcurves(obj):
    # Return F-Curves for the Action slot used by this object.
    # Older Blender exposes action.fcurves directly.
    # Newer Blender uses Action Layers / Strips / Channelbags / Slots.
    animation_data = obj.animation_data
    if not animation_data or not animation_data.action:
        return []

    action = animation_data.action

    # Legacy Action API.
    try:
        legacy = list(action.fcurves)
        if legacy:
            return legacy
    except Exception:
        pass

    # Layered Action API.
    curves = []
    action_slot = None

    try:
        action_slot = animation_data.action_slot
    except Exception:
        action_slot = None

    try:
        for layer in action.layers:
            for strip in layer.strips:
                try:
                    channelbags = strip.channelbags
                except Exception:
                    continue

                for channelbag in channelbags:
                    if action_slot is not None:
                        try:
                            if channelbag.slot != action_slot:
                                continue
                        except Exception:
                            pass

                    try:
                        curves.extend(
                            list(channelbag.fcurves)
                        )
                    except Exception:
                        pass
    except Exception:
        pass

    return curves


def make_action_linear_and_cyclic(obj):
    # Force true LINEAR interpolation and infinite F-Curve repetition.
    animation_data = obj.animation_data
    if not animation_data or not animation_data.action:
        return 0

    fcurves = iter_object_action_fcurves(obj)

    if not fcurves:
        obj["tooncar_animation_fcurve_error"] = (
            "No F-Curves found in Action slot"
        )
        return 0

    modified = 0

    for fcurve in fcurves:
        for point in fcurve.keyframe_points:
            point.interpolation = 'LINEAR'

            try:
                point.handle_left_type = 'VECTOR'
                point.handle_right_type = 'VECTOR'
            except Exception:
                pass

        cycles = None

        try:
            for modifier in fcurve.modifiers:
                if modifier.type == 'CYCLES':
                    cycles = modifier
                    break
        except Exception:
            cycles = None

        if cycles is None:
            cycles = fcurve.modifiers.new(
                type='CYCLES'
            )

        cycles.mode_before = 'REPEAT'
        cycles.mode_after = 'REPEAT'
        modified += 1

    obj["tooncar_animation_fcurve_count"] = len(
        fcurves
    )
    obj["tooncar_animation_cycles_count"] = (
        modified
    )
    obj["tooncar_animation_interpolation"] = (
        "LINEAR"
    )

    return modified




def set_fcurve_cycle_mode_for_path(
    obj,
    data_path,
    array_index=None,
    after_mode='REPEAT',
    before_mode='REPEAT',
):
    # Adjust an existing Cycles modifier for selected channels.
    changed = 0

    for fcurve in iter_object_action_fcurves(obj):
        if fcurve.data_path != data_path:
            continue

        if (
            array_index is not None
            and int(fcurve.array_index) != int(array_index)
        ):
            continue

        for modifier in fcurve.modifiers:
            if modifier.type == 'CYCLES':
                modifier.mode_before = before_mode
                modifier.mode_after = after_mode
                changed += 1
                break

    return changed


def bake_tooncar_track_action(
    obj,
    track,
    length_scale,
    ticks_per_key=6,
):
    # Store exactly one complete ToonCar cycle.
    #
    # Important: Blender LINEAR interpolation of quaternion *components* is
    # not equivalent to ToonCar's shortest-arc SLERP and causes visible speed
    # changes near some keys / at the loop seam. Therefore sample the verified
    # ToonCar interpolation once per game tick, but only for ONE cycle.
    keys = track.get("keys", [])
    if not keys:
        return 0

    ticks_per_key = max(
        1,
        int(ticks_per_key),
    )
    cycle_ticks = len(keys) * ticks_per_key

    for tick in range(cycle_ticks + 1):
        # The final tick resolves to key 0 again, explicitly closing the cycle.
        if tick == cycle_ticks:
            sampled = keys[0]
        else:
            key_index = tick // ticks_per_key
            sub_tick = tick % ticks_per_key
            next_index = (
                key_index + 1
            ) % len(keys)

            factor = (
                sub_tick
                / float(ticks_per_key)
            )

            sampled = interpolate_tooncar_key(
                keys[key_index],
                keys[next_index],
                factor,
            )

        matrix = source_trs_key_to_blender_matrix(
            sampled,
            length_scale=length_scale,
        )
        apply_local_matrix_as_trs(
            obj,
            matrix,
        )
        keyframe_local_trs(
            obj,
            1 + tick,
        )

    modified_curve_count = (
        make_action_linear_and_cyclic(obj)
    )

    obj["tooncar_animation_storage"] = (
        "one_cycle_tick_sampled"
    )
    obj["tooncar_animation_cycles_modifier"] = bool(
        modified_curve_count
    )
    obj["tooncar_animation_source_key_count"] = len(
        keys
    )
    obj["tooncar_animation_cycle_ticks"] = (
        cycle_ticks
    )
    obj["tooncar_animation_sample_rate"] = (
        "one_key_per_game_tick"
    )
    obj["tooncar_animation_rotation_interpolation"] = (
        "source_slerp_sampled"
    )

    return cycle_ticks



def build_animated_props(
    manifest,
    root,
    collection,
    enabled=True,
):
    if not enabled:
        return []

    animated = manifest.get("animated_props")
    if not animated or not animated.get("obj") or not animated.get("json"):
        return []

    obj_path = root / animated["obj"]
    json_path = root / animated["json"]

    if not obj_path.exists() or not json_path.exists():
        return []

    details = json.loads(
        json_path.read_text(encoding="utf-8")
    )

    definitions = {
        int(definition["root_index"]): definition
        for definition in details.get("definitions", [])
        if definition.get("supported")
    }

    if not definitions:
        return []

    imported_templates = import_obj(obj_path)
    move_objects_to_collection(
        imported_templates,
        collection,
    )

    templates = {}
    template_import_matrices = {}

    for template in imported_templates:
        templates[template.name] = template

        # Keep Blender's OBJ-axis conversion. Depending on Blender/importer
        # version this can live on the Object transform rather than being
        # baked into Mesh vertices. Animated attachment math must preserve it.
        template_import_matrices[template.name] = (
            template.matrix_world.copy()
        )

    # Blender's OBJ importer can append ".001". Resolve by prefix as fallback.
    def template_for(name):
        if name in templates:
            return (
                templates[name],
                template_import_matrices.get(
                    name,
                    Matrix.Identity(4),
                ).copy(),
            )

        for template_name, template in templates.items():
            if (
                template_name == name
                or template_name.startswith(name + ".")
            ):
                return (
                    template,
                    template_import_matrices.get(
                        template_name,
                        Matrix.Identity(4),
                    ).copy(),
                )

        return (None, Matrix.Identity(4))

    length_scale = float(
        manifest.get("export_scale", 0.1)
    )

    # Map animated props are initialized by 0x401993 with phase step 1/6.
    ticks_per_key = 6

    # Build an independent one-cycle Action for every placed instance/node.
    # Each Action contains one tick-sampled cycle using ToonCar's verified
    # interpolation, then F-Curve Cycles repeats it indefinitely.
    reports = []

    bpy.context.scene.render.fps = 55
    bpy.context.scene.render.fps_base = 1.0

    max_frame = 1

    for instance in details.get("instances", []):
        root_index = int(instance["root_index"])
        definition = definitions.get(root_index)

        if not definition:
            continue

        instance_index = int(instance["index"])

        instance_empty = bpy.data.objects.new(
            f"Animated Prop {instance_index:03d} Root {root_index:02d}",
            None,
        )
        collection.objects.link(instance_empty)

        instance_empty.empty_display_type = 'PLAIN_AXES'
        instance_empty.empty_display_size = 0.25
        instance_empty.matrix_world = source_row_matrix_to_blender(
            instance["matrix_row_major"],
            length_scale=length_scale,
        )

        instance_empty["tooncar_role"] = "animated_prop_instance"
        instance_empty["tooncar_root_index"] = root_index
        instance_empty["tooncar_instance_index"] = instance_index
        instance_empty["tooncar_animation_phase_step"] = 1.0 / 6.0
        instance_empty["tooncar_animation_loop_mode"] = "forward_loop"

        node_objects = {}

        for node in sorted(
            definition.get("nodes", []),
            key=lambda item: int(item["index"]),
        ):
            node_index = int(node["index"])

            node_obj = bpy.data.objects.new(
                (
                    f"anim_i{instance_index:03d}_"
                    f"r{root_index:02d}_n{node_index:02d}"
                ),
                None,
            )
            collection.objects.link(node_obj)
            node_objects[node_index] = node_obj

            parent_index = node.get("parent_index")

            if parent_index is None:
                node_obj.parent = instance_empty
            else:
                node_obj.parent = node_objects.get(
                    int(parent_index),
                    instance_empty,
                )

            static_matrix = source_row_matrix_to_blender(
                node["static_matrix_row_major"],
                length_scale=length_scale,
            )
            apply_local_matrix_as_trs(
                node_obj,
                static_matrix,
            )

            node_obj["tooncar_role"] = "animated_prop_node"
            node_obj["tooncar_node_index"] = node_index
            node_obj["tooncar_root_index"] = root_index
            node_obj["tooncar_track_index"] = int(
                node.get("track_index", -1)
            )

            mesh_index = int(node.get("mesh_index", -1))
            object_name = node.get("object_name")

            if mesh_index >= 0 and object_name:
                template, obj_import_matrix = template_for(
                    object_name
                )

                if template is not None:
                    mesh_obj = template.copy()
                    mesh_obj.data = template.data
                    mesh_obj.animation_data_clear()
                    mesh_obj.name = (
                        f"mesh_i{instance_index:03d}_"
                        f"r{root_index:02d}_"
                        f"n{node_index:02d}"
                    )
                    collection.objects.link(mesh_obj)

                    mesh_obj.parent = node_obj
                    mesh_obj.matrix_parent_inverse = Matrix.Identity(4)

                    # ObjectMesh geometry is serialized in the model's bind
                    # coordinate space, not already local to this animated
                    # node. Undo the accumulated bind transform before
                    # parenting it to the animated node. This is the rigid-
                    # mesh equivalent of an inverse bind matrix in skeletal
                    # animation and fixes rotation axes/pivots.
                    # Animated ObjectMesh vertices are already serialized
                    # in the local coordinate space of their mapped animation
                    # node. This is visible directly in the R3D data: on Luna,
                    # for example, animated nodes have substantial bind
                    # translations while their child mesh bounds remain
                    # centered around the local origin.
                    #
                    # Therefore do NOT apply inverse(bind). The only transform
                    # needed on the imported template is Blender's OBJ-axis
                    # conversion, so the hierarchy is:
                    #
                    #   local ObjectMesh
                    #       -> OBJ-to-Blender axis conversion
                    #       -> animated node TRS
                    #       -> placed root instance
                    mesh_obj.matrix_local = (
                        obj_import_matrix.copy()
                    )

                    mesh_obj["tooncar_role"] = "animated_prop_mesh"
                    mesh_obj["tooncar_inverse_bind_applied"] = False
                    mesh_obj["tooncar_mesh_space"] = "node_local"
                    mesh_obj["tooncar_obj_import_transform_preserved"] = True
                    mesh_obj["tooncar_root_index"] = root_index
                    mesh_obj["tooncar_node_index"] = node_index
                    mesh_obj["tooncar_mesh_index"] = mesh_index

        animation_set = definition.get("animation_set")
        tracks = (
            animation_set.get("tracks", [])
            if animation_set
            else []
        )

        for node in definition.get("nodes", []):
            node_index = int(node["index"])
            track_index = int(node.get("track_index", -1))

            if not (0 <= track_index < len(tracks)):
                continue

            node_obj = node_objects.get(node_index)
            if node_obj is None:
                continue

            total_ticks = bake_tooncar_track_action(
                node_obj,
                tracks[track_index],
                length_scale=length_scale,
                ticks_per_key=ticks_per_key,
            )

            if (
                node_obj.animation_data
                and node_obj.animation_data.action
            ):
                node_obj.animation_data.action.name = (
                    f"ToonCar Instance {instance_index:03d} "
                    f"Root {root_index:02d} "
                    f"Node {node_index:02d} Track {track_index:02d}"
                )

            max_frame = max(
                max_frame,
                total_ticks + 1,
            )

        animated_node_count = sum(
            1
            for node in definition.get("nodes", [])
            if 0 <= int(node.get("track_index", -1)) < len(tracks)
        )

        reports.append({
            "instance_index": instance_index,
            "root_index": root_index,
            "node_count": len(node_objects),
            "animated_node_count": animated_node_count,
            "actions_are_per_instance": True,
            "animation_storage": "one_cycle_tick_sampled_fcurve_cycles",
            "inverse_bind_mesh_attachment": False,
            "animated_mesh_space": "node_local",
            "obj_import_transform_preserved": True,
        })

    bpy.context.scene.frame_start = 1
    if max_frame > 1:
        bpy.context.scene.frame_end = int(max_frame)

    # Imported objects were only local mesh templates. Duplicates keep the
    # Mesh datablocks/materials, so remove the template Objects themselves.
    for template in imported_templates:
        try:
            bpy.data.objects.remove(
                template,
                do_unlink=True,
            )
        except Exception:
            pass

    return reports



def source_point_to_blender(point, scale):
    # Same final basis as verified ToonCar mesh -> OBJ -> Blender:
    # source (X,Y,Z) -> Blender (X,Z,Y).
    return (
        float(point[0]) * scale,
        float(point[2]) * scale,
        float(point[1]) * scale,
    )



def bake_sorpresa_idle_animation(
    obj,
    source_scale,
    tick_hz=55,
    cycle_ticks=360,
):
    # Compact cyclic Sorpresa idle for helper-based placements.
    # Rotation: 2 deg/tick -> 360 degrees in 180 ticks.
    # Bob phase: 5 deg/tick -> one full bob cycle in 72 ticks.
    obj.rotation_mode = 'XYZ'
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)

    rotation_cycle_ticks = 180
    bob_cycle_ticks = 72

    obj.rotation_euler.z = 0.0
    obj.keyframe_insert(
        data_path="rotation_euler",
        index=2,
        frame=1,
        group="ToonCar Sorpresa Idle",
    )
    obj.rotation_euler.z = math.tau
    obj.keyframe_insert(
        data_path="rotation_euler",
        index=2,
        frame=1 + rotation_cycle_ticks,
        group="ToonCar Sorpresa Idle",
    )

    phase_degrees = 0.0
    source_y_offset = 0.0

    obj.location.z = 0.0
    obj.keyframe_insert(
        data_path="location",
        index=2,
        frame=1,
        group="ToonCar Sorpresa Idle",
    )

    for tick in range(1, bob_cycle_ticks + 1):
        phase_degrees += 5.0
        source_y_offset += (
            math.sin(
                math.radians(phase_degrees)
            )
            * 0.05
        )
        obj.location.z = (
            source_y_offset * source_scale
        )
        obj.keyframe_insert(
            data_path="location",
            index=2,
            frame=1 + tick,
            group="ToonCar Sorpresa Idle",
        )

    modified_curve_count = (
        make_action_linear_and_cyclic(obj)
    )

    set_fcurve_cycle_mode_for_path(
        obj,
        "rotation_euler",
        array_index=2,
        after_mode='REPEAT_OFFSET',
        before_mode='REPEAT_OFFSET',
    )

    obj["tooncar_idle_animation"] = True
    obj["tooncar_idle_cycles_curve_count"] = (
        modified_curve_count
    )
    obj["tooncar_idle_animation_mode"] = (
        "compact_fcurve_cycles"
    )
    obj["tooncar_idle_tick_hz"] = tick_hz
    obj["tooncar_idle_rotation_cycle_ticks"] = 180
    obj["tooncar_idle_rotation_cycle_mode"] = "REPEAT_OFFSET"
    obj["tooncar_idle_bob_cycle_ticks"] = 72

    return rotation_cycle_ticks + 1

def bake_sorpresa_idle_animation_direct(
    obj,
    source_scale,
    tick_hz=55,
    cycle_ticks=360,
):
    # Compact cyclic Sorpresa idle directly on a placed mesh.
    # Base matrix_world remains exact placement + OBJ import basis.
    obj.rotation_mode = 'XYZ'
    obj.delta_rotation_euler = (0.0, 0.0, 0.0)
    obj.delta_location = (0.0, 0.0, 0.0)

    rotation_cycle_ticks = 180
    bob_cycle_ticks = 72

    # Rotation needs only two keys.
    obj.delta_rotation_euler.z = 0.0
    obj.keyframe_insert(
        data_path="delta_rotation_euler",
        index=2,
        frame=1,
        group="ToonCar Sorpresa Idle",
    )
    obj.delta_rotation_euler.z = math.tau
    obj.keyframe_insert(
        data_path="delta_rotation_euler",
        index=2,
        frame=1 + rotation_cycle_ticks,
        group="ToonCar Sorpresa Idle",
    )

    # Bob stores one exact 72-tick source cycle.
    phase_degrees = 0.0
    source_y_offset = 0.0

    obj.delta_location.z = 0.0
    obj.keyframe_insert(
        data_path="delta_location",
        index=2,
        frame=1,
        group="ToonCar Sorpresa Idle",
    )

    for tick in range(1, bob_cycle_ticks + 1):
        phase_degrees += 5.0
        source_y_offset += (
            math.sin(
                math.radians(phase_degrees)
            )
            * 0.05
        )
        obj.delta_location.z = (
            source_y_offset * source_scale
        )
        obj.keyframe_insert(
            data_path="delta_location",
            index=2,
            frame=1 + tick,
            group="ToonCar Sorpresa Idle",
        )

    modified_curve_count = (
        make_action_linear_and_cyclic(obj)
    )

    # Keep the spin numerically continuous across the cycle boundary:
    # ... 358°, 360°, 362° ... instead of ... 358°, 360°, 2° ...
    set_fcurve_cycle_mode_for_path(
        obj,
        "delta_rotation_euler",
        array_index=2,
        after_mode='REPEAT_OFFSET',
        before_mode='REPEAT_OFFSET',
    )

    obj["tooncar_idle_animation"] = True
    obj["tooncar_idle_cycles_curve_count"] = (
        modified_curve_count
    )
    obj["tooncar_idle_animation_mode"] = (
        "compact_fcurve_cycles"
    )
    obj["tooncar_idle_tick_hz"] = tick_hz
    obj["tooncar_idle_rotation_cycle_ticks"] = (
        rotation_cycle_ticks
    )
    obj["tooncar_idle_rotation_cycle_mode"] = (
        "REPEAT_OFFSET"
    )
    obj["tooncar_idle_bob_cycle_ticks"] = (
        bob_cycle_ticks
    )
    obj["tooncar_idle_rotation_key_count"] = 2
    obj["tooncar_idle_bob_key_count"] = (
        bob_cycle_ticks + 1
    )

    return max(
        rotation_cycle_ticks,
        bob_cycle_ticks,
    ) + 1

def build_gameplay_debug(
    manifest,
    scene,
    root,
    include_ai_path=False,
    include_item_boxes=True,
    use_sorpresa_asset=True,
    animate_sorpresa=True,
    sorpresa_size_multiplier=0.65,
    include_cones=True,
    show_cone_paths=True,
    use_cone_models=True,
):
    gameplay = manifest.get("gameplay_data")
    if not gameplay:
        return None

    if (
        not include_ai_path
        and not include_item_boxes
        and not include_cones
    ):
        return None

    scale = float(manifest.get("export_scale", 0.1))

    gameplay_collection = bpy.data.collections.new("Gameplay")
    scene.collection.children.link(gameplay_collection)

    # AI path
    way = gameplay.get("way") or {}
    way_points = way.get("points", [])

    if include_ai_path:
        ai_collection = bpy.data.collections.new("AI Path")
        gameplay_collection.children.link(ai_collection)

    if include_ai_path and len(way_points) >= 2:
        curve_data = bpy.data.curves.new(
            "ToonCar AI Way",
            type='CURVE',
        )
        curve_data.dimensions = '3D'
        curve_data.resolution_u = 1
        curve_data.bevel_depth = 0.04
        curve_data.bevel_resolution = 0

        spline = curve_data.splines.new('POLY')
        spline.points.add(len(way_points) - 1)

        for index, point in enumerate(way_points):
            x, y, z = source_point_to_blender(point, scale)
            spline.points[index].co = (x, y, z, 1.0)

        spline.use_cyclic_u = bool(way.get("closed_loop", True))

        curve_obj = bpy.data.objects.new(
            "AI Way Path",
            curve_data,
        )
        ai_collection.objects.link(curve_obj)
        curve_obj["tooncar_role"] = "ai_way_path"
        curve_obj["tooncar_closed_loop"] = True
        curve_obj["tooncar_waypoint_count"] = len(way_points)

        for index, point in enumerate(way_points):
            empty = bpy.data.objects.new(
                f"Way {index:03d}",
                None,
            )
            ai_collection.objects.link(empty)
            empty.empty_display_type = 'SPHERE'
            empty.empty_display_size = 0.22
            empty.location = source_point_to_blender(point, scale)
            empty["tooncar_role"] = "ai_waypoint"
            empty["tooncar_way_index"] = index
            empty["tooncar_next_index"] = (
                (index + 1) % len(way_points)
            )

    # License cones: static Cono points + moving ConoPata paths.
    conos = gameplay.get("conos") or {}
    static_cone_points = conos.get("points") or []
    moving_cone_paths = (
        gameplay.get("moving_cone_paths")
        or gameplay.get("camera_groups")
        or []
    )

    cone_assets = (
        (manifest.get("gameplay_assets") or {}).get("cones")
        or {}
    )

    used_static_cone_model = False
    used_moving_cone_model = False

    if include_cones and (static_cone_points or moving_cone_paths):
        cones_collection = bpy.data.collections.new("Cones")
        gameplay_collection.children.link(cones_collection)

        static_collection = bpy.data.collections.new("Static Cones")
        moving_collection = bpy.data.collections.new("Moving Cones")
        paths_collection = None

        cones_collection.children.link(static_collection)
        cones_collection.children.link(moving_collection)

        if show_cone_paths:
            paths_collection = bpy.data.collections.new(
                "Moving Cone Paths"
            )
            cones_collection.children.link(
                paths_collection
            )

        static_imported = []
        moving_imported = []
        static_templates = []
        moving_templates = []

        if use_cone_models:
            static_asset = cone_assets.get("static") or {}
            static_obj_rel = static_asset.get("obj")
            if static_obj_rel:
                static_obj_path = Path(root) / Path(static_obj_rel)
                if static_obj_path.is_file():
                    static_imported = import_obj(static_obj_path)
                    static_templates = [
                        obj for obj in static_imported
                        if obj.type == 'MESH' and obj.data is not None
                    ]
                    used_static_cone_model = bool(static_templates)

            moving_asset = cone_assets.get("moving") or {}
            moving_obj_rel = moving_asset.get("obj")
            if moving_obj_rel:
                moving_obj_path = Path(root) / Path(moving_obj_rel)
                if moving_obj_path.is_file():
                    moving_imported = import_obj(moving_obj_path)
                    moving_meshes = [
                        obj for obj in moving_imported
                        if obj.type == 'MESH' and obj.data is not None
                    ]
                    preferred_name = str(
                        moving_asset.get("preferred_object_name") or ""
                    ).upper()
                    preferred = [
                        obj for obj in moving_meshes
                        if preferred_name and preferred_name in obj.name.upper()
                    ]
                    if preferred:
                        moving_templates = [preferred[0]]
                    elif moving_meshes:
                        moving_templates = [
                            max(
                                moving_meshes,
                                key=lambda obj: len(obj.data.polygons),
                            )
                        ]
                    used_moving_cone_model = bool(moving_templates)

        def place_cone_model(
            templates,
            collection,
            name_prefix,
            point,
            role,
            source_index,
        ):
            translation = Matrix.Translation(
                Vector(source_point_to_blender(point, scale))
            )
            for part_index, template in enumerate(templates):
                instance = template.copy()
                instance.data = template.data
                instance.animation_data_clear()
                instance.name = (
                    name_prefix
                    if len(templates) == 1
                    else f"{name_prefix} Part {part_index:02d}"
                )
                collection.objects.link(instance)
                instance.parent = None
                instance.matrix_world = translation @ template.matrix_world
                instance.hide_viewport = False
                instance.hide_render = False
                instance.hide_set(False)
                instance["tooncar_role"] = role
                instance["tooncar_cone_index"] = int(source_index)

        for index, point in enumerate(static_cone_points):
            if static_templates:
                place_cone_model(
                    static_templates,
                    static_collection,
                    f"Cono {index:03d}",
                    point,
                    "static_cone_model",
                    index,
                )
            else:
                empty = bpy.data.objects.new(
                    f"Cono Placeholder {index:03d}",
                    None,
                )
                static_collection.objects.link(empty)
                empty.empty_display_type = 'CONE'
                empty.empty_display_size = 0.45
                empty.rotation_euler[0] = math.radians(90.0)
                empty.location = source_point_to_blender(point, scale)
                empty["tooncar_role"] = "static_cone_position"
                empty["tooncar_cone_index"] = index

        for path_index, path_info in enumerate(moving_cone_paths):
            path_points = path_info.get("points") or []

            if (
                show_cone_paths
                and paths_collection is not None
                and len(path_points) >= 2
            ):
                curve_data = bpy.data.curves.new(
                    f"ConoPata Path {path_index:02d}",
                    type='CURVE',
                )
                curve_data.dimensions = '3D'
                curve_data.resolution_u = 1
                curve_data.bevel_depth = 0.035
                curve_data.bevel_resolution = 0

                spline = curve_data.splines.new('POLY')
                spline.points.add(len(path_points) - 1)
                for point_index, point in enumerate(path_points):
                    x, y, z = source_point_to_blender(point, scale)
                    spline.points[point_index].co = (x, y, z, 1.0)

                # No explicit closed-loop bit is serialized for ConoPata.
                spline.use_cyclic_u = False

                curve_obj = bpy.data.objects.new(
                    f"ConoPata Path {path_index:02d}",
                    curve_data,
                )
                paths_collection.objects.link(curve_obj)
                curve_obj["tooncar_role"] = "moving_cone_path"
                curve_obj["tooncar_cone_index"] = path_index
                curve_obj["tooncar_waypoint_count"] = len(path_points)
                curve_obj["tooncar_closed_loop"] = False

                for point_index, point in enumerate(path_points):
                    waypoint = bpy.data.objects.new(
                        f"ConoPata {path_index:02d} Point {point_index:02d}",
                        None,
                    )
                    paths_collection.objects.link(waypoint)
                    waypoint.empty_display_type = 'SPHERE'
                    waypoint.empty_display_size = 0.16
                    waypoint.location = source_point_to_blender(point, scale)
                    waypoint["tooncar_role"] = "moving_cone_waypoint"
                    waypoint["tooncar_cone_index"] = path_index
                    waypoint["tooncar_waypoint_index"] = point_index

            if path_points:
                start_point = path_points[0]
                if moving_templates:
                    place_cone_model(
                        moving_templates,
                        moving_collection,
                        f"ConoPata {path_index:02d}",
                        start_point,
                        "moving_cone_model",
                        path_index,
                    )
                else:
                    empty = bpy.data.objects.new(
                        f"ConoPata Placeholder {path_index:02d}",
                        None,
                    )
                    moving_collection.objects.link(empty)
                    empty.empty_display_type = 'CONE'
                    empty.empty_display_size = 0.55
                    empty.rotation_euler[0] = math.radians(90.0)
                    empty.location = source_point_to_blender(start_point, scale)
                    empty["tooncar_role"] = "moving_cone_position"
                    empty["tooncar_cone_index"] = path_index

        for template in static_imported + moving_imported:
            try:
                bpy.data.objects.remove(template, do_unlink=True)
            except Exception:
                pass

    # Item-box spawns
    sorpresa = gameplay.get("sorpresa") or {}
    item_points = sorpresa.get("points", [])

    sorpresa_asset = (
        (manifest.get("gameplay_assets") or {}).get("sorpresa")
    )
    used_sorpresa_model = False

    if include_item_boxes:
        # Treat pickups as ordinary additional map props.
        # Reuse the same visible hierarchy as reconstructed scene props.
        placed_props_collection = bpy.data.collections.get(
            "Placed Props"
        )

        if placed_props_collection is None:
            placed_props_collection = bpy.data.collections.new(
                "Placed Props"
            )
            scene.collection.children.link(
                placed_props_collection
            )

        item_collection = bpy.data.collections.get(
            "Item Boxes"
        )

        if item_collection is None:
            item_collection = bpy.data.collections.new(
                "Item Boxes"
            )
            placed_props_collection.children.link(
                item_collection
            )
        else:
            # Make sure it is not only linked under Gameplay from an old path.
            if item_collection.name not in {
                c.name for c in placed_props_collection.children
            }:
                placed_props_collection.children.link(
                    item_collection
                )

        item_collection.hide_viewport = False
        item_collection.hide_render = False

        template_objects = []
        template_import_matrices = []

        if (
            use_sorpresa_asset
            and sorpresa_asset
            and sorpresa_asset.get("obj")
        ):
            asset_obj_path = (
                Path(root)
                / Path(sorpresa_asset["obj"])
            )

            if asset_obj_path.is_file():
                template_objects = import_obj(
                    asset_obj_path
                )

                for template in template_objects:
                    template_import_matrices.append(
                        template.matrix_world.copy()
                    )

                if template_objects:
                    used_sorpresa_model = True

        if used_sorpresa_model:
            render_info = (
                sorpresa_asset.get("gameplay_render")
                or {}
            )

            game_tick_hz = int(
                render_info.get(
                    "game_tick_hz",
                    55,
                )
            )
            cycle_ticks = int(
                render_info.get(
                    "idle_cycle_ticks",
                    360,
                )
            )

            scene.render.fps = game_tick_hz
            scene.render.fps_base = 1.0

            max_sorpresa_frame = 1
            created_models = 0

            for index, point in enumerate(
                item_points
            ):
                px, py, pz = source_point_to_blender(
                    point,
                    scale,
                )

                translation = Matrix.Translation(
                    Vector((px, py, pz))
                )

                for (
                    template_index,
                    template,
                ) in enumerate(template_objects):
                    instance = template.copy()
                    instance.data = template.data
                    instance.animation_data_clear()

                    instance.name = (
                        f"Sorpresa {index:03d}"
                        if len(template_objects) == 1
                        else (
                            f"Sorpresa {index:03d} "
                            f"Part {template_index:02d}"
                        )
                    )

                    item_collection.objects.link(
                        instance
                    )

                    # EXACT original v49 placement.
                    instance.parent = None
                    instance.matrix_world = (
                        translation
                        @ template_import_matrices[
                            template_index
                        ]
                    )

                    # Optional user size is deliberately applied AFTER the
                    # proven v49 placement matrix.
                    instance.scale = (
                        instance.scale
                        * float(sorpresa_size_multiplier)
                    )

                    instance.hide_viewport = False
                    instance.hide_render = False
                    instance.hide_set(False)

                    instance["tooncar_role"] = (
                        "intact_prop"
                    )
                    instance[
                        "tooncar_prop_subtype"
                    ] = "item_box"
                    instance[
                        "tooncar_sorpresa_index"
                    ] = index
                    instance[
                        "tooncar_linked_asset"
                    ] = "Sorpresa.r3d"
                    instance[
                        "tooncar_size_multiplier"
                    ] = float(
                        sorpresa_size_multiplier
                    )
                    instance[
                        "tooncar_visible_by_default"
                    ] = True

                    created_models += 1

                    if animate_sorpresa:
                        end_frame = (
                            bake_sorpresa_idle_animation_direct(
                                instance,
                                source_scale=scale,
                                tick_hz=game_tick_hz,
                                cycle_ticks=cycle_ticks,
                            )
                        )
                        max_sorpresa_frame = max(
                            max_sorpresa_frame,
                            end_frame,
                        )

            if animate_sorpresa:
                scene.frame_start = 1
                scene.frame_end = max(
                    2,
                    int(max_sorpresa_frame),
                )

            # Imported templates are only sources for linked mesh/material data.
            for template in template_objects:
                try:
                    bpy.data.objects.remove(
                        template,
                        do_unlink=True,
                    )
                except Exception:
                    pass

            scene[
                "tooncar_sorpresa_created_mesh_count"
            ] = created_models

        else:
            # Visible prop fallback if Sorpresa.r3d cannot be loaded.
            for index, point in enumerate(
                item_points
            ):
                empty = bpy.data.objects.new(
                    f"Sorpresa {index:03d}",
                    None,
                )
                item_collection.objects.link(
                    empty
                )
                empty.empty_display_type = 'CUBE'
                empty.empty_display_size = 0.15
                empty.location = source_point_to_blender(
                    point,
                    scale,
                )
                empty.hide_viewport = False
                empty.hide_render = False
                empty.hide_set(False)
                empty["tooncar_role"] = (
                    "intact_prop"
                )
                empty[
                    "tooncar_prop_subtype"
                ] = "item_box"
                empty[
                    "tooncar_sorpresa_index"
                ] = index

    scene["tooncar_ai_waypoint_count"] = (
        len(way_points) if include_ai_path else 0
    )
    scene["tooncar_item_box_spawn_count"] = (
        len(item_points) if include_item_boxes else 0
    )
    scene["tooncar_static_cone_count"] = (
        len(static_cone_points) if include_cones else 0
    )
    scene["tooncar_moving_cone_count"] = (
        len(moving_cone_paths) if include_cones else 0
    )
    scene["tooncar_config_include_ai_path"] = include_ai_path
    scene["tooncar_config_include_item_boxes"] = include_item_boxes
    scene["tooncar_config_include_cones"] = include_cones
    scene["tooncar_config_show_cone_paths"] = show_cone_paths
    scene["tooncar_config_use_cone_models"] = use_cone_models
    scene["tooncar_config_use_sorpresa_asset"] = use_sorpresa_asset
    scene["tooncar_config_animate_sorpresa"] = animate_sorpresa
    scene["tooncar_sorpresa_size_multiplier"] = sorpresa_size_multiplier
    scene["tooncar_sorpresa_asset_used"] = used_sorpresa_model
    scene["tooncar_sorpresa_asset_available"] = bool(
        sorpresa_asset and sorpresa_asset.get("obj")
    )

    return {
        "waypoint_count": len(way_points) if include_ai_path else 0,
        "item_box_spawn_count": len(item_points) if include_item_boxes else 0,
        "static_cone_count": len(static_cone_points) if include_cones else 0,
        "moving_cone_count": len(moving_cone_paths) if include_cones else 0,
        "way_closed_loop": bool(way.get("closed_loop", True)),
        "include_ai_path": include_ai_path,
        "include_item_boxes": include_item_boxes,
        "include_cones": include_cones,
        "show_cone_paths": show_cone_paths,
        "use_cone_models": use_cone_models,
        "static_cone_model_used": used_static_cone_model,
        "moving_cone_model_used": used_moving_cone_model,
        "use_sorpresa_asset": use_sorpresa_asset,
        "animate_sorpresa": animate_sorpresa,
        "sorpresa_size_multiplier": sorpresa_size_multiplier,
        "sorpresa_asset_used": used_sorpresa_model,
        "sorpresa_game_model_scale": (
            float(
                ((sorpresa_asset or {}).get("gameplay_render") or {})
                .get("idle_instance_scale", 0.75)
            )
            if used_sorpresa_model
            else None
        ),
    }


def build_alpha_lookup(manifest):
    lookup = {}

    for bank in manifest.get("texture_banks", []):
        for tex in bank.get("entries", []):
            png = tex.get("png")
            if not png:
                continue

            alpha = tex.get("alpha") or {}
            lookup[png.lower()] = {
                "has_alpha": bool(tex.get("has_alpha")),
                "mode": alpha.get(
                    "mode",
                    "blend" if tex.get("has_alpha") else "opaque",
                ),
                "source_name": tex.get("name"),
                "stats": alpha,
            }

    return lookup


def image_alpha_info(image, alpha_lookup):
    if image is None:
        return None

    candidates = set()

    try:
        candidates.add(Path(bpy.path.abspath(image.filepath)).name.lower())
    except Exception:
        pass

    try:
        candidates.add(Path(image.filepath_raw).name.lower())
    except Exception:
        pass

    try:
        candidates.add(Path(image.name).name.lower())
    except Exception:
        pass

    for candidate in candidates:
        if candidate in alpha_lookup:
            return alpha_lookup[candidate]

    return None


def set_material_render_mode(material, alpha_mode):
    # Blender 4.2+/5.x uses surface_render_method.
    # Blender <=4.1 uses blend_method. Support both so the generated helper
    # remains useful across installations.
    if alpha_mode == "opaque":
        return

    # New Blender API.
    if hasattr(material, "surface_render_method"):
        try:
            if alpha_mode == "blend":
                material.surface_render_method = 'BLENDED'
            else:
                material.surface_render_method = 'DITHERED'
        except Exception:
            # DITHERED is a safe grayscale-transparency fallback.
            try:
                material.surface_render_method = 'DITHERED'
            except Exception:
                pass

    # Older Blender API.
    if hasattr(material, "blend_method"):
        try:
            material.blend_method = (
                'HASHED' if alpha_mode == "blend" else 'CLIP'
            )
        except Exception:
            try:
                material.blend_method = 'BLEND'
            except Exception:
                pass

    if hasattr(material, "alpha_threshold"):
        try:
            material.alpha_threshold = 0.5
        except Exception:
            pass

    if hasattr(material, "show_transparent_back"):
        try:
            material.show_transparent_back = True
        except Exception:
            pass


def configure_material_alpha(manifest):
    alpha_lookup = build_alpha_lookup(manifest)
    configured = []

    for material in bpy.data.materials:
        if not material.use_nodes or not material.node_tree:
            continue

        nodes = material.node_tree.nodes
        links = material.node_tree.links

        principled = next(
            (
                node
                for node in nodes
                if node.type == 'BSDF_PRINCIPLED'
            ),
            None,
        )
        if principled is None:
            continue

        alpha_input = principled.inputs.get("Alpha")
        if alpha_input is None:
            continue

        image_nodes = [
            node
            for node in nodes
            if node.type == 'TEX_IMAGE' and node.image is not None
        ]

        selected = None
        selected_info = None

        # Prefer an image explicitly marked as carrying alpha in the R3D.
        for node in image_nodes:
            info = image_alpha_info(node.image, alpha_lookup)
            if info and info.get("has_alpha"):
                selected = node
                selected_info = info
                break

        if selected is None or selected_info is None:
            continue

        alpha_output = selected.outputs.get("Alpha")
        if alpha_output is None:
            continue

        # Replace any stale alpha link created by another importer.
        for link in list(alpha_input.links):
            links.remove(link)

        links.new(alpha_output, alpha_input)

        alpha_mode = selected_info.get("mode", "blend")
        set_material_render_mode(material, alpha_mode)

        material["tooncar_alpha_mode"] = alpha_mode
        material["tooncar_alpha_texture"] = (
            selected_info.get("source_name") or selected.image.name
        )

        configured.append({
            "material": material.name,
            "texture": selected_info.get("source_name"),
            "mode": alpha_mode,
        })

    return configured




def tooncar_name_hash_blender(name):
    raw = name.encode("latin1", errors="ignore")
    if not raw:
        return 0

    upper = bytes(
        (b - 32 if 97 <= b <= 122 else b)
        for b in raw
    )

    total = 0
    for i, value in enumerate(upper):
        total = (
            total
            + value * upper[(i + 1) % len(upper)]
        ) & 0xFFFFFFFF

    return total


def material_index_from_name(name):
    match = re.match(r"^mat_(\d+)(?:\.\d+)?$", name)
    if not match:
        return None
    return int(match.group(1))


def configure_texture_animations(
    manifest,
    root,
    enabled=True,
    use_r3d_timing=True,
    frame_interval=0.20,
):
    # Exact ToonCar texture-animation timing recovered from ToonCar.exe:
    #
    #   0x4027F0:
    #       if accumulator <= 0:
    #           display frame[counter % frame_count]
    #           counter += 1
    #           accumulator += 1.0
    #       accumulator -= record[+0x1A4]
    #
    # The game simulation clock is 55 Hz:
    #   0x494520 = 0.018181819... = 1 / 55 s
    #   0x4944A4 = 55.0
    #
    # Therefore +0x1A4 is NOT seconds/frame. It is phase step per
    # 55 Hz game tick. For example:
    #   0.1        -> approximately 10 ticks/frame -> 10/55 s
    #   0.0833333  -> approximately 12 ticks/frame -> 12/55 s
    #
    # With original timing enabled, Blender uses 55 FPS and we simulate the
    # exact accumulator algorithm to generate the persistent image sequence.
    if not enabled:
        return []

    animations = manifest.get("texture_animations", [])
    if not animations:
        return []

    texture_banks = manifest.get("texture_banks", [])
    if not texture_banks:
        return []

    primary = next(
        (bank for bank in texture_banks if bank.get("offset") == 0),
        texture_banks[0],
    )

    hash_to_entry = {
        tooncar_name_hash_blender(entry["name"]): entry
        for entry in primary.get("entries", [])
    }

    scene = bpy.context.scene

    if use_r3d_timing:
        scene.render.fps = 55
        scene.render.fps_base = 1.0
    else:
        scene.render.fps = 60
        scene.render.fps_base = 1.0

    materials_by_index = {}
    for material in bpy.data.materials:
        index = material_index_from_name(material.name)
        if index is not None:
            materials_by_index.setdefault(index, []).append(material)

    sequence_root = root / "animated_textures"
    sequence_root.mkdir(parents=True, exist_ok=True)

    reports = []

    for anim in animations:
        material_index = anim.get("material_index")
        frame_hashes = anim.get("frame_resource_ids", [])

        # v102 manifests call this what it actually is. Also accept old manifests
        # so an already-unpacked folder can still be rebuilt.
        frame_step = anim.get("frame_step_per_tick")
        if frame_step is None:
            frame_step = anim.get("frame_time_seconds")
        frame_step = float(frame_step or 0.0)

        if material_index is None or not frame_hashes or frame_step <= 0.0:
            continue

        frame_entries = []
        for frame_hash in frame_hashes:
            entry = hash_to_entry.get(int(frame_hash))
            if entry is None:
                frame_entries = []
                break
            frame_entries.append(entry)

        if not frame_entries:
            continue

        anim_dir = sequence_root / (
            f"anim_{int(anim['index']):02d}_"
            f"mat_{int(material_index):03d}"
        )

        if anim_dir.exists():
            shutil.rmtree(anim_dir)
        anim_dir.mkdir(parents=True, exist_ok=True)

        sequence_frame_entries = []

        if use_r3d_timing:
            # Simulate one complete animation cycle with the exact 0x4027F0
            # accumulator behavior. Start state is zero, so source frame 0 is
            # selected on the first game tick.
            accumulator = 0.0
            counter = 0
            current_source_index = 0

            # A full cycle ends when every source frame has been selected once
            # and the next frame would wrap back to source frame 0.
            # Safety cap covers malformed data.
            max_ticks = max(
                1000,
                int(len(frame_entries) / max(frame_step, 1e-6) * 4) + 100,
            )

            selected_frames = 0

            for _tick in range(max_ticks):
                if accumulator <= 0.0:
                    source_index = counter % len(frame_entries)

                    if selected_frames >= len(frame_entries):
                        # We reached the next wrapped frame 0: the previous ticks
                        # already form exactly one complete source animation cycle.
                        break

                    current_source_index = source_index
                    counter += 1
                    selected_frames += 1
                    accumulator += 1.0

                sequence_frame_entries.append(
                    frame_entries[current_source_index]
                )

                # Match the executable's final operation in 0x4027F0.
                accumulator -= frame_step

            if not sequence_frame_entries:
                continue

            effective_interval = None
            timing_mode = "tooncar_55hz_accumulator"
        else:
            hold_frames = max(
                1,
                int(round(frame_interval * scene.render.fps)),
            )

            for entry in frame_entries:
                sequence_frame_entries.extend(
                    [entry] * hold_frames
                )

            effective_interval = frame_interval
            timing_mode = "manual_interval"

        for sequence_count, entry in enumerate(
            sequence_frame_entries,
            start=1,
        ):
            source_png = (
                root
                / "textures"
                / primary["directory"]
                / entry["png"]
            )
            target = anim_dir / f"frame_{sequence_count:04d}.png"
            shutil.copy2(source_png, target)

        sequence_count = len(sequence_frame_entries)
        first_path = anim_dir / "frame_0001.png"

        sequence_image = bpy.data.images.load(
            str(first_path),
            check_existing=False,
        )
        sequence_image.name = (
            f"ToonCar Anim {int(anim['index']):02d} "
            f"Mat {int(material_index):03d}"
        )

        try:
            sequence_image.source = 'SEQUENCE'
        except Exception:
            pass

        applied_materials = []

        for material in materials_by_index.get(int(material_index), []):
            if not material.use_nodes or not material.node_tree:
                continue

            image_nodes = [
                node
                for node in material.node_tree.nodes
                if node.type == 'TEX_IMAGE'
            ]

            if not image_nodes:
                continue

            for node in image_nodes:
                node.image = sequence_image
                try:
                    node.image_user.frame_start = 1
                    node.image_user.frame_duration = sequence_count
                    node.image_user.frame_offset = 0
                    node.image_user.use_cyclic = True
                    node.image_user.use_auto_refresh = True
                except Exception:
                    pass

            material["tooncar_texture_animation"] = int(anim["index"])
            material["tooncar_texture_animation_timing_mode"] = timing_mode
            material["tooncar_texture_animation_frame_step_per_tick"] = frame_step
            material["tooncar_texture_animation_game_tick_hz"] = 55

            if effective_interval is not None:
                material["tooncar_texture_animation_override_interval"] = (
                    effective_interval
                )

            applied_materials.append(material.name)

        if applied_materials:
            report = {
                "animation_index": int(anim["index"]),
                "material_index": int(material_index),
                "source_frame_count": len(frame_entries),
                "frame_step_per_tick": frame_step,
                "timing_mode": timing_mode,
                "timeline_fps": int(scene.render.fps),
                "sequence_frame_count": sequence_count,
                "materials": applied_materials,
                "sequence_directory": str(anim_dir),
            }

            if use_r3d_timing:
                report["game_tick_hz"] = 55
                report["cycle_seconds"] = sequence_count / 55.0
                report["average_source_frame_interval_seconds"] = (
                    sequence_count / 55.0 / len(frame_entries)
                )
            else:
                report["override_frame_interval_seconds"] = frame_interval

            reports.append(report)

    if reports:
        longest = max(item["sequence_frame_count"] for item in reports)
        scene.frame_start = 1
        scene.frame_end = max(
            int(scene.render.fps * 4),
            longest * 12,
        )

    return reports



def configure_gltf_friendly_materials(manifest):
    # glTF-friendly graph:
    # Image Color -> Principled Base Color
    # Image Alpha -> Principled Alpha (if transparent)
    # Principled   -> Material Output
    #
    # This deliberately avoids Blender-only Mix Shader / Transparent BSDF
    # constructions so the glTF exporter can recognize the material directly.
    converted = []

    animated_material_indices = {
        int(anim.get("material_index"))
        for anim in manifest.get("texture_animations", [])
        if anim.get("material_index") is not None
    }

    for material in bpy.data.materials:
        if not material.use_nodes or not material.node_tree:
            continue

        nodes = material.node_tree.nodes
        links = material.node_tree.links

        image_nodes = [
            node
            for node in nodes
            if node.type == 'TEX_IMAGE'
            and node.image is not None
        ]
        if not image_nodes:
            continue

        selected = image_nodes[0]
        alpha_texture_name = material.get(
            "tooncar_alpha_texture"
        )

        if alpha_texture_name:
            target = Path(
                str(alpha_texture_name)
            ).name.lower()

            for node in image_nodes:
                try:
                    candidate = Path(
                        node.image.filepath
                        or node.image.name
                    ).name.lower()
                except Exception:
                    candidate = ""

                if candidate == target:
                    selected = node
                    break

        image = selected.image
        alpha_mode = str(
            material.get(
                "tooncar_alpha_mode",
                "opaque",
            )
        ).lower()

        try:
            image.colorspace_settings.name = 'sRGB'
        except Exception:
            pass

        nodes.clear()

        output = nodes.new(
            "ShaderNodeOutputMaterial"
        )
        output.location = (520, 0)

        bsdf = nodes.new(
            "ShaderNodeBsdfPrincipled"
        )
        bsdf.location = (220, 0)

        texture = nodes.new(
            "ShaderNodeTexImage"
        )
        texture.location = (-120, 0)
        texture.image = image
        texture.interpolation = 'Linear'
        texture.extension = 'REPEAT'

        links.new(
            texture.outputs["Color"],
            bsdf.inputs["Base Color"],
        )

        metallic = bsdf.inputs.get("Metallic")
        if metallic is not None:
            metallic.default_value = 0.0

        roughness = bsdf.inputs.get("Roughness")
        if roughness is not None:
            roughness.default_value = 1.0

        for socket_name in (
            "Coat Weight",
            "Coat",
            "Transmission Weight",
            "Transmission",
        ):
            socket = bsdf.inputs.get(socket_name)
            if socket is not None:
                socket.default_value = 0.0

        alpha_input = bsdf.inputs.get("Alpha")
        if (
            alpha_mode != "opaque"
            and alpha_input is not None
        ):
            links.new(
                texture.outputs["Alpha"],
                alpha_input,
            )
            set_material_render_mode(
                material,
                alpha_mode,
            )
        elif alpha_input is not None:
            alpha_input.default_value = 1.0

        links.new(
            bsdf.outputs["BSDF"],
            output.inputs["Surface"],
        )

        try:
            material.use_backface_culling = False
        except Exception:
            pass

        material_index = material_index_from_name(
            material.name
        )

        material["tooncar_gltf_ready"] = True
        material["tooncar_gltf_shader"] = "Principled BSDF"
        material["tooncar_gltf_alpha_mode"] = alpha_mode
        material["tooncar_gltf_metallic"] = 0.0
        material["tooncar_gltf_roughness"] = 1.0
        material["tooncar_gltf_double_sided"] = True

        if (
            material_index is not None
            and material_index in animated_material_indices
        ):
            material[
                "tooncar_gltf_texture_animation"
            ] = "runtime_threejs"

            matching_anim = next(
                (
                    anim
                    for anim in manifest.get(
                        "texture_animations",
                        [],
                    )
                    if int(
                        anim.get(
                            "material_index",
                            -1,
                        )
                    ) == material_index
                ),
                None,
            )

            if matching_anim is not None:
                base_name = (
                    f"anim_{int(matching_anim['index']):02d}"
                    f"_mat_{int(material_index):03d}"
                )
                material[
                    "tooncar_gltf_texture_atlas"
                ] = (
                    "gltf/texture_animations/"
                    + base_name
                    + ".png"
                )
                material[
                    "tooncar_gltf_texture_animation_json"
                ] = (
                    "gltf/texture_animations/"
                    + base_name
                    + ".json"
                )

        converted.append({
            "material": material.name,
            "image": image.name,
            "alpha_mode": alpha_mode,
            "metallic": 0.0,
            "roughness": 1.0,
            "animated_texture_runtime": (
                material_index in animated_material_indices
                if material_index is not None
                else False
            ),
        })

    return converted


def add_gltf_export_metadata(
    scene,
    manifest,
    material_report,
):
    scene["tooncar_gltf_ready"] = True
    scene["tooncar_gltf_target_runtime"] = "Three.js GLTFLoader"
    scene["tooncar_gltf_transform_animation"] = (
        "One natural Action cycle; loop clip in Three.js"
    )
    scene["tooncar_gltf_texture_animation"] = (
        "Runtime Three.js; atlas metadata in gltf/texture_animations.json"
    )
    scene["tooncar_gltf_runtime_json"] = (
        "gltf/runtime.json"
    )
    scene["tooncar_gltf_texture_animation_index"] = (
        "gltf/texture_animations.json"
    )
    scene["tooncar_gltf_skybox_json"] = (
        "gltf/skybox/skybox.json"
        if manifest.get("skybox")
        else ""
    )

    payload = {
        "target": "Three.js GLTFLoader",
        "materials": material_report,
        "transform_animations": (
            "One natural Action cycle is stored/exported; "
            "loop clips at runtime in Three.js"
        ),
        "texture_animations": (
            "Blender Image Sequence playback is disabled in this preset. "
            "Use gltf/texture_animations.json and generated atlases in Three.js."
        ),
        "runtime_json": "gltf/runtime.json",
        "texture_animation_index": "gltf/texture_animations.json",
        "skybox_json": (
            "gltf/skybox/skybox.json"
            if manifest.get("skybox")
            else None
        ),
        "recommended_export": {
            "format": "GLB",
            "materials": True,
            "animations": True,
            "custom_properties": True,
            "apply_modifiers": True,
        },
        "source_texture_animations": (
            manifest.get("texture_animations", [])
        ),
    }

    txt = bpy.data.texts.get(
        "ToonCar - glTF Three.js"
    )
    if txt is None:
        txt = bpy.data.texts.new(
            "ToonCar - glTF Three.js"
        )
    txt.clear()
    txt.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
    )
    return payload


def convert_materials_to_unlit():
    # Convert imported ToonCar materials to true unlit/emissive shaders.
    #
    # Opaque:
    #   Image Color -> Emission -> Material Output
    #
    # Alpha:
    #   Transparent BSDF ---\
    #                       Mix Shader -> Material Output
    #   Emission -----------/
    #   Image Alpha -> Mix factor
    #
    # This preserves the R3D alpha work from earlier versions while making
    # texture brightness completely independent of viewport or scene lighting.
    converted = []

    for material in bpy.data.materials:
        if not material.use_nodes or not material.node_tree:
            continue

        nodes = material.node_tree.nodes
        links = material.node_tree.links

        image_nodes = [
            node
            for node in nodes
            if node.type == 'TEX_IMAGE' and node.image is not None
        ]
        if not image_nodes:
            continue

        # Prefer the texture carrying ToonCar alpha when known.
        selected = None
        alpha_texture_name = material.get("tooncar_alpha_texture")

        if alpha_texture_name:
            for node in image_nodes:
                try:
                    if (
                        Path(node.image.name).name.lower()
                        == Path(alpha_texture_name).name.lower()
                    ):
                        selected = node
                        break
                except Exception:
                    pass

        if selected is None:
            selected = image_nodes[0]

        image = selected.image
        alpha_mode = material.get("tooncar_alpha_mode", "opaque")

        # Keep the image datablock, rebuild the shader graph from scratch.
        nodes.clear()

        output = nodes.new("ShaderNodeOutputMaterial")
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = image
        texture.interpolation = 'Linear'

        emission = nodes.new("ShaderNodeEmission")
        emission.inputs["Strength"].default_value = 1.0
        links.new(texture.outputs["Color"], emission.inputs["Color"])

        if alpha_mode != "opaque":
            transparent = nodes.new("ShaderNodeBsdfTransparent")
            mix = nodes.new("ShaderNodeMixShader")

            # Alpha 0 -> transparent, Alpha 1 -> emission.
            links.new(texture.outputs["Alpha"], mix.inputs[0])
            links.new(transparent.outputs["BSDF"], mix.inputs[1])
            links.new(emission.outputs["Emission"], mix.inputs[2])
            links.new(mix.outputs["Shader"], output.inputs["Surface"])

            set_material_render_mode(material, alpha_mode)
        else:
            links.new(emission.outputs["Emission"], output.inputs["Surface"])

        material["tooncar_unlit"] = True

        converted.append({
            "material": material.name,
            "image": image.name,
            "alpha_mode": alpha_mode,
        })

    return converted


def set_material_preview_for_saved_file(
    use_environment=True,
    hide_grid_axes=True,
    use_scene_lights=False,
):
    # Material Preview can mimic the game (no scene lights) or use the
    # generated Sun for the realistic preset.
    changed = 0

    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue

            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue

                try:
                    space.shading.type = 'MATERIAL'
                    space.shading.use_scene_world = bool(use_environment)
                    space.shading.use_scene_lights = bool(use_scene_lights)
                    changed += 1
                except Exception:
                    pass

                try:
                    space.clip_start = 0.01
                    space.clip_end = 50000.0
                except Exception:
                    pass

                if hide_grid_axes:
                    try:
                        overlay = space.overlay
                        overlay.show_floor = False
                        overlay.show_axis_x = False
                        overlay.show_axis_y = False
                        overlay.show_axis_z = False
                    except Exception:
                        pass

    return changed


def make_sky_material(name, image_path):
    material = bpy.data.materials.new(name=name)
    material.use_nodes = True

    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    texture = nodes.new("ShaderNodeTexImage")

    texture.image = bpy.data.images.load(
        str(image_path),
        check_existing=True,
    )

    links.new(texture.outputs["Color"], emission.inputs["Color"])
    links.new(emission.outputs["Emission"], output.inputs["Surface"])
    emission.inputs["Strength"].default_value = 1.0

    material["tooncar_skybox_face"] = name
    return material


def add_tooncar_skybox(manifest, root, main_objects):
    sky = manifest.get("skybox")
    if not sky:
        return None

    faces = sky.get("faces", {})
    required = ("UP", "DN", "FR", "BK", "LF", "RT")
    if not all(face in faces for face in required):
        return None

    # ToonCar renders the sky as a camera-relative sky object. A centered
    # cubemap has the same angular appearance regardless of its physical cube
    # size. In Blender the cube still participates in depth testing, so keep a
    # large enclosure while preserving the original six-face projection.
    #
    # The executable contains the original sky-model scale 0.025; retain that
    # as metadata below. We intentionally do NOT shrink the Blender enclosure
    # to 128*0.025 because that would make it occlude normal level geometry,
    # unlike the original engine's sky render pass.
    h = 10000.0

    # Blender-space cube. Every face has its own 4 vertices so its cubemap
    # texture can use the complete 0..1 UV square independently.
    vertices = [
        # RT +X
        ( h,-h,-h), ( h, h,-h), ( h, h, h), ( h,-h, h),
        # LF -X
        (-h, h,-h), (-h,-h,-h), (-h,-h, h), (-h, h, h),
        # FR -Y
        (-h,-h,-h), ( h,-h,-h), ( h,-h, h), (-h,-h, h),
        # BK +Y
        ( h, h,-h), (-h, h,-h), (-h, h, h), ( h, h, h),
        # UP +Z
        (-h,-h, h), ( h,-h, h), ( h, h, h), (-h, h, h),
        # DN -Z
        (-h, h,-h), ( h, h,-h), ( h,-h,-h), (-h,-h,-h),
    ]

    polygons = [
        (0, 1, 2, 3),
        (4, 5, 6, 7),
        (8, 9,10,11),
        (12,13,14,15),
        (16,17,18,19),
        (20,21,22,23),
    ]

    face_order = ("RT", "LF", "FR", "BK", "UP", "DN")

    mesh = bpy.data.meshes.new("ToonCar Skybox")
    mesh.from_pydata(vertices, [], polygons)
    mesh.update()

    obj = bpy.data.objects.new("ToonCar Skybox", mesh)
    collection = bpy.data.collections.new("Skybox")
    bpy.context.scene.collection.children.link(collection)
    collection.objects.link(obj)

    uv_layer = mesh.uv_layers.new(name="UVMap")
    uv_square = (
        (0.0, 0.0),
        (1.0, 0.0),
        (1.0, 1.0),
        (0.0, 1.0),
    )

    # ToonCar's four side cubemap faces use the opposite image orientation
    # from the way these inward-facing Blender quads are laid out.
    # UP/DN are already correct; FR/BK/LF/RT need both U and V flipped.
    uv_square_side_uv_flipped = (
        (1.0, 1.0),
        (0.0, 1.0),
        (0.0, 0.0),
        (1.0, 0.0),
    )

    bank_dir = sky["bank_directory"]

    for poly_index, face in enumerate(face_order):
        image_path = (
            root
            / "textures"
            / bank_dir
            / faces[face]["png"]
        )

        material = make_sky_material(
            "Sky_" + face,
            image_path,
        )
        mesh.materials.append(material)

        poly = mesh.polygons[poly_index]
        poly.material_index = poly_index

        face_uvs = (
            uv_square
            if face in ("UP", "DN")
            else uv_square_side_uv_flipped
        )

        for loop_index, uv in zip(poly.loop_indices, face_uvs):
            uv_layer.data[loop_index].uv = uv

    # Render and view the cube from inside. Disable culling explicitly because
    # viewport backface behavior varies between Blender versions.
    for material in mesh.materials:
        try:
            material.use_backface_culling = False
        except Exception:
            pass

    obj["tooncar_role"] = "skybox"
    obj["tooncar_source_bank_offset"] = sky["bank_offset"]
    obj["tooncar_half_extent"] = h
    obj["tooncar_render_model"] = "camera_relative_cube"
    obj["tooncar_original_engine_model_scale"] = 0.025
    obj["tooncar_note"] = (
        "Six original cubemap faces; camera-relative like ToonCar. "
        "Large Blender enclosure is depth-safe and does not change angular projection."
    )

    # Follow the active render camera without inheriting its rotation.
    # ToonCar sky should react to camera rotation by view direction only; the
    # sky object itself follows translation so the camera never approaches a wall.
    scene = bpy.context.scene

    for axis in range(3):
        try:
            fcurve = obj.driver_add("location", axis)
            driver = fcurve.driver
            driver.type = 'SCRIPTED'

            variable = driver.variables.new()
            variable.name = "cam"
            variable.type = 'SINGLE_PROP'
            target = variable.targets[0]
            target.id_type = 'SCENE'
            target.id = scene
            target.data_path = f"camera.location[{axis}]"

            driver.expression = "cam"
        except Exception:
            # If no active scene camera exists yet, leave the box at origin.
            # The driver data path becomes useful as soon as scene.camera is set.
            pass

    return obj



def image_to_numpy_rgba(image):
    # Blender Image.pixels -> H x W x 4 float32.
    width, height = image.size
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    return pixels.reshape((height, width, 4))


def sample_image_nearest(arr, u, v):
    # Sample with Blender UV convention: V=0 is the bottom image row.
    h, w, _ = arr.shape
    x = np.clip(np.rint(u * (w - 1)).astype(np.int32), 0, w - 1)
    y = np.clip(np.rint(v * (h - 1)).astype(np.int32), 0, h - 1)
    return arr[y, x]


def generate_equirectangular_from_tooncar_cubemap(manifest, root):
    # Convert the six original ToonCar cubemap faces to a 2:1 panorama.
    # Orientation exactly follows the verified v24/v25 cube:
    #   UP/DN unchanged
    #   FR/BK/LF/RT flipped in both U and V.
    sky = manifest.get("skybox")
    if not sky:
        return None

    faces_meta = sky.get("faces", {})
    required = ("UP", "DN", "FR", "BK", "LF", "RT")
    if not all(face in faces_meta for face in required):
        return None

    bank_dir = sky["bank_directory"]
    arrays = {}
    images = {}

    for face in required:
        image_path = root / "textures" / bank_dir / faces_meta[face]["png"]
        image = bpy.data.images.load(str(image_path), check_existing=True)
        images[face] = image
        arrays[face] = image_to_numpy_rgba(image)

    face_width = int(images["FR"].size[0])
    output_width = face_width * 4
    output_height = output_width // 2

    # Build a direction vector for every equirectangular pixel.
    x = (np.arange(output_width, dtype=np.float32) + 0.5) / output_width
    y = (np.arange(output_height, dtype=np.float32) + 0.5) / output_height

    longitude = (x * 2.0 - 1.0) * np.pi
    latitude = (0.5 - y) * np.pi

    lon, lat = np.meshgrid(longitude, latitude)
    cos_lat = np.cos(lat)

    dx = cos_lat * np.sin(lon)
    dy = -cos_lat * np.cos(lon)
    dz = np.sin(lat)

    ax = np.abs(dx)
    ay = np.abs(dy)
    az = np.abs(dz)

    result = np.zeros((output_height, output_width, 4), dtype=np.float32)
    result[..., 3] = 1.0

    dominant_x = (ax >= ay) & (ax >= az)
    dominant_y = (ay > ax) & (ay >= az)
    dominant_z = ~(dominant_x | dominant_y)

    def put(face, mask, u, v):
        if np.any(mask):
            result[mask] = sample_image_nearest(
                arrays[face],
                u[mask],
                v[mask],
            )

    # +X = RT
    denom = np.maximum(ax, 1e-12)
    gy = dy / denom
    gz = dz / denom
    mask = dominant_x & (dx >= 0.0)
    put("RT", mask, (1.0 - gy) * 0.5, (1.0 - gz) * 0.5)

    # -X = LF
    mask = dominant_x & (dx < 0.0)
    put("LF", mask, (1.0 + gy) * 0.5, (1.0 - gz) * 0.5)

    # -Y = FR
    denom_y = np.maximum(ay, 1e-12)
    gx = dx / denom_y
    gz_y = dz / denom_y
    mask = dominant_y & (dy < 0.0)
    put("FR", mask, (1.0 - gx) * 0.5, (1.0 - gz_y) * 0.5)

    # +Y = BK
    mask = dominant_y & (dy >= 0.0)
    put("BK", mask, (1.0 + gx) * 0.5, (1.0 - gz_y) * 0.5)

    # +Z = UP
    denom_z = np.maximum(az, 1e-12)
    gx_z = dx / denom_z
    gy_z = dy / denom_z
    mask = dominant_z & (dz >= 0.0)
    put("UP", mask, (gx_z + 1.0) * 0.5, (gy_z + 1.0) * 0.5)

    # -Z = DN
    mask = dominant_z & (dz < 0.0)
    put("DN", mask, (gx_z + 1.0) * 0.5, (1.0 - gy_z) * 0.5)

    sky_dir = root / "skybox"
    sky_dir.mkdir(parents=True, exist_ok=True)
    output_path = sky_dir / "environment.png"

    # result is top->bottom; Blender's image pixel buffer is bottom->top.
    pixels = np.flipud(result).reshape(-1)

    image = bpy.data.images.new(
        "ToonCar Environment",
        width=output_width,
        height=output_height,
        alpha=False,
        float_buffer=False,
    )
    image.pixels.foreach_set(pixels)
    image.filepath_raw = str(output_path)
    image.file_format = 'PNG'
    image.save()

    # Use the saved asset as a normal file-backed Blender image.
    try:
        bpy.data.images.remove(image)
    except Exception:
        pass

    image = bpy.data.images.load(str(output_path), check_existing=True)

    return {
        "image": image,
        "path": output_path,
        "width": output_width,
        "height": output_height,
    }


def configure_world_environment(
    environment,
    bright_preview=True,
    realistic_lighting=False,
):
    if not environment:
        return False

    world = bpy.data.worlds.get("ToonCar World")
    if world is None:
        world = bpy.data.worlds.new("ToonCar World")

    bpy.context.scene.world = world
    world.use_nodes = True

    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputWorld")

    # Camera background: show the converted ToonCar sky exactly as a visible
    # environment, without using that LDR image as the only light source.
    environment_node = nodes.new("ShaderNodeTexEnvironment")
    environment_node.image = environment["image"]
    environment_node.projection = 'EQUIRECTANGULAR'
    environment_node.location = (-300, 0)

    visible_background = nodes.new("ShaderNodeBackground")
    visible_background.name = "ToonCar Visible Sky"
    visible_background.label = "Visible Sky"
    visible_background.location = (240, -120)
    visible_background.inputs["Strength"].default_value = 1.0
    links.new(
        environment_node.outputs["Color"],
        visible_background.inputs["Color"],
    )

    # Neutral ambient branch. This makes Material Preview behave much closer to
    # an unlit/emissive texture viewer while still leaving the actual materials
    # as normal Principled shaders for Rendered View.
    ambient_background = nodes.new("ShaderNodeBackground")
    ambient_background.name = (
        "ToonCar Environment Lighting"
        if realistic_lighting
        else "ToonCar Preview Ambient"
    )
    ambient_background.label = (
        "Environment Lighting"
        if realistic_lighting
        else "Preview Ambient"
    )
    ambient_background.location = (240, 120)
    ambient_background.inputs["Color"].default_value = (
        1.0,
        1.0,
        1.0,
        1.0,
    )
    ambient_background.inputs["Strength"].default_value = 1.0

    # Realistic preset: use the generated environment texture not only as the
    # visible sky, but also as the World lighting color.
    if realistic_lighting:
        links.new(
            environment_node.outputs["Color"],
            ambient_background.inputs["Color"],
        )

    light_path = nodes.new("ShaderNodeLightPath")
    light_path.location = (-20, -360)

    mix = nodes.new("ShaderNodeMixShader")
    mix.location = (560, 0)

    output.location = (820, 0)

    # Is Camera Ray = 1 -> visible ToonCar sky.
    # Other rays -> bright neutral ambient illumination.
    links.new(
        light_path.outputs["Is Camera Ray"],
        mix.inputs[0],
    )
    links.new(
        ambient_background.outputs["Background"],
        mix.inputs[1],
    )
    links.new(
        visible_background.outputs["Background"],
        mix.inputs[2],
    )
    links.new(
        mix.outputs["Shader"],
        output.inputs["Surface"],
    )

    world["tooncar_environment_texture"] = str(environment["path"])
    world["tooncar_visible_sky_strength"] = 1.0
    world["tooncar_preview_ambient_strength"] = 1.0
    world["tooncar_environment_lighting"] = bool(
        realistic_lighting
    )
    world["tooncar_environment_connected_to_visible_sky"] = True
    world["tooncar_environment_connected_to_lighting"] = bool(
        realistic_lighting
    )
    return True



def add_tooncar_sun():
    # Simple directional light for readable Rendered View.
    # The original game used much simpler lighting than modern PBR, so one
    # broad Sun plus the LDR sky environment gives a useful approximation.
    existing = bpy.data.objects.get("ToonCar Sun")
    if existing:
        return existing

    light_data = bpy.data.lights.new(
        name="ToonCar Sun",
        type='SUN',
    )
    light_data.energy = 2.0

    # Slightly softened sun shadows.
    try:
        light_data.angle = math.radians(5.0)
    except Exception:
        pass

    light_obj = bpy.data.objects.new(
        name="ToonCar Sun",
        object_data=light_data,
    )
    bpy.context.scene.collection.objects.link(light_obj)

    # Diagonal midday-ish direction. SUN position is irrelevant; only rotation
    # matters. This gives readable forms without trying to infer an exact sun
    # vector that is not stored in the skybox bitmap itself.
    light_obj.rotation_euler = (
        math.radians(35.0),
        math.radians(-20.0),
        math.radians(-35.0),
    )

    light_obj["tooncar_generated_light"] = True
    light_obj["tooncar_note"] = (
        "Approximate viewer light; not decoded from original R3D."
    )

    return light_obj



def collapse_all_outliners():
    # Save the .blend with a clean, collapsed Outliner hierarchy.
    # show_hierarchy expands the hierarchy consistently first; the following
    # recursive toggle collapses it. Failure here must never break conversion.
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue

        for area in screen.areas:
            if area.type != 'OUTLINER':
                continue

            region = next(
                (
                    region
                    for region in area.regions
                    if region.type == 'WINDOW'
                ),
                None,
            )
            if region is None:
                continue

            try:
                with bpy.context.temp_override(
                    window=window,
                    screen=screen,
                    area=area,
                    region=region,
                ):
                    bpy.ops.outliner.show_hierarchy()
                    bpy.ops.outliner.expanded_toggle()
            except Exception:
                pass








def detect_user_desktop_path():
    home = Path.home()

    candidates = [
        home / "Desktop",
        home / "OneDrive" / "Desktop",
    ]

    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except Exception:
            pass

    return candidates[0]


def set_render_output_to_user_desktop(
    scene,
    render_name="ToonCar_Render.png",
):
    desktop = detect_user_desktop_path()
    output_path = desktop / render_name

    scene.render.filepath = str(output_path)

    scene["tooncar_render_output_directory"] = str(
        desktop
    )
    scene["tooncar_render_output_filepath"] = str(
        output_path
    )

    return output_path


def add_map_top_camera(
    scene,
    main_objects,
    ortho_scale=180.0,
    render_width=8192,
    render_height=8192,
):
    # Fixed orthographic scale intentionally stays identical between maps so
    # their apparent size can be compared directly.
    points = []

    for obj in main_objects:
        if obj.type != 'MESH':
            continue

        try:
            for corner in obj.bound_box:
                points.append(
                    obj.matrix_world @ Vector(corner)
                )
        except Exception:
            pass

    if points:
        min_x = min(p.x for p in points)
        min_y = min(p.y for p in points)
        min_z = min(p.z for p in points)
        max_x = max(p.x for p in points)
        max_y = max(p.y for p in points)
        max_z = max(p.z for p in points)

        center = Vector((
            (min_x + max_x) * 0.5,
            (min_y + max_y) * 0.5,
            (min_z + max_z) * 0.5,
        ))

        scene["tooncar_map_bounds_blender"] = [
            min_x,
            min_y,
            min_z,
            max_x,
            max_y,
            max_z,
        ]
    else:
        center = Vector((0.0, 0.0, 0.0))

    camera_data = bpy.data.cameras.new(
        "ToonCar Map Top Camera"
    )
    camera_data.type = 'ORTHO'
    camera_data.ortho_scale = float(
        ortho_scale
    )
    camera_data.clip_start = 0.1
    camera_data.clip_end = 50000.0

    camera_obj = bpy.data.objects.new(
        "ToonCar Map Top Camera",
        camera_data,
    )
    scene.collection.objects.link(camera_obj)

    # Pure bird's-eye / top-down view.
    # Place the camera directly above the map center and look straight down
    # along world -Z, with no tilt at all.
    camera_obj.location = (
        center.x,
        center.y,
        center.z + 500.0,
    )
    camera_obj.rotation_euler = (
        0.0,
        0.0,
        0.0,
    )

    scene.camera = camera_obj

    scene.render.resolution_x = int(
        render_width
    )
    scene.render.resolution_y = int(
        render_height
    )
    scene.render.resolution_percentage = 100

    try:
        scene.render.pixel_aspect_x = 1.0
        scene.render.pixel_aspect_y = 1.0
    except Exception:
        pass

    try:
        scene.render.image_settings.file_format = 'PNG'
    except Exception:
        pass

    camera_obj["tooncar_role"] = (
        "map_top_camera"
    )
    camera_obj["tooncar_fixed_ortho_scale"] = float(
        ortho_scale
    )
    camera_obj["tooncar_render_resolution"] = (
        f"{int(render_width)}x{int(render_height)}"
    )

    scene[
        "tooncar_config_top_map_camera"
    ] = True
    scene[
        "tooncar_top_camera_ortho_scale"
    ] = float(ortho_scale)
    scene[
        "tooncar_render_aspect"
    ] = "1:1"
    scene[
        "tooncar_render_resolution"
    ] = f"{int(render_width)}x{int(render_height)}"

    return camera_obj


def ensure_long_animation_preview(
    scene,
    minutes=5.0,
):
    # Cyclic Actions repeat indefinitely; this only makes Blender's playback
    # range long enough that normal previewing does not hit the timeline end.
    fps = float(scene.render.fps) / max(
        float(scene.render.fps_base),
        1e-8,
    )

    preview_frames = int(
        round(
            max(1.0, float(minutes))
            * 60.0
            * fps
        )
    )

    scene.frame_start = 1
    scene.frame_end = max(
        int(scene.frame_end),
        preview_frames,
    )

    scene["tooncar_preview_duration_minutes"] = float(
        minutes
    )
    scene["tooncar_preview_frame_end"] = int(
        scene.frame_end
    )

    return int(scene.frame_end)


def set_gltf_single_cycle_timeline(
    scene,
    fallback_seconds=1.0,
):
    # glTF should contain one natural animation cycle only.
    # Three.js can repeat that clip indefinitely with LoopRepeat.
    #
    # Action.frame_range is based on real stored keyframes, so F-Curve Cycles
    # modifiers do not artificially extend this range.
    longest_end = 1.0
    action_count = 0

    for action in bpy.data.actions:
        try:
            frame_start, frame_end = action.frame_range
        except Exception:
            continue

        if frame_end <= frame_start:
            continue

        action_count += 1
        longest_end = max(
            longest_end,
            float(frame_end),
        )

    if action_count == 0:
        fps = float(scene.render.fps) / max(
            float(scene.render.fps_base),
            1e-8,
        )
        longest_end = max(
            2.0,
            float(fallback_seconds) * fps,
        )

    scene.frame_start = 1
    scene.frame_end = max(
        2,
        int(math.ceil(longest_end)),
    )

    scene["tooncar_gltf_timeline_mode"] = (
        "single_natural_action_cycle"
    )
    scene["tooncar_gltf_timeline_frame_end"] = int(
        scene.frame_end
    )
    scene["tooncar_gltf_action_count"] = int(
        action_count
    )

    return int(scene.frame_end)



def main():
    args = args_after_double_dash()
    if len(args) < 2:
        raise RuntimeError(
            "Expected: -- <manifest.json> <output.blend> [options...]"
        )

    manifest_path = Path(args[0]).resolve()
    output_blend = Path(args[1]).resolve()
    root = manifest_path.parent

    def arg_bool(index, default):
        if len(args) <= index:
            return default
        return args[index] not in ("0", "false", "False", "no", "NO")

    sky_mode = (
        args[2].strip().lower()
        if len(args) > 2
        else "environment"
    )
    if sky_mode not in ("environment", "tooncar"):
        sky_mode = "environment"

    hide_destroyed_props = arg_bool(3, True)
    hide_raw_asset_meshes = arg_bool(4, True)
    generate_sun = arg_bool(5, False)
    material_preview_environment = arg_bool(6, True)
    bright_material_preview = arg_bool(7, True)
    hide_viewport_grid_axes = arg_bool(8, True)
    unlit_materials = arg_bool(9, True)
    animate_textures = arg_bool(10, True)
    include_animated_prop_geometry = arg_bool(11, True)

    include_ai_path = arg_bool(12, True)
    include_item_boxes = arg_bool(13, True)
    use_sorpresa_asset = arg_bool(14, True)
    animate_sorpresa = arg_bool(15, True)

    try:
        sorpresa_size_multiplier = (
            float(args[16]) if len(args) > 16 else 0.65
        )
    except Exception:
        sorpresa_size_multiplier = 0.65

    sorpresa_size_multiplier = max(
        0.10,
        min(2.0, sorpresa_size_multiplier),
    )

    use_r3d_texture_timing = arg_bool(17, True)

    try:
        texture_animation_interval = float(args[18]) if len(args) > 18 else 0.20
    except Exception:
        texture_animation_interval = 0.20

    texture_animation_interval = max(
        0.01,
        min(2.0, texture_animation_interval),
    )

    include_cones = arg_bool(19, True)
    use_cone_models = arg_bool(20, True)
    show_cone_paths = arg_bool(21, True)
    add_isometric_camera = arg_bool(22, True)

    material_profile = (
        args[23].strip().lower()
        if len(args) > 23
        else "game"
    )
    if material_profile not in ("game", "realistic", "gltf"):
        material_profile = "game"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Clean default startup scene.
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)

    for datablocks in (
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.materials,
        bpy.data.cameras,
        bpy.data.lights,
    ):
        for block in list(datablocks):
            if block.users == 0:
                datablocks.remove(block)

    scene = bpy.context.scene

    # ToonCar uses simple LDR textures. Standard preserves their stored RGB
    # appearance more directly than Blender's AgX view transform.
    try:
        scene.view_settings.view_transform = 'Standard'
    except Exception:
        pass

    scene["tooncar_source"] = manifest["source"]["filename"]
    scene["tooncar_unpacker_version"] = manifest.get("version", 102)
    scene["tooncar_export_scale"] = manifest.get("export_scale", 0.1)
    scene["tooncar_color_view_transform"] = "Standard"
    scene["tooncar_config_sky_mode"] = sky_mode
    scene["tooncar_config_cube_skybox"] = (sky_mode == "tooncar")
    scene["tooncar_config_environment"] = (sky_mode == "environment")
    scene["tooncar_config_hide_destroyed"] = hide_destroyed_props
    scene["tooncar_config_hide_raw_assets"] = hide_raw_asset_meshes
    scene["tooncar_config_sun"] = generate_sun
    scene["tooncar_config_preview_environment"] = material_preview_environment
    scene["tooncar_config_bright_material_preview"] = bright_material_preview
    scene["tooncar_config_hide_viewport_grid_axes"] = hide_viewport_grid_axes
    scene["tooncar_config_unlit_materials"] = unlit_materials
    scene["tooncar_config_animate_textures"] = animate_textures
    scene["tooncar_config_animated_prop_animation"] = include_animated_prop_geometry
    scene["tooncar_config_use_r3d_texture_timing"] = use_r3d_texture_timing
    scene["tooncar_texture_animation_game_tick_hz"] = 55
    scene["tooncar_config_texture_animation_override_interval"] = texture_animation_interval
    scene["tooncar_config_top_map_camera"] = bool(
        add_isometric_camera
    )
    scene["tooncar_material_profile"] = material_profile
    scene["tooncar_gltf_target"] = (
        "Three.js GLTFLoader"
        if material_profile == "gltf"
        else ""
    )

    main_collection = bpy.data.collections.new("Static World")
    asset_collection = bpy.data.collections.new("Asset Meshes")
    scene.collection.children.link(main_collection)
    scene.collection.children.link(asset_collection)

    main_objects = []
    asset_objects = []

    meshes = manifest.get("meshes", [])
    for entry in meshes:
        obj_path = root / "meshes" / entry["obj"]
        if not obj_path.exists():
            print(f"SKIP missing OBJ: {obj_path}")
            continue

        imported = import_obj(obj_path)

        target_collection = (
            main_collection
            if entry.get("role") == "main"
            else asset_collection
        )
        move_objects_to_collection(imported, target_collection)

        for obj in imported:
            obj["tooncar_role"] = entry.get("role", "asset")
            obj["tooncar_source_offset"] = entry.get("offset", 0)
            obj["tooncar_vertex_count"] = entry.get("vertex_count", 0)
            obj["tooncar_face_count"] = entry.get("face_count", 0)

            if entry.get("role") == "main":
                main_objects.append(obj)
            else:
                asset_objects.append(obj)

    # Raw asset meshes are diagnostic/collision candidates.
    asset_collection.hide_viewport = bool(hide_raw_asset_meshes)
    asset_collection.hide_render = bool(hide_raw_asset_meshes)

    # Import reconstructed prop scene.
    #
    # ToonCar's hierarchy distinguishes the root parent part from child parts.
    # On destructible Venus props the parent is the intact model, while
    # child_XX parts are detached/destruction pieces. Keep both in the .blend,
    # but hide the destroyed variants by default.
    placed_props = manifest.get("placed_props")
    if placed_props and placed_props.get("obj"):
        prop_collection = bpy.data.collections.new("Placed Props")
        intact_collection = bpy.data.collections.new("Intact Props")
        destroyed_collection = bpy.data.collections.new("Destroyed Parts")

        scene.collection.children.link(prop_collection)
        prop_collection.children.link(intact_collection)
        prop_collection.children.link(destroyed_collection)

        prop_obj = root / placed_props["obj"]
        if prop_obj.exists():
            imported_props = import_obj(prop_obj)

            for obj in imported_props:
                is_destroyed_part = "_child_" in obj.name.lower()
                target = destroyed_collection if is_destroyed_part else intact_collection
                move_objects_to_collection([obj], target)

                obj["tooncar_role"] = (
                    "destroyed_prop_part" if is_destroyed_part else "intact_prop"
                )
                obj["tooncar_position_source"] = "R3D 0x44 instance matrix"
                obj["tooncar_destroyed_variant"] = bool(is_destroyed_part)

        # Optionally open the file in intact-only state.
        destroyed_collection.hide_viewport = bool(hide_destroyed_props)
        destroyed_collection.hide_render = bool(hide_destroyed_props)

    animated_collection = bpy.data.collections.new(
        "Animated Props"
    )
    scene.collection.children.link(animated_collection)

    animated_prop_report = build_animated_props(
        manifest,
        root,
        animated_collection,
        enabled=include_animated_prop_geometry,
    )
    scene["tooncar_animated_prop_instance_count"] = len(
        animated_prop_report
    )

    if not animated_prop_report:
        animated_collection.hide_viewport = True
        animated_collection.hide_render = True

    gameplay_report = build_gameplay_debug(
        manifest,
        scene,
        root,
        include_ai_path=include_ai_path,
        include_item_boxes=include_item_boxes,
        use_sorpresa_asset=use_sorpresa_asset,
        animate_sorpresa=animate_sorpresa,
        sorpresa_size_multiplier=sorpresa_size_multiplier,
        include_cones=include_cones,
        show_cone_paths=show_cone_paths,
        use_cone_models=use_cone_models,
    )

    if gameplay_report:
        gameplay_text = bpy.data.texts.new(
            "ToonCar - Gameplay Data"
        )
        gameplay_text.write(
            json.dumps(
                gameplay_report,
                ensure_ascii=False,
                indent=2,
            )
        )

    # Sky modes are mutually exclusive:
    #   environment = current Blender equirectangular World
    #   tooncar     = six original faces on a camera-relative cube
    skybox_object = None
    environment = None

    if sky_mode == "tooncar":
        skybox_object = add_tooncar_skybox(
            manifest,
            root,
            main_objects,
        )

        if skybox_object:
            skybox_object.hide_viewport = False
            skybox_object.hide_render = False

            for collection in skybox_object.users_collection:
                if collection.name == "Skybox":
                    collection.hide_viewport = False
                    collection.hide_render = False

        # Use a neutral World only for non-camera illumination. The visible
        # background comes from the physical ToonCar cube.
        world = bpy.data.worlds.get("ToonCar World")
        if world is None:
            world = bpy.data.worlds.new("ToonCar World")
        scene.world = world
        world.use_nodes = True
        nodes = world.node_tree.nodes
        links = world.node_tree.links
        nodes.clear()
        output = nodes.new("ShaderNodeOutputWorld")
        background = nodes.new("ShaderNodeBackground")
        background.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        background.inputs["Strength"].default_value = 1.0
        links.new(background.outputs["Background"], output.inputs["Surface"])

    else:
        environment = generate_equirectangular_from_tooncar_cubemap(
            manifest,
            root,
        )

        if environment:
            configure_world_environment(
                environment,
                bright_preview=bright_material_preview,
                realistic_lighting=bool(
                    generate_sun
                ),
            )
            scene["tooncar_environment_path"] = str(environment["path"])
            scene["tooncar_environment_resolution"] = (
                str(environment["width"]) + "x" + str(environment["height"])
            )

    scene["tooncar_skybox"] = bool(skybox_object)
    scene["tooncar_environment_generated"] = bool(environment)

    sun_object = add_tooncar_sun() if generate_sun else None
    scene["tooncar_generated_sun"] = bool(sun_object)

    # Reconstruct transparency from the alpha byte stored in R3D texture BGRA.
    alpha_materials = configure_material_alpha(manifest)
    scene["tooncar_alpha_material_count"] = len(alpha_materials)

    alpha_text = bpy.data.texts.new("ToonCar - Alpha Materials")
    alpha_text.write(
        json.dumps(alpha_materials, ensure_ascii=False, indent=2)
    )

    unlit_material_report = []
    gltf_material_report = []

    if material_profile == "gltf":
        gltf_material_report = (
            configure_gltf_friendly_materials(
                manifest
            )
        )
        add_gltf_export_metadata(
            scene,
            manifest,
            gltf_material_report,
        )
    elif unlit_materials:
        unlit_material_report = (
            convert_materials_to_unlit()
        )

    scene["tooncar_unlit_materials"] = bool(
        unlit_materials
        and material_profile != "gltf"
    )
    scene["tooncar_unlit_material_count"] = len(
        unlit_material_report
    )
    scene["tooncar_gltf_material_count"] = len(
        gltf_material_report
    )

    if unlit_material_report:
        unlit_text = bpy.data.texts.new(
            "ToonCar - Unlit Materials"
        )
        unlit_text.write(
            json.dumps(
                unlit_material_report,
                ensure_ascii=False,
                indent=2,
            )
        )

    texture_animation_report = configure_texture_animations(
        manifest,
        root,
        enabled=(
            animate_textures
            and material_profile != "gltf"
        ),
        use_r3d_timing=use_r3d_texture_timing,
        frame_interval=texture_animation_interval,
    )

    if animated_prop_report:
        mesh_anim_text = bpy.data.texts.new(
            "ToonCar - Mesh Animations"
        )
        mesh_anim_text.write(
            json.dumps(
                animated_prop_report,
                ensure_ascii=False,
                indent=2,
            )
        )
    scene["tooncar_texture_animation_count"] = len(texture_animation_report)

    if texture_animation_report:
        anim_text = bpy.data.texts.new("ToonCar - Texture Animations")
        anim_text.write(
            json.dumps(
                texture_animation_report,
                ensure_ascii=False,
                indent=2,
            )
        )

    # Save the .blend with all 3D views already in Material Preview.
    preview_viewports = set_material_preview_for_saved_file(
        use_environment=(
            material_preview_environment
            and sky_mode == "environment"
        ),
        hide_grid_axes=hide_viewport_grid_axes,
        use_scene_lights=(
            generate_sun
            and not bright_material_preview
            and not unlit_materials
        ),
    )
    scene["tooncar_material_preview_viewports"] = preview_viewports
    scene["tooncar_view_material_preset"] = material_profile

    # Store mapping diagnostics directly in the .blend as text blocks.
    mapping_path = root / "metadata" / "main_mesh_material_mapping.json"
    if mapping_path.exists():
        txt = bpy.data.texts.new("ToonCar - Material Mapping")
        txt.write(mapping_path.read_text(encoding="utf-8"))

    manifest_text = bpy.data.texts.new("ToonCar - Manifest")
    manifest_text.write(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )

    map_camera = None
    if add_isometric_camera:
        map_camera = add_map_top_camera(
            scene,
            main_objects,
            ortho_scale=180.0,
            render_width=8192,
            render_height=8192,
        )

    scene["tooncar_generated_top_camera"] = bool(
        map_camera
    )

    source_filename = (
        manifest.get("source", {}).get(
            "filename",
            "ToonCar.r3d",
        )
    )
    render_output_name = (
        f"{Path(source_filename).stem}_Render.png"
    )
    set_render_output_to_user_desktop(
        scene,
        render_name=render_output_name,
    )

    # Final render settings safeguard for exported top camera renders.
    scene.render.resolution_percentage = 100
    try:
        scene.render.pixel_aspect_x = 1.0
        scene.render.pixel_aspect_y = 1.0
    except Exception:
        pass

    # Keep the far plane well beyond the reconstructed ToonCar skybox.
    for camera in bpy.data.cameras:
        try:
            camera.clip_end = 50000.0
        except Exception:
            pass

    scene["tooncar_clip_end"] = 50000.0

    output_blend.parent.mkdir(parents=True, exist_ok=True)

    collapse_all_outliners()

    if material_profile == "gltf":
        set_gltf_single_cycle_timeline(
            scene,
            fallback_seconds=1.0,
        )
    else:
        ensure_long_animation_preview(
            scene,
            minutes=5.0,
        )

    bpy.ops.wm.save_as_mainfile(filepath=str(output_blend))
    print(f"TOONCAR_BLEND_SAVED={output_blend}")


if __name__ == "__main__":
    main()
"""
    path.write_text(code, encoding="utf-8")



def write_standalone_blender_builder_script(path: Path):
    code = r"""
import bpy
import json
import sys
from pathlib import Path
from mathutils import Vector


def args_after_double_dash():
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1:]


def import_obj(path):
    before = set(bpy.data.objects)

    try:
        bpy.ops.wm.obj_import(
            filepath=str(path),
            forward_axis='NEGATIVE_Z',
            up_axis='Y',
        )
    except Exception:
        bpy.ops.import_scene.obj(
            filepath=str(path),
            axis_forward='-Z',
            axis_up='Y',
        )

    return [
        obj
        for obj in bpy.data.objects
        if obj not in before
    ]


def set_material_render_mode(material, alpha_mode):
    if alpha_mode == "opaque":
        return

    if hasattr(material, "surface_render_method"):
        try:
            material.surface_render_method = (
                'BLENDED'
                if alpha_mode == "blend"
                else 'DITHERED'
            )
        except Exception:
            pass

    if hasattr(material, "blend_method"):
        try:
            material.blend_method = (
                'HASHED'
                if alpha_mode == "blend"
                else 'CLIP'
            )
        except Exception:
            try:
                material.blend_method = 'BLEND'
            except Exception:
                pass

    if hasattr(material, "alpha_threshold"):
        try:
            material.alpha_threshold = 0.5
        except Exception:
            pass


def configure_standalone_materials(
    objects,
    root,
    asset,
    unlit=True,
    gltf_friendly=False,
):
    textures = {
        int(entry.get("index", -1)): entry
        for entry in asset.get("textures", [])
    }

    selected_texture_index = asset.get(
        "selected_texture_index"
    )
    selected_texture = (
        textures.get(int(selected_texture_index))
        if selected_texture_index is not None
        else None
    )

    texture_dir = root / asset["texture_directory"]
    configured = []

    for material in bpy.data.materials:
        name = material.name

        material_index = None
        if name.startswith("mat_"):
            try:
                material_index = int(name[4:].split(".")[0])
            except Exception:
                material_index = None

        if material_index is None:
            continue

        tex_entry = (
            selected_texture
            if selected_texture is not None
            else textures.get(material_index)
        )
        if not tex_entry:
            continue

        image_path = texture_dir / tex_entry["png"]
        if not image_path.is_file():
            raise RuntimeError(
                f"Brak tekstury standalone assetu: {image_path}"
            )

        image = bpy.data.images.load(
            str(image_path),
            check_existing=True,
        )
        try:
            image.colorspace_settings.name = 'sRGB'
        except Exception:
            pass

        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()

        output = nodes.new("ShaderNodeOutputMaterial")
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = image
        texture.interpolation = 'Linear'

        alpha = tex_entry.get("alpha") or {}
        alpha_mode = alpha.get(
            "mode",
            "blend" if tex_entry.get("has_alpha") else "opaque",
        )

        if unlit:
            emission = nodes.new("ShaderNodeEmission")
            emission.inputs["Strength"].default_value = 1.0
            links.new(
                texture.outputs["Color"],
                emission.inputs["Color"],
            )

            if alpha_mode != "opaque":
                transparent = nodes.new("ShaderNodeBsdfTransparent")
                mix = nodes.new("ShaderNodeMixShader")
                links.new(texture.outputs["Alpha"], mix.inputs[0])
                links.new(transparent.outputs["BSDF"], mix.inputs[1])
                links.new(emission.outputs["Emission"], mix.inputs[2])
                links.new(mix.outputs["Shader"], output.inputs["Surface"])
                set_material_render_mode(material, alpha_mode)
            else:
                links.new(
                    emission.outputs["Emission"],
                    output.inputs["Surface"],
                )

            material["tooncar_unlit"] = True
        else:
            bsdf = nodes.new("ShaderNodeBsdfPrincipled")
            links.new(
                texture.outputs["Color"],
                bsdf.inputs["Base Color"],
            )

            if gltf_friendly:
                metallic = bsdf.inputs.get("Metallic")
                if metallic is not None:
                    metallic.default_value = 0.0

                roughness = bsdf.inputs.get("Roughness")
                if roughness is not None:
                    roughness.default_value = 1.0

                try:
                    material.use_backface_culling = False
                except Exception:
                    pass

                material["tooncar_gltf_ready"] = True
                material["tooncar_gltf_shader"] = "Principled BSDF"
                material["tooncar_gltf_alpha_mode"] = alpha_mode
                material["tooncar_gltf_metallic"] = 0.0
                material["tooncar_gltf_roughness"] = 1.0

            if alpha_mode != "opaque":
                links.new(
                    texture.outputs["Alpha"],
                    bsdf.inputs["Alpha"],
                )
                set_material_render_mode(material, alpha_mode)

            links.new(
                bsdf.outputs["BSDF"],
                output.inputs["Surface"],
            )

        material["tooncar_texture_source"] = tex_entry.get(
            "source_name",
            "",
        )
        material["tooncar_texture_index"] = material_index
        configured.append(material.name)

    return configured


def set_material_preview_for_saved_file(
    use_environment=True,
    hide_grid_axes=True,
):
    changed = 0

    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue

            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue

                try:
                    space.shading.type = 'MATERIAL'
                    space.shading.use_scene_world = bool(use_environment)
                    space.shading.use_scene_lights = False
                    changed += 1
                except Exception:
                    pass

                try:
                    space.clip_start = 0.01
                    space.clip_end = 50000.0
                except Exception:
                    pass

                if hide_grid_axes:
                    try:
                        overlay = space.overlay
                        overlay.show_floor = False
                        overlay.show_axis_x = False
                        overlay.show_axis_y = False
                        overlay.show_axis_z = False
                    except Exception:
                        pass

    return changed



def hide_viewport_overlays():
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue

            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue

                try:
                    space.overlay.show_floor = False
                    space.overlay.show_axis_x = False
                    space.overlay.show_axis_y = False
                    space.overlay.show_axis_z = False
                except Exception:
                    pass


def collapse_all_outliners():
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue

        for area in screen.areas:
            if area.type != 'OUTLINER':
                continue

            region = next(
                (
                    r
                    for r in area.regions
                    if r.type == 'WINDOW'
                ),
                None,
            )
            if region is None:
                continue

            try:
                with bpy.context.temp_override(
                    window=window,
                    screen=screen,
                    area=area,
                    region=region,
                ):
                    bpy.ops.outliner.show_hierarchy()
                    bpy.ops.outliner.expanded_toggle()
            except Exception:
                pass



def source_point_to_blender(point, scale):
    x, y, z = point
    return (
        float(x) * scale,
        float(z) * scale,
        float(y) * scale,
    )


def move_object_to_collection(obj, collection):
    for old_collection in list(obj.users_collection):
        old_collection.objects.unlink(obj)

    collection.objects.link(obj)


def find_car_mesh_object(objects, mesh_index):
    prefix = f"CarMesh_{mesh_index:02d}_"

    for obj in objects:
        if obj.name.startswith(prefix):
            return obj

    return None


def assemble_car(imported, scene, manifest):
    asset = manifest["asset"]
    assembly = asset.get("assembly") or {}
    scale = float(manifest.get("export_scale", 0.1))

    body_lods = [
        int(x)
        for x in assembly.get(
            "body_lod_mesh_indices",
            [assembly.get("body_mesh_index", 0)],
        )
        if x is not None
    ]
    wheel_lods = [
        int(x)
        for x in assembly.get(
            "wheel_lod_levels",
            assembly.get("wheel_mesh_indices", []),
        )
    ]

    collision_indices = [
        int(x)
        for x in assembly.get("collision_mesh_indices", [])
    ]
    shadow_indices = [
        int(x)
        for x in assembly.get("shadow_mesh_indices", [])
    ]
    alternate_indices = [
        int(x)
        for x in assembly.get("alternate_mesh_indices", [])
    ]
    wheel_positions = assembly.get(
        "wheel_positions_source",
        [],
    )

    root_collection = bpy.data.collections.new("Car")
    scene.collection.children.link(root_collection)

    lod_root = bpy.data.collections.new("LODs")
    helpers_collection = bpy.data.collections.new("Helpers")
    root_collection.children.link(lod_root)
    root_collection.children.link(helpers_collection)

    template_collection = bpy.data.collections.new(
        "Wheel Templates - Hidden"
    )
    collision_collection = bpy.data.collections.new("Collision")
    shadow_collection = bpy.data.collections.new(
        "Shadow - Planar Helpers"
    )
    unknown_collection = bpy.data.collections.new(
        "Unknown - Hidden"
    )

    helpers_collection.children.link(template_collection)
    helpers_collection.children.link(collision_collection)
    helpers_collection.children.link(shadow_collection)
    helpers_collection.children.link(unknown_collection)

    lod_names = [
        "LOD 0 - High",
        "LOD 1 - Medium",
        "LOD 2 - Low",
        "LOD 3 - Lowest",
    ]
    wheel_labels = (
        "Front Left",
        "Front Right",
        "Rear Left",
        "Rear Right",
    )

    max_lods = max(len(body_lods), len(wheel_lods), 1)

    for lod_index in range(max_lods):
        lod_name = (
            lod_names[lod_index]
            if lod_index < len(lod_names)
            else f"LOD {lod_index}"
        )
        lod_collection = bpy.data.collections.new(lod_name)
        lod_root.children.link(lod_collection)

        body_collection = bpy.data.collections.new("Body")
        wheels_collection = bpy.data.collections.new("Wheels")
        lod_collection.children.link(body_collection)
        lod_collection.children.link(wheels_collection)

        # Highest detail visible by default; lower LODs retained but hidden.
        if lod_index > 0:
            lod_collection.hide_viewport = True
            lod_collection.hide_render = True

        if lod_index < len(body_lods):
            body_mesh_index = body_lods[lod_index]
            body_obj = find_car_mesh_object(
                imported,
                body_mesh_index,
            )
            if body_obj is not None:
                body_obj.name = f"Body LOD {lod_index}"
                move_object_to_collection(
                    body_obj,
                    body_collection,
                )
                body_obj["tooncar_car_lod"] = lod_index
                body_obj["tooncar_car_mesh_index"] = body_mesh_index

        if lod_index < len(wheel_lods):
            wheel_mesh_index = wheel_lods[lod_index]
            wheel_template = find_car_mesh_object(
                imported,
                wheel_mesh_index,
            )

            if wheel_template is not None:
                wheel_template.name = (
                    f"Wheel LOD {lod_index} Template"
                )
                move_object_to_collection(
                    wheel_template,
                    template_collection,
                )
                wheel_template.hide_viewport = True
                wheel_template.hide_render = True
                wheel_template.hide_set(True)

                for wheel_index, point in enumerate(wheel_positions[:4]):
                    wheel_side_collection = bpy.data.collections.new(
                        wheel_labels[wheel_index]
                    )
                    wheels_collection.children.link(
                        wheel_side_collection
                    )

                    location = source_point_to_blender(
                        point,
                        scale,
                    )

                    instance = bpy.data.objects.new(
                        wheel_labels[wheel_index],
                        wheel_template.data,
                    )
                    wheel_side_collection.objects.link(instance)
                    instance.matrix_world = (
                        wheel_template.matrix_world.copy()
                    )
                    instance.location.x += location[0]
                    instance.location.y += location[1]
                    instance.location.z += location[2]

                    # Mesh is authored for one side. Source X maps to Blender X.
                    # Mirror the +X side so the textured outer face points out.
                    source_x = float(point[0])
                    mirrored = source_x > 0.0
                    if mirrored:
                        instance.scale.x *= -1.0

                    instance["tooncar_car_lod"] = lod_index
                    instance["tooncar_car_wheel_index"] = wheel_index
                    instance["tooncar_car_mesh_index"] = wheel_mesh_index
                    instance["tooncar_car_mirrored"] = mirrored

    for mesh_index in collision_indices:
        obj = find_car_mesh_object(imported, mesh_index)
        if obj is None:
            continue
        obj.name = f"Collision_{mesh_index:02d}"
        move_object_to_collection(obj, collision_collection)
        obj.hide_viewport = True
        obj.hide_render = True

    for mesh_index in shadow_indices:
        obj = find_car_mesh_object(imported, mesh_index)
        if obj is None:
            continue
        obj.name = f"Shadow_{mesh_index:02d}"
        move_object_to_collection(obj, shadow_collection)
        obj.hide_viewport = True
        obj.hide_render = True

    for mesh_index in alternate_indices:
        obj = find_car_mesh_object(imported, mesh_index)
        if obj is None:
            continue
        obj.name = f"Unknown_{mesh_index:02d}"
        move_object_to_collection(obj, unknown_collection)
        obj.hide_viewport = True
        obj.hide_render = True

    template_collection.hide_viewport = True
    template_collection.hide_render = True
    collision_collection.hide_viewport = True
    collision_collection.hide_render = True
    shadow_collection.hide_viewport = True
    shadow_collection.hide_render = True
    unknown_collection.hide_viewport = True
    unknown_collection.hide_render = True

    scene["tooncar_car_default_lod"] = 0
    scene["tooncar_car_lod_count"] = max_lods

    return root_collection



def frame_visible_model_in_viewports(scene):
    # Save asset/car .blend files with the visible model framed similarly to
    # Blender's Numpad '.' / View Selected.
    #
    # Prefer the real VIEW3D operator when a window context exists. Blender
    # running in --background may not expose one, so there is a robust fallback
    # that fits every RegionView3D to the exact world-space bounding box.

    view_layer = bpy.context.view_layer
    visible_objects = []

    for obj in scene.objects:
        if obj.type != 'MESH':
            continue

        try:
            if not obj.visible_get(view_layer=view_layer):
                continue
        except Exception:
            try:
                if obj.hide_viewport or obj.hide_get():
                    continue
            except Exception:
                pass

        visible_objects.append(obj)

    if not visible_objects:
        return 0

    bpy.ops.object.select_all(action='DESELECT')

    for obj in visible_objects:
        try:
            obj.select_set(True)
        except Exception:
            pass

    try:
        view_layer.objects.active = visible_objects[0]
    except Exception:
        pass

    framed = 0

    # Preferred route: Blender's actual View Selected.
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue

        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue

            region = next(
                (
                    region
                    for region in area.regions
                    if region.type == 'WINDOW'
                ),
                None,
            )
            if region is None:
                continue

            try:
                with bpy.context.temp_override(
                    window=window,
                    screen=screen,
                    area=area,
                    region=region,
                ):
                    bpy.ops.view3d.view_selected(
                        use_all_regions=False,
                    )
                    framed += 1
            except Exception:
                pass

    # Background-mode fallback: compute exact visible bounds.
    if framed == 0:
        points = []

        for obj in visible_objects:
            try:
                for corner in obj.bound_box:
                    points.append(
                        obj.matrix_world @ Vector(corner)
                    )
            except Exception:
                pass

        if points:
            min_x = min(p.x for p in points)
            min_y = min(p.y for p in points)
            min_z = min(p.z for p in points)
            max_x = max(p.x for p in points)
            max_y = max(p.y for p in points)
            max_z = max(p.z for p in points)

            center = Vector((
                (min_x + max_x) * 0.5,
                (min_y + max_y) * 0.5,
                (min_z + max_z) * 0.5,
            ))

            extent = Vector((
                max_x - min_x,
                max_y - min_y,
                max_z - min_z,
            ))

            radius = max(extent.length * 0.5, 0.001)
            distance = radius * 1.35

            for screen in bpy.data.screens:
                for area in screen.areas:
                    if area.type != 'VIEW_3D':
                        continue

                    for space in area.spaces:
                        if space.type != 'VIEW_3D':
                            continue

                        try:
                            space.region_3d.view_location = center
                            space.region_3d.view_distance = distance
                            framed += 1
                        except Exception:
                            pass

    bpy.ops.object.select_all(action='DESELECT')
    return framed




def matrix_translation_source(matrix_values):
    # ToonCar uses row-vector affine matrices; translation is at indices
    # 12/13/14, matching the previously decoded animated-prop transforms.
    return (
        float(matrix_values[12]),
        float(matrix_values[13]),
        float(matrix_values[14]),
    )


def find_character_lod_object(objects, lod_index):
    prefix = f"CharacterLOD_{lod_index:02d}"

    for obj in objects:
        if obj.name.startswith(prefix):
            return obj

    return None


def assemble_character(imported, scene, manifest):
    asset = manifest["asset"]
    lods = asset.get("lods") or []
    skeleton = asset.get("skeleton") or {}
    nodes = skeleton.get("nodes") or []
    scale = float(manifest.get("export_scale", 0.1))

    root_collection = bpy.data.collections.new("Character")
    scene.collection.children.link(root_collection)

    lod_root = bpy.data.collections.new("LODs")
    rig_collection = bpy.data.collections.new("Rig")
    root_collection.children.link(lod_root)
    root_collection.children.link(rig_collection)

    # Build Armature from the decoded node hierarchy.
    armature_data = bpy.data.armatures.new("ToonCar Rig")
    armature_obj = bpy.data.objects.new(
        "ToonCar Rig",
        armature_data,
    )
    rig_collection.objects.link(armature_obj)
    armature_obj.show_in_front = True
    armature_obj.data.display_type = 'OCTAHEDRAL'

    bpy.context.view_layer.objects.active = armature_obj
    armature_obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')

    edit_bones = {}

    node_positions = {
        int(node["index"]): Vector(
            source_point_to_blender(
                matrix_translation_source(
                    node["global_matrix"]
                ),
                scale,
            )
        )
        for node in nodes
    }

    for node in nodes:
        index = int(node["index"])
        bone = armature_data.edit_bones.new(
            f"Bone_{index:02d}"
        )

        head = node_positions[index]
        child_indices = [
            int(x)
            for x in node.get("child_indices", [])
            if int(x) in node_positions
        ]

        if child_indices:
            # Aim toward the average child joint for branching nodes.
            tail = sum(
                (node_positions[x] for x in child_indices),
                Vector((0.0, 0.0, 0.0)),
            ) / len(child_indices)
        else:
            parent_index = node.get("parent_index")
            if (
                parent_index is not None
                and int(parent_index) in node_positions
            ):
                direction = (
                    head
                    - node_positions[int(parent_index)]
                )
                if direction.length > 1e-6:
                    tail = head + direction.normalized() * max(
                        direction.length * 0.35,
                        0.025,
                    )
                else:
                    tail = head + Vector((0.0, 0.0, 0.05))
            else:
                tail = head + Vector((0.0, 0.0, 0.05))

        if (tail - head).length < 1e-5:
            tail = head + Vector((0.0, 0.0, 0.05))

        bone.head = head
        bone.tail = tail
        edit_bones[index] = bone

    for node in nodes:
        index = int(node["index"])
        parent_index = node.get("parent_index")

        if (
            parent_index is not None
            and int(parent_index) in edit_bones
        ):
            edit_bones[index].parent = edit_bones[
                int(parent_index)
            ]
            edit_bones[index].use_connect = False

    bpy.ops.object.mode_set(mode='OBJECT')
    armature_obj.select_set(False)

    lod_names = [
        "LOD 0 - High",
        "LOD 1 - Medium",
        "LOD 2 - Low",
        "LOD 3 - Lowest",
    ]

    for lod_info in lods:
        lod_index = int(lod_info["lod_index"])
        obj = find_character_lod_object(
            imported,
            lod_index,
        )
        if obj is None:
            continue

        lod_name = (
            lod_names[lod_index]
            if lod_index < len(lod_names)
            else f"LOD {lod_index}"
        )
        lod_collection = bpy.data.collections.new(
            lod_name
        )
        lod_root.children.link(lod_collection)

        move_object_to_collection(
            obj,
            lod_collection,
        )
        obj.name = f"Character LOD {lod_index}"

        if lod_index > 0:
            lod_collection.hide_viewport = True
            lod_collection.hide_render = True

        # Decode ToonCar's rigid per-bone vertex partitions into Blender
        # vertex groups. Every listed source vertex receives weight 1.0.
        for bone_index_text, indices in (
            lod_info.get("bone_vertex_groups") or {}
        ).items():
            bone_index = int(bone_index_text)
            group = obj.vertex_groups.new(
                name=f"Bone_{bone_index:02d}"
            )

            valid_indices = [
                int(v)
                for v in indices
                if 0 <= int(v) < len(obj.data.vertices)
            ]

            if valid_indices:
                group.add(
                    valid_indices,
                    1.0,
                    'REPLACE',
                )

        modifier = obj.modifiers.new(
            name="ToonCar Armature",
            type='ARMATURE',
        )
        modifier.object = armature_obj

        obj["tooncar_character_lod"] = lod_index
        obj["tooncar_character_vertex_count"] = int(
            lod_info.get("vertex_count", 0)
        )

    scene["tooncar_character_bone_count"] = len(nodes)
    scene["tooncar_character_default_lod"] = 0
    return root_collection



def main():
    args = args_after_double_dash()

    if len(args) < 2:
        raise RuntimeError(
            "Expected: -- <manifest.json> <output.blend> "
            "[unlit] [hide_grid] [preview_environment]"
        )

    manifest_path = Path(args[0]).resolve()
    output_blend = Path(args[1]).resolve()
    unlit = len(args) <= 2 or args[2] not in ("0", "false", "False")
    hide_grid = len(args) <= 3 or args[3] not in ("0", "false", "False")
    preview_environment = (
        len(args) <= 4
        or args[4] not in ("0", "false", "False")
    )
    material_profile = (
        args[5].strip().lower()
        if len(args) > 5
        else "game"
    )
    if material_profile not in ("game", "realistic", "gltf"):
        material_profile = "game"

    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    root = manifest_path.parent

    asset = manifest["asset"]
    obj_path = root / asset["obj"]

    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)

    imported = import_obj(obj_path)

    scene = bpy.context.scene
    try:
        scene.view_settings.view_transform = 'Standard'
    except Exception:
        pass

    scene["tooncar_unpacker_version"] = manifest.get("version", 102)
    scene["tooncar_asset_type"] = manifest.get(
        "asset_type",
        "standalone_object",
    )
    scene["tooncar_source_file"] = manifest["source"]["filename"]
    scene["tooncar_export_scale"] = manifest.get("export_scale", 0.1)

    configured_materials = configure_standalone_materials(
        imported,
        root,
        asset,
        unlit=(
            unlit
            and material_profile != "gltf"
        ),
        gltf_friendly=(
            material_profile == "gltf"
        ),
    )

    if not configured_materials:
        raise RuntimeError(
            "Nie udało się przypisać żadnej tekstury do materiałów "
            "Standalone ObjectMesh."
        )

    set_material_preview_for_saved_file(
        use_environment=preview_environment,
        hide_grid_axes=hide_grid,
    )

    scene["tooncar_color_view_transform"] = "Standard"
    scene["tooncar_material_preview"] = True
    scene["tooncar_material_preview_scene_lights"] = False
    scene["tooncar_material_preview_environment"] = preview_environment
    scene["tooncar_clip_end"] = 50000.0
    scene["tooncar_config_unlit_materials"] = bool(
        unlit
        and material_profile != "gltf"
    )
    scene["tooncar_material_profile"] = material_profile
    scene["tooncar_gltf_ready"] = bool(
        material_profile == "gltf"
    )
    scene["tooncar_gltf_target_runtime"] = (
        "Three.js GLTFLoader"
        if material_profile == "gltf"
        else ""
    )
    scene["tooncar_config_hide_grid_axes"] = hide_grid
    scene["tooncar_configured_material_count"] = len(
        configured_materials
    )

    asset_type = manifest.get(
        "asset_type",
        "standalone_object",
    )

    if asset_type == "car":
        primary_collection = assemble_car(
            imported,
            scene,
            manifest,
        )
    elif asset_type in (
        "character",
        "rigged_object",
    ):
        primary_collection = assemble_character(
            imported,
            scene,
            manifest,
        )
    else:
        asset_name = Path(
            manifest["source"]["filename"]
        ).stem

        primary_collection = bpy.data.collections.new(
            asset_name
        )
        scene.collection.children.link(
            primary_collection
        )

        for obj in imported:
            move_object_to_collection(
                obj,
                primary_collection,
            )

    for coll in list(bpy.data.collections):
        if coll == primary_collection:
            continue
        if (
            len(coll.objects) == 0
            and len(coll.children) == 0
        ):
            try:
                bpy.data.collections.remove(coll)
            except Exception:
                pass

    framed_viewports = frame_visible_model_in_viewports(
        scene
    )
    scene["tooncar_asset_framed_viewports"] = framed_viewports
    scene["tooncar_asset_default_view"] = "Frame Selected"

    collapse_all_outliners()

    output_blend.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    bpy.ops.wm.save_as_mainfile(
        filepath=str(output_blend)
    )
    print(f"TOONCAR_BLEND_SAVED={output_blend}")


if __name__ == "__main__":
    main()
"""
    path.write_text(code, encoding="utf-8")


def build_standalone_object_blend_file(
    unpacked_dir,
    blender_executable=None,
    output_blend=None,
    log=print,
    unlit_materials=True,
    hide_viewport_grid_axes=True,
    material_preview_environment=True,
    material_profile="game",
):
    unpacked_dir = Path(unpacked_dir).resolve()
    manifest_path = unpacked_dir / "manifest.json"

    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"Brak manifest.json: {manifest_path}"
        )

    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )

    if manifest.get("asset_type") not in (
        "standalone_object",
        "simple_metadata_object",
        "rigged_object",
        "car",
        "character",
    ):
        raise ValueError(
            "Manifest nie opisuje obsługiwanego assetu ObjectMesh."
        )

    blender = (
        Path(blender_executable).resolve()
        if blender_executable
        else find_blender_executable()
    )

    if not blender or not blender.is_file():
        raise FileNotFoundError(
            "Nie znaleziono blender.exe. Wskaż Blender w GUI."
        )

    if output_blend is None:
        source_name = manifest["source"]["filename"]
        output_blend = (
            unpacked_dir
            / f"{Path(source_name).stem}.blend"
        )
    else:
        output_blend = Path(output_blend).resolve()

    metadata_dir = unpacked_dir / "metadata"
    metadata_dir.mkdir(exist_ok=True)
    builder = metadata_dir / "_build_standalone_blend.py"

    write_standalone_blender_builder_script(builder)

    log("")
    log("Uruchamiam Blendera dla assetu…")
    log(f"Blender: {blender}")

    command = [
        str(blender),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(builder),
        "--",
        str(manifest_path),
        str(output_blend),
        "1" if unlit_materials else "0",
        "1" if hide_viewport_grid_axes else "0",
        "1" if material_preview_environment else "0",
        str(material_profile).strip().lower(),
    ]

    proc = subprocess.run(
        command,
        cwd=str(unpacked_dir),
        capture_output=True,
        text=True,
    )

    blender_log = unpacked_dir / "blender_build.log"
    blender_log.write_text(
        (proc.stdout or "")
        + ("\n--- STDERR ---\n" if proc.stderr else "")
        + (proc.stderr or ""),
        encoding="utf-8",
        errors="replace",
    )

    if proc.returncode != 0:
        tail = "\n".join(
            ((proc.stderr or proc.stdout or "").splitlines())[-15:]
        )
        raise RuntimeError(
            "Blender nie wygenerował pliku .blend.\n"
            f"Szczegóły: {blender_log}\n\n{tail}"
        )

    if not output_blend.is_file():
        raise RuntimeError(
            "Blender zakończył się bez błędu, "
            "ale plik .blend nie powstał."
        )

    log(f"Plik Blender: {output_blend}")
    return output_blend



def build_blend_file(
    unpacked_dir,
    blender_executable=None,
    output_blend=None,
    log=print,
    sky_mode="environment",
    hide_destroyed_props=True,
    hide_raw_asset_meshes=True,
    generate_sun=False,
    material_preview_environment=True,
    bright_material_preview=True,
    hide_viewport_grid_axes=True,
    unlit_materials=True,
    animate_textures=True,
    include_animated_prop_geometry=True,
    include_ai_path=False,
    include_item_boxes=True,
    use_sorpresa_asset=True,
    animate_sorpresa=True,
    sorpresa_size_multiplier=0.65,
    use_r3d_texture_timing=True,
    texture_animation_interval=0.20,
    include_cones=True,
    show_cone_paths=True,
    use_cone_models=True,
    add_isometric_camera=True,
    material_profile="game",
):
    unpacked_dir = Path(unpacked_dir).resolve()
    manifest_path = unpacked_dir / "manifest.json"

    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Brak manifest.json: {manifest_path}"
        )

    blender = (
        Path(blender_executable).resolve()
        if blender_executable
        else find_blender_executable()
    )

    if not blender or not blender.is_file():
        raise FileNotFoundError(
            "Nie znaleziono blender.exe. Wskaż Blender w GUI."
        )

    if output_blend is None:
        source_name = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )["source"]["filename"]
        output_blend = unpacked_dir / f"{Path(source_name).stem}.blend"
    else:
        output_blend = Path(output_blend).resolve()

    sky_mode = str(sky_mode).strip().lower()
    if sky_mode not in ("environment", "tooncar"):
        raise ValueError(
            "sky_mode musi być 'environment' albo 'tooncar'"
        )

    material_profile = str(material_profile).strip().lower()
    if material_profile not in ("game", "realistic", "gltf"):
        material_profile = "game"

    if material_profile == "gltf":
        manifest_for_gltf = json.loads(
            manifest_path.read_text(
                encoding="utf-8"
            )
        )
        prepare_gltf_runtime_assets(
            unpacked_dir,
            manifest_for_gltf,
            log=log,
        )

    builder = unpacked_dir / "metadata" / "_build_blend.py"
    write_blender_builder_script(builder)

    log("")
    log("Uruchamiam Blendera w tle…")
    log(f"Blender: {blender}")

    command = [
        str(blender),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(builder),
        "--",
        str(manifest_path),
        str(output_blend),
        sky_mode,
        "1" if hide_destroyed_props else "0",
        "1" if hide_raw_asset_meshes else "0",
        "1" if generate_sun else "0",
        "1" if material_preview_environment else "0",
        "1" if bright_material_preview else "0",
        "1" if hide_viewport_grid_axes else "0",
        "1" if unlit_materials else "0",
        "1" if animate_textures else "0",
        "1" if include_animated_prop_geometry else "0",
        "1" if include_ai_path else "0",
        "1" if include_item_boxes else "0",
        "1" if use_sorpresa_asset else "0",
        "1" if animate_sorpresa else "0",
        str(float(sorpresa_size_multiplier)),
        "1" if use_r3d_texture_timing else "0",
        str(float(texture_animation_interval)),
        "1" if include_cones else "0",
        "1" if use_cone_models else "0",
        "1" if show_cone_paths else "0",
        "1" if add_isometric_camera else "0",
        material_profile,
    ]

    proc = subprocess.run(
        command,
        cwd=str(unpacked_dir),
        capture_output=True,
        text=True,
    )

    # Preserve Blender output for debugging without flooding the GUI.
    blender_log = unpacked_dir / "blender_build.log"
    blender_log.write_text(
        (proc.stdout or "")
        + ("\n--- STDERR ---\n" if proc.stderr else "")
        + (proc.stderr or ""),
        encoding="utf-8",
        errors="replace",
    )

    if proc.returncode != 0:
        tail = "\n".join(
            ((proc.stderr or proc.stdout or "").splitlines())[-15:]
        )
        raise RuntimeError(
            "Blender nie wygenerował pliku .blend.\n"
            f"Szczegóły: {blender_log}\n\n{tail}"
        )

    if not output_blend.exists():
        raise RuntimeError(
            "Blender zakończył się bez błędu, ale plik .blend nie powstał."
        )

    log(f"Plik Blender: {output_blend}")
    return output_blend


def run_cli():
    ap = argparse.ArgumentParser(
        description="ToonCar R3D Code-Guided Unpacker v102"
    )
    ap.add_argument("file")
    ap.add_argument("--out")
    ap.add_argument("--scale", type=float, default=0.1)
    ap.add_argument("--blender", help="Ścieżka do blender.exe")
    ap.add_argument(
        "--no-blend",
        action="store_true",
        help="Tylko rozpakuj, bez generowania .blend",
    )
    ap.add_argument(
        "--sky-mode",
        choices=("tooncar", "environment"),
        default="environment",
        help="Sposób wyświetlania nieba w .blend",
    )
    ap.add_argument("--show-destroyed-props", action="store_true")
    ap.add_argument("--show-raw-assets", action="store_true")
    ap.add_argument(
        "--raw-data",
        action="store_true",
        help=(
            "Eksportuj surowe dane diagnostyczne "
            "(.bgra, .raw.bin, unknown_raw)."
        ),
    )
    ap.add_argument("--sun", action="store_true")
    ap.add_argument("--no-preview-environment", action="store_true")
    ap.add_argument("--no-bright-preview", action="store_true")
    ap.add_argument("--show-grid-axes", action="store_true")
    ap.add_argument("--lit-materials", action="store_true")
    ap.add_argument("--no-texture-animation", action="store_true")
    ap.add_argument("--no-animated-prop-geometry", action="store_true")
    ap.add_argument(
        "--ai-path",
        action="store_true",
        help="Dodaj trasę AI do pliku .blend.",
    )
    ap.add_argument(
        "--no-cones",
        action="store_true",
        help="Nie pokazuj pachołków ani placeholderów.",
    )
    ap.add_argument(
        "--no-cone-paths",
        action="store_true",
        help="Nie pokazuj ścieżek ruchomych pachołków.",
    )
    ap.add_argument(
        "--cone-placeholders",
        action="store_true",
        help=(
            "Użyj Empty typu Cone zamiast "
            "wbudowanych modeli Cono/ConoPatas."
        ),
    )
    ap.add_argument(
        "--no-item-boxes",
        action="store_true",
        help="Nie dodawaj placeholderów Sorpresa do pliku .blend",
    )
    ap.add_argument(
        "--empty-item-boxes",
        action="store_true",
        help="Użyj Cube Empty zamiast modelu Sorpresa.r3d",
    )
    ap.add_argument(
        "--no-sorpresa-animation",
        action="store_true",
        help="Nie animuj skrzynek Sorpresa jak w grze",
    )
    ap.add_argument(
        "--sorpresa-size",
        type=float,
        default=0.65,
        help="Dodatkowy mnożnik rozmiaru modelu Sorpresa.r3d",
    )
    ap.add_argument(
        "--material-profile",
        choices=("game", "realistic", "gltf"),
        default="game",
        help="Preset materiałów w wygenerowanym .blend.",
    )
    ap.add_argument(
        "--no-isometric-camera",
        action="store_true",
        help=(
            "Nie dodawaj ortograficznej kamery mapy z lotu ptaka."
        ),
    )
    ap.add_argument(
        "--texture-animation-interval",
        type=float,
        default=None,
        help=(
            "Nadpisz interwał między klatkami animowanych tekstur w sekundach. "
            "Bez tej opcji emulowany jest oryginalny algorytm ToonCar 55 Hz."
        ),
    )
    args = ap.parse_args()

    detected = detect_r3d_asset_type(args.file)
    asset_type = detected.get("type")

    if asset_type == "track":
        result = unpack_r3d(
            args.file,
            args.out,
            args.scale,
            export_raw_data=args.raw_data,
        )
    elif asset_type == "car":
        result = export_car_r3d(
            args.file,
            args.out,
            args.scale,
            texture_variant_index=0,
            export_raw_data=args.raw_data,
        )
    elif asset_type == "character":
        result = export_character_r3d(
            args.file,
            args.out,
            args.scale,
            export_raw_data=args.raw_data,
        )
    elif asset_type == "rigged_object":
        result = export_rigged_object_r3d(
            args.file,
            args.out,
            args.scale,
            export_raw_data=args.raw_data,
        )
    elif asset_type == "simple_metadata_object":
        result = export_simple_metadata_object_r3d(
            args.file,
            args.out,
            args.scale,
            export_raw_data=args.raw_data,
        )
    elif asset_type == "standalone_object":
        result = unpack_standalone_object_r3d(
            args.file,
            args.out,
            args.scale,
            export_raw_data=args.raw_data,
        )
    else:
        raise ValueError(
            "Brak eksportera dla wykrytego typu R3D: "
            f"{detected.get('label', asset_type)}"
        )

    if not args.no_blend:
        if asset_type == "track":
            build_blend_file(
                result,
                blender_executable=args.blender,
                sky_mode=args.sky_mode,
                hide_destroyed_props=not args.show_destroyed_props,
                hide_raw_asset_meshes=not args.show_raw_assets,
                generate_sun=args.sun,
                material_preview_environment=not args.no_preview_environment,
                bright_material_preview=not args.no_bright_preview,
                hide_viewport_grid_axes=not args.show_grid_axes,
                unlit_materials=not args.lit_materials,
                animate_textures=not args.no_texture_animation,
                include_animated_prop_geometry=not args.no_animated_prop_geometry,
                include_ai_path=args.ai_path,
                include_item_boxes=not args.no_item_boxes,
                include_cones=not args.no_cones,
                show_cone_paths=not args.no_cone_paths,
                use_cone_models=not args.cone_placeholders,
                use_sorpresa_asset=not args.empty_item_boxes,
                animate_sorpresa=not args.no_sorpresa_animation,
                sorpresa_size_multiplier=args.sorpresa_size,
                use_r3d_texture_timing=(
                    args.texture_animation_interval is None
                ),
                texture_animation_interval=(
                    args.texture_animation_interval
                    if args.texture_animation_interval is not None
                    else 0.20
                ),
                add_isometric_camera=(
                    not args.no_isometric_camera
                ),
                material_profile=args.material_profile,
            )
        else:
            build_standalone_object_blend_file(
                result,
                blender_executable=args.blender,
                unlit_materials=not args.lit_materials,
                hide_viewport_grid_axes=not args.show_grid_axes,
                material_preview_environment=(
                    not args.no_preview_environment
                ),
                material_profile=args.material_profile,
            )




def car_texture_preview_rgb(data: bytes, entry):
    """
    Return a useful GUI color swatch for a car texture variant.

    ToonCar car atlases contain body paint plus windows, tires, highlights and
    shadows, so averaging the atlas does NOT reliably represent the named car
    color (e.g. blanco could look cream/brown). For the known Spanish variant
    names use a canonical display swatch. Unknown names still fall back to
    pixel analysis.
    """
    variant_name = Path(entry["name"]).stem.lower()

    canonical = {
        "amarillo": (255, 210, 0),
        "azul": (45, 105, 220),
        "blanco": (245, 245, 245),
        "negro": (32, 32, 32),
        "rojo": (220, 45, 45),
        "rosa": (240, 115, 190),
        "verde": (55, 165, 75),
        "violeta": (145, 75, 190),
    }

    if variant_name in canonical:
        return canonical[variant_name]

    start = entry["pixel_offset"]
    end = start + entry["pixel_size"]
    raw = data[start:end]

    if len(raw) < 4:
        return (128, 128, 128)

    saturated = []
    all_rgb = []

    # Subsample large atlases for fast GUI detection.
    pixel_count = len(raw) // 4
    step = max(1, pixel_count // 4096)

    for i in range(0, pixel_count, step):
        off = i * 4
        b = raw[off]
        g = raw[off + 1]
        r = raw[off + 2]

        all_rgb.append((r, g, b))

        mx = max(r, g, b)
        mn = min(r, g, b)
        if mx - mn >= 35 and mx >= 55:
            saturated.append((r, g, b))

    samples = saturated if saturated else all_rgb
    if not samples:
        return (128, 128, 128)

    # Median-ish robust center: average after dropping darkest/brightest 10%.
    samples = sorted(
        samples,
        key=lambda rgb: sum(rgb),
    )
    cut = len(samples) // 10
    if len(samples) > 20:
        samples = samples[cut:len(samples)-cut]

    r = round(sum(x[0] for x in samples) / len(samples))
    g = round(sum(x[1] for x in samples) / len(samples))
    b = round(sum(x[2] for x in samples) / len(samples))
    return (r, g, b)



def detect_r3d_asset_type(path):
    """
    Lightweight top-level classifier used by the GUI.

    It does not rely on filenames. It validates known binary layouts in
    descending confidence:
      - Track
      - compact standalone ObjectMesh asset (e.g. Sorpresa.r3d)
      - Car family header (texture bank at 0x23C)
      - Character family header (texture bank at 0x1C8)
      - generic/unknown R3D with recognizable ToonCar substructures
    """
    path = Path(path).resolve()

    result = {
        "type": "unknown",
        "label": "Nieznany R3D",
        "supported": False,
        "confidence": 0.0,
        "details": "",
    }

    if not path.is_file() or path.suffix.lower() != ".r3d":
        return result

    try:
        data = path.read_bytes()
    except Exception as exc:
        result["details"] = f"Nie udało się odczytać pliku: {exc}"
        return result

    # 1) Track: strongest signature because the complete verified top-level
    # prefix must parse through material/animation tables and StaticMesh.
    try:
        top = parse_code_guided_top_level(data)
    except Exception:
        top = None

    if top:
        mm = top["main_mesh"]

        return {
            "type": "track",
            "label": "Mapa / Map",
            "supported": True,
            "confidence": 1.0,
            "details": (
                f"StaticMesh: {mm['vertex_count']} vertexów, "
                f"{mm['face_count']} trójkątów"
            ),
        }

    car_layout = parse_car_r3d_layout(data)
    if car_layout:
        car_bank = car_layout["texture_bank"]
        object_meshes = car_layout["object_meshes"]

        return {
            "type": "car",
            "label": "Car",
            "supported": True,
            "confidence": 1.0,
            "details": (
                f"{len(object_meshes)} części ObjectMesh; "
                f"{car_bank['count']} wariantów tekstury"
            ),
            "texture_variants": [
                {
                    "index": int(entry["index"]),
                    "name": entry["name"],
                    "label": Path(entry["name"]).stem,
                    "preview_rgb": list(
                        car_texture_preview_rgb(
                            data,
                            entry,
                        )
                    ),
                }
                for entry in car_bank["entries"]
            ],
        }

    # Chatty-style characters have their embedded texture bank at 0x1C8.
    character_layout = parse_character_r3d_layout(data)
    if character_layout:
        return {
            "type": "character",
            "label": "Character",
            "supported": True,
            "confidence": 1.0,
            "details": (
                f"{len(character_layout['lods'])} LOD-y; "
                f"{len(character_layout['skeleton']['nodes'])} kości/node'ów"
            ),
        }

    # 4) Generic recognizable ToonCar asset. This is intentionally broad and
    # only used to present the right UI state, never to silently choose a
    # destructive parser.
    banks = find_all_texture_banks(data)
    object_meshes = find_all_object_meshes(data)

    if banks or object_meshes:
        return {
            "type": "generic_asset",
            "label": "Inny asset R3D",
            "supported": False,
            "confidence": 0.60,
            "details": (
                f"banki tekstur: {len(banks)}, "
                f"ObjectMesh: {len(object_meshes)}"
            ),
        }

    return result




def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog
    import threading

    root = tk.Tk()
    root.title("ToonCar R3D → Blender v102")
    # Initial width only; final height is calculated from the actual
    # requested size of all widgets after the GUI is constructed.
    root.geometry("1180x700")
    root.minsize(1000, 600)

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    file_var = tk.StringVar()
    out_var = tk.StringVar()
    scale_var = tk.StringVar(value="0.1")

    detected_blender = find_blender_executable()
    blender_var = tk.StringVar(
        value=str(detected_blender) if detected_blender else ""
    )

    make_blend_var = tk.BooleanVar(value=True)
    export_raw_data_var = tk.BooleanVar(value=False)
    view_material_preset_var = tk.StringVar(value="game")
    sky_mode_var = tk.StringVar(value="environment")
    hide_destroyed_props_var = tk.BooleanVar(value=True)
    hide_raw_assets_var = tk.BooleanVar(value=True)
    generate_sun_var = tk.BooleanVar(value=False)
    material_preview_environment_var = tk.BooleanVar(value=True)
    bright_material_preview_var = tk.BooleanVar(value=True)
    hide_viewport_grid_axes_var = tk.BooleanVar(value=True)
    unlit_materials_var = tk.BooleanVar(value=True)
    animate_textures_var = tk.BooleanVar(value=True)
    include_animated_prop_geometry_var = tk.BooleanVar(value=True)
    include_ai_path_var = tk.BooleanVar(value=False)
    include_item_boxes_var = tk.BooleanVar(value=True)
    include_cones_var = tk.BooleanVar(value=False)
    add_isometric_camera_var = tk.BooleanVar(value=True)
    show_cone_paths_var = tk.BooleanVar(value=True)
    use_cone_models_var = tk.BooleanVar(value=True)
    use_sorpresa_asset_var = tk.BooleanVar(value=True)
    animate_sorpresa_var = tk.BooleanVar(value=True)
    sorpresa_size_multiplier_var = tk.DoubleVar(value=0.65)
    sorpresa_size_label_var = tk.StringVar(value="65%")
    use_r3d_texture_timing_var = tk.BooleanVar(value=True)
    texture_animation_interval_var = tk.DoubleVar(value=0.20)
    texture_animation_interval_label_var = tk.StringVar(value="200 ms")
    auto_open_blend_var = tk.BooleanVar(value=True)
    car_texture_variant_var = tk.StringVar(value="")

    def apply_view_material_preset(*_):
        preset = view_material_preset_var.get()

        if preset == "realistic":
            material_preview_environment_var.set(True)
            bright_material_preview_var.set(False)
            unlit_materials_var.set(False)
            generate_sun_var.set(True)
        elif preset == "gltf":
            material_preview_environment_var.set(True)
            bright_material_preview_var.set(True)
            unlit_materials_var.set(False)
            generate_sun_var.set(False)
            # Standard glTF does not carry Blender Image Sequence playback.
            # Keep texture-animation metadata for Three.js runtime instead.
            animate_textures_var.set(False)
        else:
            material_preview_environment_var.set(True)
            bright_material_preview_var.set(True)
            unlit_materials_var.set(True)
            generate_sun_var.set(False)
            animate_textures_var.set(True)

    status_var = tk.StringVar(value="Wybierz plik .r3d")
    detected_type_var = tk.StringVar(value="Typ: —")
    detected_details_var = tk.StringVar(
        value="Po wybraniu pliku typ R3D zostanie wykryty automatycznie."
    )
    detected_support_var = tk.StringVar(value="")
    detected_asset_state = {"info": None}
    latest_blend_path = {"path": None}

    # Expensive map metadata is cached by file identity.
    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=3)
    frame.columnconfigure(1, weight=2)
    frame.rowconfigure(1, weight=1)

    header = ttk.Frame(frame)
    header.grid(
        row=0,
        column=0,
        columnspan=2,
        sticky="ew",
    )

    ttk.Label(
        header,
        text="ToonCar R3D → Blender v102",
        font=("Segoe UI", 15, "bold"),
    ).pack(anchor="w")

    ttk.Label(
        header,
        text="Rozpakowuje R3D i automatycznie generuje gotowy plik .blend.",
    ).pack(anchor="w", pady=(2, 14))

    form = ttk.Frame(frame)
    form.grid(
        row=1,
        column=0,
        sticky="nsew",
        padx=(0, 8),
    )
    form.columnconfigure(1, weight=1)

    ttk.Label(form, text="Plik R3D:").grid(
        row=0, column=0, sticky="w", pady=5
    )
    ttk.Entry(form, textvariable=file_var).grid(
        row=0, column=1, sticky="ew", padx=(10, 8), pady=5
    )

    def choose_file():
        p = filedialog.askopenfilename(
            title="Wybierz R3D",
            filetypes=[
                ("ToonCar R3D", "*.r3d"),
                ("Wszystkie", "*.*"),
            ],
        )
        if p:
            q = Path(p)
            file_var.set(str(q))
            out_var.set(str(q.parent / f"{q.stem}_unpacked_v102"))

    ttk.Button(
        form,
        text="Wybierz…",
        command=choose_file,
        width=12,
    ).grid(row=0, column=2, pady=5)

    ttk.Label(form, text="Folder wyjściowy:").grid(
        row=1, column=0, sticky="w", pady=5
    )
    ttk.Entry(form, textvariable=out_var).grid(
        row=1, column=1, sticky="ew", padx=(10, 8), pady=5
    )

    def choose_out():
        p = filedialog.askdirectory(title="Folder wyjściowy")
        if p:
            out_var.set(p)

    ttk.Button(
        form,
        text="Wybierz…",
        command=choose_out,
        width=12,
    ).grid(row=1, column=2, pady=5)

    ttk.Label(form, text="Blender:").grid(
        row=2, column=0, sticky="w", pady=5
    )
    ttk.Entry(form, textvariable=blender_var).grid(
        row=2, column=1, sticky="ew", padx=(10, 8), pady=5
    )

    def choose_blender():
        p = filedialog.askopenfilename(
            title="Wybierz blender.exe",
            filetypes=[
                ("Blender", "blender.exe"),
                ("Program EXE", "*.exe"),
                ("Wszystkie", "*.*"),
            ],
        )
        if p:
            blender_var.set(p)

    ttk.Button(
        form,
        text="Wybierz…",
        command=choose_blender,
        width=12,
    ).grid(row=2, column=2, pady=5)

    ttk.Label(form, text="Skala:").grid(
        row=3, column=0, sticky="w", pady=5
    )
    ttk.Entry(
        form,
        textvariable=scale_var,
        width=10,
    ).grid(
        row=3, column=1, sticky="w", padx=(10, 8), pady=5
    )

    detection_frame = ttk.LabelFrame(
        form,
        text="Wykryty typ importu",
        padding=(10, 7),
    )
    detection_frame.grid(
        row=4,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(8, 2),
    )
    detection_frame.columnconfigure(0, weight=1)

    ttk.Label(
        detection_frame,
        textvariable=detected_type_var,
        font=("Segoe UI", 10, "bold"),
    ).grid(
        row=0,
        column=0,
        sticky="w",
    )

    ttk.Label(
        detection_frame,
        textvariable=detected_details_var,
    ).grid(
        row=1,
        column=0,
        sticky="w",
        pady=(2, 0),
    )

    ttk.Label(
        detection_frame,
        textvariable=detected_support_var,
    ).grid(
        row=2,
        column=0,
        sticky="w",
        pady=(2, 0),
    )

    options = ttk.LabelFrame(form, text="Opcje importu", padding=(10, 8))
    options.grid(
        row=5,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(10, 5),
    )
    options.columnconfigure(0, weight=1)
    options.columnconfigure(1, weight=1)

    # ---------------------------------------------------------------
    # Blender / wynik
    # ---------------------------------------------------------------
    output_options = ttk.LabelFrame(
        options,
        text="Blender / wynik",
        padding=(8, 6),
    )
    output_options.grid(
        row=0,
        column=0,
        sticky="nsew",
        padx=(0, 4),
        pady=(0, 5),
    )

    ttk.Checkbutton(
        output_options,
        text="Generuj plik .blend",
        variable=make_blend_var,
    ).pack(anchor="w", pady=1)

    ttk.Checkbutton(
        output_options,
        text="Po konwersji otwórz .blend",
        variable=auto_open_blend_var,
    ).pack(anchor="w", pady=1)

    ttk.Checkbutton(
        output_options,
        text="Eksportuj surowe dane diagnostyczne",
        variable=export_raw_data_var,
    ).pack(anchor="w", pady=1)

    # ---------------------------------------------------------------
    # Asset R3D (non-track)
    # ---------------------------------------------------------------
    asset_options = ttk.LabelFrame(
        options,
        text="Asset R3D",
        padding=(8, 6),
    )
    asset_options.grid(
        row=0,
        column=1,
        sticky="nsew",
        padx=(4, 0),
        pady=(0, 5),
    )

    asset_options_message_var = tk.StringVar(
        value="Parser tego wariantu będzie dodany w kolejnych etapach."
    )

    ttk.Label(
        asset_options,
        textvariable=asset_options_message_var,
        wraplength=360,
        justify="left",
    ).pack(anchor="w")

    car_options_frame = ttk.Frame(asset_options)

    ttk.Label(
        car_options_frame,
        text="Wariant tekstury / kolor:",
    ).pack(anchor="w")

    car_variant_row = ttk.Frame(car_options_frame)
    car_variant_row.pack(
        fill="x",
        anchor="w",
        pady=(3, 0),
    )
    car_variant_row.columnconfigure(0, weight=1)

    car_texture_combo = ttk.Combobox(
        car_variant_row,
        textvariable=car_texture_variant_var,
        state="readonly",
    )
    car_texture_combo.grid(
        row=0,
        column=0,
        sticky="ew",
        padx=(0, 8),
    )

    car_color_swatch = tk.Label(
        car_variant_row,
        width=6,
        relief="solid",
        borderwidth=1,
        background="#808080",
    )
    car_color_swatch.grid(
        row=0,
        column=1,
        sticky="ns",
    )

    asset_options.grid_remove()

    # ---------------------------------------------------------------
    # Skybox
    # ---------------------------------------------------------------
    sky_options = ttk.LabelFrame(
        options,
        text="Skybox",
        padding=(8, 6),
    )
    sky_options.grid(
        row=0,
        column=1,
        sticky="nsew",
        padx=(4, 0),
        pady=(0, 5),
    )

    ttk.Radiobutton(
        sky_options,
        text="Blender Environment",
        variable=sky_mode_var,
        value="environment",
    ).pack(anchor="w", pady=1)

    ttk.Radiobutton(
        sky_options,
        text="ToonCar Skybox — 6 ścian / tryb gry",
        variable=sky_mode_var,
        value="tooncar",
    ).pack(anchor="w", pady=1)

    # ---------------------------------------------------------------
    # Widok i materiały
    # ---------------------------------------------------------------
    view_options = ttk.LabelFrame(
        options,
        text="Widok i materiały",
        padding=(8, 6),
    )
    view_options.grid(
        row=1,
        column=0,
        sticky="nsew",
        padx=(0, 4),
        pady=5,
    )

    preset_row = ttk.Frame(view_options)
    preset_row.pack(
        fill="x",
        anchor="w",
        pady=(0, 5),
    )

    ttk.Radiobutton(
        preset_row,
        text="Gra / ToonCar",
        variable=view_material_preset_var,
        value="game",
    ).pack(
        side="left",
        padx=(0, 12),
    )

    ttk.Radiobutton(
        preset_row,
        text="Realistyczne",
        variable=view_material_preset_var,
        value="realistic",
    ).pack(
        side="left",
        padx=(0, 12),
    )

    ttk.Radiobutton(
        preset_row,
        text="glTF / Three.js",
        variable=view_material_preset_var,
        value="gltf",
    ).pack(
        side="left",
    )

    ttk.Separator(
        view_options,
        orient="horizontal",
    ).pack(
        fill="x",
        pady=(0, 5),
    )

    ttk.Checkbutton(
        view_options,
        text="Material Preview: pokaż Environment",
        variable=material_preview_environment_var,
    ).pack(anchor="w", pady=1)

    ttk.Checkbutton(
        view_options,
        text="Jasny Material Preview (bez cieni)",
        variable=bright_material_preview_var,
    ).pack(anchor="w", pady=1)

    ttk.Checkbutton(
        view_options,
        text="Materiały bez cieniowania (Emission)",
        variable=unlit_materials_var,
    ).pack(anchor="w", pady=1)

    ttk.Checkbutton(
        view_options,
        text="Dodaj Sun do Rendered View",
        variable=generate_sun_var,
    ).pack(anchor="w", pady=1)

    ttk.Checkbutton(
        view_options,
        text="Ukryj grid i osie w Blenderze",
        variable=hide_viewport_grid_axes_var,
    ).pack(anchor="w", pady=1)


    ttk.Label(
        view_options,
        text=(
            "glTF / Three.js: prosty Principled BSDF, alpha z tekstury, "
            "Metallic 0, Roughness 1; animowane tekstury są zostawiane "
            "jako metadata do obsługi runtime w Three.js."
        ),
        wraplength=520,
        justify="left",
    ).pack(anchor="w", pady=(4, 0))

    # ---------------------------------------------------------------
    # Obiekty sceny
    # ---------------------------------------------------------------
    scene_options = ttk.LabelFrame(
        options,
        text="Obiekty sceny",
        padding=(8, 6),
    )
    scene_options.grid(
        row=1,
        column=1,
        sticky="nsew",
        padx=(4, 0),
        pady=5,
    )

    ttk.Checkbutton(
        scene_options,
        text="Ukryj zniszczone części propów",
        variable=hide_destroyed_props_var,
    ).pack(anchor="w", pady=1)

    ttk.Checkbutton(
        scene_options,
        text="Ukryj diagnostyczne Asset Meshes",
        variable=hide_raw_assets_var,
    ).pack(anchor="w", pady=1)

    # ---------------------------------------------------------------
    # Animacje
    # ---------------------------------------------------------------
    animation_options = ttk.LabelFrame(
        options,
        text="Animacje",
        padding=(8, 6),
    )
    animation_options.grid(
        row=2,
        column=0,
        columnspan=2,
        sticky="ew",
        pady=5,
    )
    animation_options.columnconfigure(0, weight=1)
    animation_options.columnconfigure(1, weight=1)

    ttk.Checkbutton(
        animation_options,
        text="Odtwarzaj animowane tekstury",
        variable=animate_textures_var,
    ).grid(
        row=0,
        column=0,
        sticky="w",
        padx=(0, 12),
        pady=1,
    )

    ttk.Checkbutton(
        animation_options,
        text="Odtwarzaj animowane propy 3D",
        variable=include_animated_prop_geometry_var,
    ).grid(
        row=0,
        column=1,
        sticky="w",
        pady=1,
    )

    speed_frame = ttk.Frame(animation_options)
    speed_frame.grid(
        row=1,
        column=0,
        columnspan=2,
        sticky="ew",
        pady=(5, 0),
    )
    speed_frame.columnconfigure(1, weight=1)

    use_r3d_timing_check = ttk.Checkbutton(
        speed_frame,
        text="Użyj oryginalnego timingu ToonCar (55 Hz)",
        variable=use_r3d_texture_timing_var,
    )
    use_r3d_timing_check.grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="w",
        pady=(0, 4),
    )

    ttk.Label(
        speed_frame,
        text="Ręczny interwał klatki:",
    ).grid(
        row=1,
        column=0,
        sticky="w",
        padx=(0, 8),
    )

    texture_speed_scale = ttk.Scale(
        speed_frame,
        from_=0.02,
        to=0.50,
        variable=texture_animation_interval_var,
        orient="horizontal",
    )
    texture_speed_scale.grid(
        row=1,
        column=1,
        sticky="ew",
        padx=(0, 8),
    )

    texture_interval_value_label = ttk.Label(
        speed_frame,
        textvariable=texture_animation_interval_label_var,
        width=10,
        anchor="e",
    )
    texture_interval_value_label.grid(
        row=1,
        column=2,
        sticky="e",
    )

    # ---------------------------------------------------------------
    # Gameplay
    # ---------------------------------------------------------------
    gameplay_options = ttk.LabelFrame(
        options,
        text="Gameplay",
        padding=(8, 6),
    )
    gameplay_options.grid(
        row=3,
        column=0,
        sticky="nsew",
        padx=(0, 4),
        pady=(5, 0),
    )

    include_ai_path_check = ttk.Checkbutton(
        gameplay_options,
        text="Pokaż trasę AI",
        variable=include_ai_path_var,
    )
    include_ai_path_check.pack(
        anchor="w",
        pady=1,
    )

    include_item_boxes_check = ttk.Checkbutton(
        gameplay_options,
        text="Pokaż skrzynki / placeholdery",
        variable=include_item_boxes_var,
    )
    include_item_boxes_check.pack(
        anchor="w",
        pady=1,
    )

    # ---------------------------------------------------------------
    # Skrzynki / Sorpresa
    # ---------------------------------------------------------------
    sorpresa_options = ttk.LabelFrame(
        options,
        text="Skrzynki / Sorpresa",
        padding=(8, 6),
    )
    sorpresa_options.grid(
        row=3,
        column=1,
        sticky="nsew",
        padx=(4, 0),
        pady=(5, 0),
    )

    use_sorpresa_asset_check = ttk.Checkbutton(
        sorpresa_options,
        text="Podstaw model skrzynki z gry",
        variable=use_sorpresa_asset_var,
    )
    use_sorpresa_asset_check.pack(
        anchor="w",
        pady=1,
    )

    sorpresa_model_details = ttk.Frame(
        sorpresa_options
    )

    animate_sorpresa_check = ttk.Checkbutton(
        sorpresa_model_details,
        text="Animuj skrzynki jak w ToonCar",
        variable=animate_sorpresa_var,
    )
    animate_sorpresa_check.pack(
        anchor="w",
        pady=1,
    )

    sorpresa_scale_frame = ttk.Frame(
        sorpresa_model_details
    )
    sorpresa_scale_frame.pack(
        fill="x",
        anchor="w",
        pady=(5, 0),
    )
    sorpresa_scale_frame.columnconfigure(
        1,
        weight=1,
    )

    ttk.Label(
        sorpresa_scale_frame,
        text="Rozmiar modelu:",
    ).grid(
        row=0,
        column=0,
        sticky="w",
        padx=(0, 8),
    )

    sorpresa_scale_slider = ttk.Scale(
        sorpresa_scale_frame,
        from_=0.25,
        to=1.25,
        variable=sorpresa_size_multiplier_var,
        orient="horizontal",
    )
    sorpresa_scale_slider.grid(
        row=0,
        column=1,
        sticky="ew",
        padx=(0, 8),
    )

    ttk.Label(
        sorpresa_scale_frame,
        textvariable=sorpresa_size_label_var,
        width=6,
        anchor="e",
    ).grid(
        row=0,
        column=2,
        sticky="e",
    )

    def update_sorpresa_size_label(*_):
        value = round(
            sorpresa_size_multiplier_var.get() * 20.0
        ) / 20.0
        sorpresa_size_label_var.set(
            f"{int(round(value * 100.0))}%"
        )

    sorpresa_size_multiplier_var.trace_add(
        "write",
        update_sorpresa_size_label,
    )

    view_material_preset_var.trace_add(
        "write",
        apply_view_material_preset,
    )

    def refresh_sorpresa_option_visibility(*_):
        info = detected_asset_state.get("info") or {}
        is_track = info.get("type") == "track"

        if (
            is_track
            and include_item_boxes_var.get()
        ):
            sorpresa_options.grid()
        else:
            sorpresa_options.grid_remove()

        if (
            is_track
            and include_item_boxes_var.get()
            and use_sorpresa_asset_var.get()
        ):
            if not sorpresa_model_details.winfo_manager():
                sorpresa_model_details.pack(
                    fill="x",
                    anchor="w",
                    pady=(4, 0),
                )
        else:
            sorpresa_model_details.pack_forget()

        try:
            root.after_idle(
                fit_window_to_content
            )
        except Exception:
            pass

    include_item_boxes_var.trace_add(
        "write",
        refresh_sorpresa_option_visibility,
    )
    use_sorpresa_asset_var.trace_add(
        "write",
        refresh_sorpresa_option_visibility,
    )

    # ---------------------------------------------------------------
    # Pachołki / Cones
    # ---------------------------------------------------------------
    cone_options = ttk.LabelFrame(
        options,
        text="Pachołki / Cones",
        padding=(8, 6),
    )
    cone_options.grid(
        row=4,
        column=0,
        columnspan=2,
        sticky="ew",
        pady=(5, 0),
    )
    cone_options.grid_remove()

    include_cones_check = ttk.Checkbutton(
        cone_options,
        text="Pokaż pachołki / placeholdery",
        variable=include_cones_var,
    )
    include_cones_check.pack(
        anchor="w",
        pady=1,
    )

    cone_details = ttk.Frame(
        cone_options
    )

    ttk.Checkbutton(
        cone_details,
        text="Pokaż ścieżki ruchomych pachołków",
        variable=show_cone_paths_var,
    ).pack(
        anchor="w",
        pady=1,
    )

    ttk.Checkbutton(
        cone_details,
        text="Podstaw modele pachołków z gry",
        variable=use_cone_models_var,
    ).pack(
        anchor="w",
        pady=1,
    )

    ttk.Label(
        cone_details,
        text=(
            "Placeholder: Empty typu Cone. "
            "Model z gry zastępuje placeholder w tej samej pozycji."
        ),
        wraplength=720,
        justify="left",
    ).pack(
        anchor="w",
        pady=(3, 0),
    )

    def refresh_cone_option_visibility(*_):
        info = detected_asset_state.get("info") or {}
        is_track = info.get("type") == "track"

        # Show the section immediately for every track. The expensive
        # Conos/ConoPata scan runs in the background and only reports counts
        # to the log.
        if is_track:
            cone_options.grid()

            if include_cones_var.get():
                if not cone_details.winfo_manager():
                    cone_details.pack(
                        fill="x",
                        anchor="w",
                        pady=(4, 0),
                    )
            else:
                cone_details.pack_forget()
        else:
            cone_details.pack_forget()
            cone_options.grid_remove()

        try:
            root.after_idle(
                fit_window_to_content
            )
        except Exception:
            pass

    include_cones_var.trace_add(
        "write",
        refresh_cone_option_visibility,
    )

    # ---------------------------------------------------------------
    # Kamera mapy
    # ---------------------------------------------------------------
    map_camera_options = ttk.LabelFrame(
        options,
        text="Kamera mapy",
        padding=(8, 6),
    )
    map_camera_options.grid(
        row=5,
        column=0,
        columnspan=2,
        sticky="ew",
        pady=(5, 0),
    )

    ttk.Checkbutton(
        map_camera_options,
        text="Dodaj kamerę z lotu ptaka",
        variable=add_isometric_camera_var,
    ).pack(
        anchor="w",
        pady=1,
    )

    ttk.Label(
        map_camera_options,
        text=(
            "Widok centralnie z góry • ortograficzna • stała skala 180 • "
            "render 8192×8192 (1:1), skala rendera 100%. Output rendera: Desktop aktywnego użytkownika. "
            "Stała skala pozwala porównywać wielkość tras."
        ),
        wraplength=720,
        justify="left",
    ).pack(
        anchor="w",
        pady=(3, 0),
    )

    apply_view_material_preset()

    def update_texture_interval_label(*_):
        value = round(texture_animation_interval_var.get() * 100.0) / 100.0
        texture_animation_interval_label_var.set(
            f"{int(round(value * 1000.0))} ms"
        )

    def refresh_texture_timing_controls(*_):
        state = (
            "disabled"
            if use_r3d_texture_timing_var.get()
            else "normal"
        )
        texture_speed_scale.configure(state=state)
        texture_interval_value_label.configure(state=state)

    texture_animation_interval_var.trace_add(
        "write",
        update_texture_interval_label,
    )
    use_r3d_texture_timing_var.trace_add(
        "write",
        refresh_texture_timing_controls,
    )
    refresh_texture_timing_controls()

    if detected_blender:
        blender_hint = f"Wykryto automatycznie: {detected_blender}"
    else:
        blender_hint = "Nie wykryto Blendera automatycznie — wskaż blender.exe."

    ttk.Label(
        form,
        text=blender_hint,
    ).grid(
        row=6,
        column=0,
        columnspan=3,
        sticky="w",
        padx=(0, 8),
        pady=(3, 5),
    )

    log_frame = ttk.LabelFrame(
        frame,
        text="Log",
        padding=(8, 6),
    )
    log_frame.grid(
        row=1,
        column=1,
        sticky="nsew",
        padx=(8, 0),
    )
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    log_box = tk.Text(
        log_frame,
        state="disabled",
        wrap="word",
    )
    log_box.grid(row=0, column=0, sticky="nsew")

    scroll = ttk.Scrollbar(
        log_frame,
        command=log_box.yview,
    )
    scroll.grid(row=0, column=1, sticky="ns")
    log_box.configure(yscrollcommand=scroll.set)

    def log(message):
        def append():
            log_box.configure(state="normal")
            log_box.insert("end", str(message) + "\n")
            log_box.see("end")
            log_box.configure(state="disabled")
        root.after(0, append)

    footer = ttk.Frame(frame)
    footer.grid(
        row=2,
        column=0,
        columnspan=2,
        sticky="ew",
        pady=(12, 0),
    )
    footer.columnconfigure(0, weight=1)

    ttk.Label(
        footer,
        textvariable=status_var,
    ).grid(
        row=0,
        column=0,
        sticky="ew",
        padx=(0, 12),
    )

    def open_out():
        p = Path(out_var.get())
        if p.exists():
            os.startfile(p)
        else:
            log("Folder wyjściowy jeszcze nie istnieje.")

    convert_btn = ttk.Button(
        footer,
        text="Konwertuj",
        width=13,
        state="disabled",
    )
    convert_btn.grid(
        row=0,
        column=1,
        padx=(0, 8),
    )

    track_only_sections = (
        sky_options,
        scene_options,
        animation_options,
        gameplay_options,
        sorpresa_options,
        map_camera_options,
    )

    spanish_color_names = {
        "amarillo": "żółty",
        "azul": "niebieski",
        "blanco": "biały",
        "negro": "czarny",
        "rojo": "czerwony",
        "rosa": "różowy",
        "verde": "zielony",
        "violeta": "fioletowy",
    }

    def car_variant_display_label(variant):
        raw = (
            variant.get("label")
            or variant.get("name")
            or ""
        )
        translated = spanish_color_names.get(
            raw.lower()
        )
        return (
            f"{raw} ({translated})"
            if translated
            else raw
        )

    def refresh_car_color_swatch(*_):
        info = detected_asset_state.get("info") or {}
        variants = info.get("texture_variants") or []
        selected = car_texture_variant_var.get()

        for variant in variants:
            if car_variant_display_label(variant) != selected:
                continue

            rgb = variant.get("preview_rgb") or [128, 128, 128]
            try:
                r, g, b = [max(0, min(255, int(v))) for v in rgb[:3]]
            except Exception:
                r, g, b = 128, 128, 128

            car_color_swatch.configure(
                background=f"#{r:02x}{g:02x}{b:02x}"
            )
            return

        car_color_swatch.configure(
            background="#808080"
        )

    car_texture_variant_var.trace_add(
        "write",
        refresh_car_color_swatch,
    )

    def apply_options_for_asset_type(info):
        asset_type = (info or {}).get("type", "unknown")

        car_options_frame.pack_forget()

        if asset_type == "track":
            asset_options.grid_remove()
            for section in track_only_sections:
                section.grid()
            refresh_sorpresa_option_visibility()
            refresh_cone_option_visibility()
        else:
            for section in track_only_sections:
                section.grid_remove()
            sorpresa_model_details.pack_forget()
            cone_details.pack_forget()
            cone_options.grid_remove()

            asset_options.grid()

            if asset_type == "standalone_object":
                asset_options_message_var.set(
                    "Standalone ObjectMesh: geometria, tekstury i .blend "
                    "zostaną wyeksportowane osobnym pipeline'em assetu."
                )
            elif asset_type == "simple_metadata_object":
                asset_options_message_var.set(
                    "Static Object: prosty ObjectMesh z blokiem metadata. "
                    "Geometria, tekstury i metadata zostaną wyeksportowane."
                )
            elif asset_type == "rigged_object":
                asset_options_message_var.set(
                    "Rigged Object: LOD-y, skinning, szkielet i dane "
                    "animacji zostaną odczytane. LOD 0 będzie widoczny domyślnie."
                )
            elif asset_type == "car":
                asset_options_message_var.set(
                    "Car: wszystkie części ObjectMesh i wybrany "
                    "wariant kolorystyczny zostaną wyeksportowane do .blend."
                )

                variants = info.get("texture_variants") or []
                labels = [
                    car_variant_display_label(variant)
                    for variant in variants
                ]
                car_texture_combo.configure(values=labels)

                if labels:
                    current = car_texture_variant_var.get()
                    if current not in labels:
                        car_texture_variant_var.set(labels[0])

                car_options_frame.pack(
                    fill="x",
                    anchor="w",
                    pady=(8, 0),
                )
            elif asset_type == "character":
                asset_options_message_var.set(
                    "Character: LOD-y, szkielet i grupy vertexów zostaną "
                    "odtworzone jako Blender Armature + Armature Modifier."
                )
            elif asset_type == "generic_asset":
                asset_options_message_var.set(
                    "Plik zawiera rozpoznane struktury ToonCar, ale jego "
                    "top-level layout nie został jeszcze sklasyfikowany."
                )
            else:
                asset_options_message_var.set(
                    "Nie udało się bezpiecznie dobrać parsera dla tego R3D."
                )

        # Refit after sections appear/disappear.
        try:
            root.after_idle(fit_window_to_content)
        except Exception:
            pass

    def refresh_source_detection(*_):
        raw = file_var.get().strip()
        p = Path(raw) if raw else None

        valid_file = bool(
            p
            and p.suffix.lower() == ".r3d"
            and p.is_file()
        )

        if not valid_file:
            detected_asset_state["info"] = None
            detected_type_var.set("Typ: —")
            detected_details_var.set(
                "Po wybraniu pliku typ R3D zostanie wykryty automatycznie."
            )
            detected_support_var.set("")
            apply_options_for_asset_type(None)
            convert_btn.configure(state="disabled")
            return

        info = detect_r3d_asset_type(p)
        detected_asset_state["info"] = info

        detected_type_var.set(
            f"Typ: {info['label']}"
        )
        detected_details_var.set(
            info.get("details")
            or "Brak dodatkowych danych."
        )

        if info.get("supported"):
            detected_support_var.set(
                "Konwerter: gotowy — zostaną użyte opcje dla tego typu."
            )
            convert_btn.configure(state="normal")
            status_var.set(
                f"Gotowy: {p.name}"
            )
        else:
            detected_support_var.set(
                "Konwerter: typ rozpoznany, ale jego eksporter nie jest jeszcze gotowy."
            )
            convert_btn.configure(state="disabled")
            status_var.set(
                f"Wykryto {info['label']} — eksport jeszcze niedostępny"
            )

        apply_options_for_asset_type(info)

    file_var.trace_add("write", refresh_source_detection)

    def open_blend():
        p = latest_blend_path.get("path")
        if p and Path(p).exists():
            os.startfile(str(p))
        else:
            log("Plik .blend nie jest jeszcze gotowy.")

    open_blend_btn = ttk.Button(
        footer,
        text="Otwórz .blend",
        width=18,
        command=open_blend,
        state="disabled",
    )
    open_blend_btn.grid(
        row=0,
        column=2,
        padx=(0, 8),
    )

    open_folder_btn = ttk.Button(
        footer,
        text="Otwórz folder",
        width=15,
        command=open_out,
        state="disabled",
    )
    open_folder_btn.grid(row=0, column=3)

    def start_conversion():
        src = file_var.get().strip()
        out = out_var.get().strip()

        if not src:
            status_var.set("Najpierw wybierz plik .r3d")
            log("BŁĄD: nie wybrano pliku .r3d")
            return

        asset_info = detected_asset_state.get("info") or {}
        asset_type = asset_info.get("type")

        if asset_type not in (
            "track",
            "standalone_object",
            "simple_metadata_object",
            "rigged_object",
            "car",
            "character",
        ):
            status_var.set("Ten typ R3D nie ma jeszcze gotowego eksportera")
            log(
                "BŁĄD: wykryty typ "
                f"{asset_info.get('label', 'R3D')} "
                "nie ma jeszcze podpiętego eksportera."
            )
            return

        try:
            scale = float(scale_var.get().replace(",", "."))
        except ValueError:
            status_var.set("Nieprawidłowa skala")
            log("BŁĄD: nieprawidłowa skala")
            return

        make_blend = make_blend_var.get()
        export_raw_data = export_raw_data_var.get()
        material_profile = view_material_preset_var.get()
        blender_path = blender_var.get().strip()

        sky_mode = sky_mode_var.get()
        hide_destroyed_props = hide_destroyed_props_var.get()
        hide_raw_assets = hide_raw_assets_var.get()
        generate_sun = generate_sun_var.get()
        material_preview_environment = material_preview_environment_var.get()
        bright_material_preview = bright_material_preview_var.get()
        hide_viewport_grid_axes = hide_viewport_grid_axes_var.get()
        unlit_materials = unlit_materials_var.get()
        animate_textures = animate_textures_var.get()
        include_animated_prop_geometry = include_animated_prop_geometry_var.get()
        include_ai_path = include_ai_path_var.get()
        include_item_boxes = include_item_boxes_var.get()
        include_cones = include_cones_var.get()
        add_isometric_camera = (
            add_isometric_camera_var.get()
        )
        show_cone_paths = show_cone_paths_var.get()
        use_cone_models = use_cone_models_var.get()
        use_sorpresa_asset = use_sorpresa_asset_var.get()
        animate_sorpresa = animate_sorpresa_var.get()
        sorpresa_size_multiplier = (
            round(sorpresa_size_multiplier_var.get() * 20.0)
            / 20.0
        )
        use_r3d_texture_timing = use_r3d_texture_timing_var.get()
        texture_animation_interval = (
            round(texture_animation_interval_var.get() * 100.0) / 100.0
        )
        auto_open_blend = auto_open_blend_var.get()

        car_texture_variant_index = 0
        if asset_type == "car":
            variants = asset_info.get("texture_variants") or []
            selected_label = car_texture_variant_var.get()

            for variant in variants:
                label = car_variant_display_label(
                    variant
                )
                if label == selected_label:
                    car_texture_variant_index = int(
                        variant.get("index", 0)
                    )
                    break

        if make_blend and not blender_path:
            status_var.set("Wskaż blender.exe")
            log("BŁĄD: nie znaleziono Blendera. Wskaż blender.exe.")
            return

        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        log_box.configure(state="disabled")

        convert_btn.configure(state="disabled")
        open_blend_btn.configure(state="disabled")
        open_folder_btn.configure(state="disabled")
        latest_blend_path["path"] = None
        status_var.set("Konwersja…")

        def worker():
            try:
                if asset_type == "track":
                    result = unpack_r3d(
                        src,
                        out or None,
                        scale,
                        export_raw_data=export_raw_data,
                        log=log,
                    )

                    # Gameplay feature checks are intentionally deferred until
                    # export. The file-selection UI does no gameplay parsing.
                    try:
                        export_manifest = json.loads(
                            (
                                Path(result)
                                / "manifest.json"
                            ).read_text(
                                encoding="utf-8"
                            )
                        )
                        gameplay = (
                            export_manifest.get(
                                "gameplay_data"
                            )
                            or {}
                        )

                        if include_cones:
                            static_cones = int(
                                (
                                    gameplay.get("conos")
                                    or {}
                                ).get("count", 0)
                            )
                            moving_cones = len(
                                gameplay.get(
                                    "moving_cone_paths"
                                )
                                or gameplay.get(
                                    "camera_groups"
                                )
                                or []
                            )

                            if (
                                static_cones
                                or moving_cones
                            ):
                                log(
                                    "Pachołki: znaleziono "
                                    f"{static_cones} zwykłych, "
                                    f"{moving_cones} ruchome."
                                )
                            else:
                                log(
                                    "Pachołki: nie znaleziono "
                                    "danych na tej mapie."
                                )

                        if include_item_boxes:
                            item_boxes = int(
                                (
                                    gameplay.get(
                                        "sorpresa"
                                    )
                                    or {}
                                ).get("count", 0)
                            )

                            if item_boxes:
                                log(
                                    "Skrzynki / Sorpresa: "
                                    f"znaleziono {item_boxes}."
                                )
                            else:
                                log(
                                    "Skrzynki / Sorpresa: "
                                    "nie znaleziono danych "
                                    "na tej mapie."
                                )
                    except Exception as exc:
                        log(
                            "Nie udało się odczytać "
                            "podsumowania gameplay po eksporcie: "
                            f"{exc}"
                        )
                elif asset_type == "car":
                    result = export_car_r3d(
                        src,
                        out or None,
                        scale,
                        texture_variant_index=(
                            car_texture_variant_index
                        ),
                        export_raw_data=export_raw_data,
                        log=log,
                    )
                elif asset_type == "character":
                    result = export_character_r3d(
                        src,
                        out or None,
                        scale,
                        export_raw_data=export_raw_data,
                        log=log,
                    )
                elif asset_type == "rigged_object":
                    result = export_rigged_object_r3d(
                        src,
                        out or None,
                        scale,
                        export_raw_data=export_raw_data,
                        log=log,
                    )
                elif asset_type == "simple_metadata_object":
                    result = export_simple_metadata_object_r3d(
                        src,
                        out or None,
                        scale,
                        export_raw_data=export_raw_data,
                        log=log,
                    )
                else:
                    result = unpack_standalone_object_r3d(
                        src,
                        out or None,
                        scale,
                        export_raw_data=export_raw_data,
                        log=log,
                    )

                root.after(
                    0,
                    lambda: open_folder_btn.configure(state="normal"),
                )

                blend_path = None
                if make_blend:
                    if asset_type == "track":
                        blend_path = build_blend_file(
                            result,
                            blender_executable=blender_path,
                            log=log,
                            sky_mode=sky_mode,
                            hide_destroyed_props=hide_destroyed_props,
                            hide_raw_asset_meshes=hide_raw_assets,
                            generate_sun=generate_sun,
                            material_preview_environment=material_preview_environment,
                            bright_material_preview=bright_material_preview,
                            hide_viewport_grid_axes=hide_viewport_grid_axes,
                            unlit_materials=unlit_materials,
                            animate_textures=animate_textures,
                            include_animated_prop_geometry=include_animated_prop_geometry,
                            include_ai_path=include_ai_path,
                            include_item_boxes=include_item_boxes,
                            include_cones=include_cones,
                            show_cone_paths=show_cone_paths,
                            use_cone_models=use_cone_models,
                            add_isometric_camera=add_isometric_camera,
                            material_profile=material_profile,
                            use_sorpresa_asset=use_sorpresa_asset,
                            animate_sorpresa=animate_sorpresa,
                            sorpresa_size_multiplier=sorpresa_size_multiplier,
                            use_r3d_texture_timing=use_r3d_texture_timing,
                            texture_animation_interval=texture_animation_interval,
                        )
                    else:
                        blend_path = build_standalone_object_blend_file(
                            result,
                            blender_executable=blender_path,
                            log=log,
                            unlit_materials=unlit_materials,
                            hide_viewport_grid_axes=hide_viewport_grid_axes,
                            material_preview_environment=(
                                material_preview_environment
                            ),
                            material_profile=material_profile,
                        )

            except Exception as exc:
                root.after(
                    0,
                    lambda: status_var.set("Błąd"),
                )
                log("")
                log("BŁĄD")
                log(str(exc))
            else:
                root.after(
                    0,
                    lambda: status_var.set("Gotowe"),
                )
                log("")
                log("=" * 58)
                log("GOTOWE")
                if blend_path:
                    latest_blend_path["path"] = str(blend_path)
                    root.after(
                        0,
                        lambda: open_blend_btn.configure(state="normal"),
                    )
                    log(f"Otwórz w Blenderze: {blend_path}")

                    if auto_open_blend:
                        try:
                            os.startfile(str(blend_path))
                        except Exception as exc:
                            log(f"Nie udało się automatycznie otworzyć .blend: {exc}")
                else:
                    log(f"Folder wynikowy: {result}")
                log("=" * 58)
            finally:
                root.after(
                    0,
                    lambda: convert_btn.configure(state="normal"),
                )

        threading.Thread(
            target=worker,
            daemon=True,
        ).start()

    convert_btn.configure(command=start_conversion)

    def fit_window_to_content():
        # Let Tk calculate the real requested size of every widget first.
        root.update_idletasks()

        requested_width = root.winfo_reqwidth()
        requested_height = root.winfo_reqheight()

        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()

        # Leave some room for the Windows taskbar/titlebar and screen edges.
        max_width = max(900, screen_width - 80)
        max_height = max(650, screen_height - 100)

        target_width = min(
            max(1180, requested_width + 10),
            max_width,
        )
        target_height = min(
            max(700, requested_height + 10),
            max_height,
        )

        # Never make the window smaller than what the content requests when
        # there is enough desktop space.
        min_width = min(
            max(1000, requested_width),
            max_width,
        )
        min_height = min(
            max(600, requested_height),
            max_height,
        )

        root.minsize(min_width, min_height)
        root.geometry(
            f"{target_width}x{target_height}"
        )

        # Center the final window on screen.
        root.update_idletasks()
        x = max(
            0,
            (screen_width - target_width) // 2,
        )
        y = max(
            0,
            (screen_height - target_height) // 2,
        )
        root.geometry(
            f"{target_width}x{target_height}+{x}+{y}"
        )

    # Initialize dynamic import UI once all widgets/functions exist.
    refresh_source_detection()

    # Run after Tk has completed the first full layout pass.
    root.after_idle(fit_window_to_content)

    root.mainloop()


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1:
        run_gui()
    else:
        run_cli()
