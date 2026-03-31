#!/usr/bin/env python3
"""
RenderDoc Capture Comparison Tool - Ultimate Version
- Direct .rdc file input with automatic conversion
- Fixed Mali compiler integration (vertex/fragment shader types)
- Shader complexity comparison
- HTML report with texture images
- Full automation

Usage:
    python rdc_compare_ultimate.py base.rdc new.rdc [--strict] [--renderdoc PATH]
"""

import xml.etree.ElementTree as ET
import zipfile
import hashlib
import os
import sys
import subprocess
import json
import shutil
import tempfile
import re
import base64
import struct
import platform
from datetime import datetime
from pathlib import Path
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Set

try:
    from PIL import Image
    import io
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class ASTCDecoder:
    """Handles ASTC texture decoding"""
    
    ASTC_FORMATS = {
        'GL_COMPRESSED_RGBA_ASTC_4x4': (4, 4, False),
        'GL_COMPRESSED_RGBA_ASTC_5x4': (5, 4, False),
        'GL_COMPRESSED_RGBA_ASTC_5x5': (5, 5, False),
        'GL_COMPRESSED_RGBA_ASTC_6x5': (6, 5, False),
        'GL_COMPRESSED_RGBA_ASTC_6x6': (6, 6, False),
        'GL_COMPRESSED_RGBA_ASTC_8x5': (8, 5, False),
        'GL_COMPRESSED_RGBA_ASTC_8x6': (8, 6, False),
        'GL_COMPRESSED_RGBA_ASTC_8x8': (8, 8, False),
        'GL_COMPRESSED_RGBA_ASTC_10x5': (10, 5, False),
        'GL_COMPRESSED_RGBA_ASTC_10x6': (10, 6, False),
        'GL_COMPRESSED_RGBA_ASTC_10x8': (10, 8, False),
        'GL_COMPRESSED_RGBA_ASTC_10x10': (10, 10, False),
        'GL_COMPRESSED_RGBA_ASTC_12x10': (12, 10, False),
        'GL_COMPRESSED_RGBA_ASTC_12x12': (12, 12, False),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_4x4': (4, 4, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_5x4': (5, 4, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_5x5': (5, 5, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_6x5': (6, 5, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_6x6': (6, 6, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_8x5': (8, 5, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_8x6': (8, 6, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_8x8': (8, 8, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_10x5': (10, 5, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_10x6': (10, 6, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_10x8': (10, 8, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_10x10': (10, 10, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_12x10': (12, 10, True),
        'GL_COMPRESSED_SRGB8_ALPHA8_ASTC_12x12': (12, 12, True),
        # Vulkan ASTC formats
        'VK_FORMAT_ASTC_4x4_UNORM_BLOCK': (4, 4, False),
        'VK_FORMAT_ASTC_5x4_UNORM_BLOCK': (5, 4, False),
        'VK_FORMAT_ASTC_5x5_UNORM_BLOCK': (5, 5, False),
        'VK_FORMAT_ASTC_6x5_UNORM_BLOCK': (6, 5, False),
        'VK_FORMAT_ASTC_6x6_UNORM_BLOCK': (6, 6, False),
        'VK_FORMAT_ASTC_8x5_UNORM_BLOCK': (8, 5, False),
        'VK_FORMAT_ASTC_8x6_UNORM_BLOCK': (8, 6, False),
        'VK_FORMAT_ASTC_8x8_UNORM_BLOCK': (8, 8, False),
        'VK_FORMAT_ASTC_10x5_UNORM_BLOCK': (10, 5, False),
        'VK_FORMAT_ASTC_10x6_UNORM_BLOCK': (10, 6, False),
        'VK_FORMAT_ASTC_10x8_UNORM_BLOCK': (10, 8, False),
        'VK_FORMAT_ASTC_10x10_UNORM_BLOCK': (10, 10, False),
        'VK_FORMAT_ASTC_12x10_UNORM_BLOCK': (12, 10, False),
        'VK_FORMAT_ASTC_12x12_UNORM_BLOCK': (12, 12, False),
        'VK_FORMAT_ASTC_4x4_SRGB_BLOCK': (4, 4, True),
        'VK_FORMAT_ASTC_5x4_SRGB_BLOCK': (5, 4, True),
        'VK_FORMAT_ASTC_5x5_SRGB_BLOCK': (5, 5, True),
        'VK_FORMAT_ASTC_6x5_SRGB_BLOCK': (6, 5, True),
        'VK_FORMAT_ASTC_6x6_SRGB_BLOCK': (6, 6, True),
        'VK_FORMAT_ASTC_8x5_SRGB_BLOCK': (8, 5, True),
        'VK_FORMAT_ASTC_8x6_SRGB_BLOCK': (8, 6, True),
        'VK_FORMAT_ASTC_8x8_SRGB_BLOCK': (8, 8, True),
        'VK_FORMAT_ASTC_10x5_SRGB_BLOCK': (10, 5, True),
        'VK_FORMAT_ASTC_10x6_SRGB_BLOCK': (10, 6, True),
        'VK_FORMAT_ASTC_10x8_SRGB_BLOCK': (10, 8, True),
        'VK_FORMAT_ASTC_10x10_SRGB_BLOCK': (10, 10, True),
        'VK_FORMAT_ASTC_12x10_SRGB_BLOCK': (12, 10, True),
        'VK_FORMAT_ASTC_12x12_SRGB_BLOCK': (12, 12, True),
    }
    
    @staticmethod
    def create_astc_header(width: int, height: int, block_x: int, block_y: int) -> bytes:
        """Create ASTC file header (16 bytes)"""
        header = bytearray([0x13, 0xAB, 0xA1, 0x5C])  # Magic number
        
        header.append(block_x)
        header.append(block_y)
        header.append(1)  # block_z (always 1 for 2D)
        
        # Image dimensions (3 bytes each, little endian)
        header.extend(struct.pack('<I', width)[:3])
        header.extend(struct.pack('<I', height)[:3])
        header.extend(struct.pack('<I', 1)[:3])  # depth (always 1 for 2D)
        
        return bytes(header)
    
    @staticmethod
    def _find_astcenc() -> Optional[Path]:
        """Find astcenc executable (cross-platform)"""
        is_windows = platform.system() == "Windows"
        platform_dir = "windows" if is_windows else "linux"
        exe_name = "astcenc-sse4.1.exe" if is_windows else "astcenc-sse4.1"

        script_dir = Path(__file__).resolve().parent
        # Check tools/astcenc/<platform>/
        candidate = script_dir / "tools" / "astcenc" / platform_dir / exe_name
        if candidate.exists():
            return candidate
        # Fallback: tools/ root (legacy layout)
        candidate = script_dir / "tools" / exe_name
        if candidate.exists():
            return candidate
        # Check PATH
        result = shutil.which("astcenc-sse4.1") or shutil.which(exe_name)
        if result:
            return Path(result)
        return None

    @staticmethod
    def decode_astc_to_png(astc_data: bytes, width: int, height: int,
                           block_x: int, block_y: int, is_srgb: bool,
                           output_path: str) -> bool:
        """Decode ASTC data to PNG using astcenc"""
        astcenc = ASTCDecoder._find_astcenc()
        if not astcenc:
            return False
        
        try:
            # Calculate mip 0 size
            # ASTC compressed data is organized in blocks of 16 bytes each
            num_blocks_x = (width + block_x - 1) // block_x
            num_blocks_y = (height + block_y - 1) // block_y
            mip0_size = num_blocks_x * num_blocks_y * 16
            
            # Extract only mip level 0 if data contains mipmaps
            if len(astc_data) > mip0_size:
                astc_data = astc_data[:mip0_size]
            
            # Verify we have enough data
            if len(astc_data) < mip0_size:
                return False  # Incomplete data
            
            # Create temporary ASTC file with proper header
            with tempfile.NamedTemporaryFile(suffix='.astc', delete=False) as f:
                header = ASTCDecoder.create_astc_header(width, height, block_x, block_y)
                f.write(header)
                f.write(astc_data)
                astc_file = f.name
            
            # Decode: -ds for sRGB, -dl for linear
            decode_flag = '-ds' if is_srgb else '-dl'
            
            result = subprocess.run([
                str(astcenc),
                decode_flag,
                astc_file,
                output_path
            ], capture_output=True, text=True, timeout=10, encoding='utf-8', errors='replace')
            
            os.unlink(astc_file)
            
            return result.returncode == 0 and os.path.exists(output_path)
            
        except Exception:
            return False

@dataclass
class TextureInfo:
    """Information about a texture resource"""
    resource_id: str
    width: int = 0
    height: int = 0
    mip_levels: int = 1
    format: str = ""
    block_width: int = 0
    block_height: int = 0
    is_astc: bool = False
    is_srgb: bool = False
    data_buffer_id: str = ""  # Which ZIP entry has the data
    data_row_pitch: int = 0
    md5: str = ""
    extracted_path: str = ""
    thumbnail_base64: str = ""  # For HTML embedding
    
@dataclass
class ShaderComplexity:
    """Shader complexity metrics from Mali compiler"""
    work_registers: int = 0
    uniform_registers: int = 0
    alu_cycles: float = 0.0
    ls_cycles: float = 0.0
    varying_cycles: float = 0.0
    texture_cycles: float = 0.0
    total_cycles: float = 0.0
    bound_unit: str = ""  # Which unit is the bottleneck
    arithmetic_16bit: float = 0.0  # Percentage of 16-bit arithmetic
    
    def __str__(self):
        return (f"Cycles: A={self.alu_cycles:.2f} LS={self.ls_cycles:.2f} "
                f"V={self.varying_cycles:.2f} T={self.texture_cycles:.2f} "
                f"(Bound: {self.bound_unit})")
    
@dataclass
class ShaderInfo:
    """Information about a shader program"""
    resource_id: str
    vertex_id: str = ""
    fragment_id: str = ""
    vertex_source: str = ""
    fragment_source: str = ""
    vertex_md5: str = ""
    fragment_md5: str = ""
    vertex_complexity: Optional[ShaderComplexity] = None
    fragment_complexity: Optional[ShaderComplexity] = None
    
@dataclass
class DrawCall:
    """Information about a draw call"""
    index: int
    chunk_index: int
    eid: str = ""  # RenderDoc Event ID
    name: str = ""
    timestamp: int = 0
    primitive_count: int = 0
    vertex_count: int = 0
    instance_count: int = 1
    bound_textures: List[TextureInfo] = field(default_factory=list)
    shader_program: Optional[ShaderInfo] = None
    state_hash: str = ""
    fbo: str = "0"
    
@dataclass
class CaptureData:
    """Complete capture analysis data"""
    xml_path: str
    zip_path: str
    driver: str = ""
    machine_ident: str = ""
    drawcalls: List[DrawCall] = field(default_factory=list)
    textures: Dict[str, TextureInfo] = field(default_factory=dict)
    shaders: Dict[str, ShaderInfo] = field(default_factory=dict)
    total_primitives: int = 0
    total_vertices: int = 0


class RDCConverter:
    """Converts .rdc files to .zip.xml format using renderdoccmd"""
    
    def __init__(self, renderdoc_path: Optional[str] = None):
        self.renderdoc_path = Path(renderdoc_path) if renderdoc_path else None
        self.renderdoccmd = self._find_renderdoccmd()
        
    def _find_renderdoccmd(self) -> Optional[Path]:
        """Find renderdoccmd executable (cross-platform: Windows & Linux)"""
        is_windows = platform.system() == "Windows"
        exe_name = "renderdoccmd.exe" if is_windows else "renderdoccmd"
        platform_dir = "windows" if is_windows else "linux"

        # Check user-provided path
        if self.renderdoc_path:
            exe = self.renderdoc_path / exe_name
            if exe.exists():
                return exe

        # Check bundled tools/renderdoc/<platform>/ relative to script location
        script_dir = Path(__file__).resolve().parent
        bundled = script_dir / "tools" / "renderdoc" / platform_dir / exe_name
        if bundled.exists():
            return bundled

        # Check PATH
        result = shutil.which("renderdoccmd") or shutil.which(exe_name)
        if result:
            return Path(result)

        return None
        
    def convert(self, rdc_path: str, output_dir: str) -> Tuple[str, str]:
        """Convert .rdc to .zip.xml, returns (xml_path, zip_path)"""
        if not self.renderdoccmd:
            raise RuntimeError("renderdoccmd not found. Provide --renderdoc PATH")
            
        rdc_path = Path(rdc_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True, parents=True)
        
        base_name = rdc_path.stem
        xml_path = output_dir / f"{base_name}.zip.xml"
        zip_path = output_dir / f"{base_name}.zip"
        
        # Check if already converted
        if xml_path.exists() and zip_path.exists():
            print(f"  Using existing conversion: {xml_path}")
            return (str(xml_path), str(zip_path))
            
        print(f"  Converting {rdc_path.name} to zip.xml format...")
        
        try:
            # Set up environment for Linux (LD_LIBRARY_PATH for librenderdoc.so)
            env = os.environ.copy()
            renderdoc_dir = self.renderdoccmd.parent
            if platform.system() != "Windows":
                ld_path = env.get("LD_LIBRARY_PATH", "")
                env["LD_LIBRARY_PATH"] = f"{renderdoc_dir}:{ld_path}" if ld_path else str(renderdoc_dir)

            # Run renderdoccmd convert
            result = subprocess.run([
                str(self.renderdoccmd),
                "convert",
                "-f", str(rdc_path),
                "-o", str(output_dir / f"{base_name}.zip.xml")
            ], capture_output=True, text=True, timeout=300, check=True, env=env, encoding='utf-8', errors='replace')
            
            print(f"  [OK] Conversion complete")
            return (str(xml_path), str(zip_path))
            
        except subprocess.CalledProcessError as e:
            print(f"  [FAIL] Conversion failed: {e.stderr}")
            raise
        except subprocess.TimeoutExpired:
            print(f"  [FAIL] Conversion timed out (> 5 minutes)")
            raise


class RDCAnalyzer:
    """Analyzes RenderDoc capture XML and ZIP files"""
    
    def __init__(self, xml_path: str, zip_path: str, output_dir: str, name: str, verbose: bool = False, malioc_path: str = None):
        self.xml_path = xml_path
        self.zip_path = zip_path
        self.output_dir = Path(output_dir)
        self.name = name
        self.verbose = verbose
        self.malioc_override = Path(malioc_path) if malioc_path else None
        self.extract_dir = self.output_dir / f"{name}_extracted"
        self.extract_dir.mkdir(exist_ok=True, parents=True)
        
        print(f"  Loading XML: {Path(xml_path).name}")
        # Read and sanitize XML: remove bytes invalid in XML (non-UTF8 or control chars)
        with open(xml_path, 'rb') as f:
            raw = f.read()
        # Try UTF-8 decoding, replacing invalid bytes
        text = raw.decode('utf-8', errors='replace')
        # Remove XML-illegal control characters (keep \t, \n, \r)
        import re as _re
        text = _re.sub(r'[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]', '', text)
        self.tree = ET.ElementTree(ET.fromstring(text))
        self.root = self.tree.getroot()
        self.zipfile = zipfile.ZipFile(zip_path, 'r')
        
        # Detect driver type
        header = self.root.find('.//header')
        self.driver = ""
        if header is not None:
            drv = header.find('driver')
            if drv is not None and drv.text:
                self.driver = drv.text
        self.is_vulkan = 'Vulkan' in self.driver or 'vulkan' in self.driver
        self.is_d3d11 = 'D3D11' in self.driver
        
    def extract_header_info(self) -> Tuple[str, str]:
        """Extract driver and machine info"""
        header = self.root.find('.//header')
        if header is None:
            return ("", "")
        driver = header.find('driver').text if header.find('driver') is not None else ""
        machine = header.find('machineIdent').text if header.find('machineIdent') is not None else ""
        return driver, machine
        
    def compute_resource_md5(self, resource_num: str) -> str:
        """Compute MD5 hash of a resource"""
        try:
            resource_file = f"{int(resource_num):06d}"
            if resource_file in self.zipfile.namelist():
                data = self.zipfile.read(resource_file)
                return hashlib.md5(data).hexdigest()
        except:
            pass
        return ""
        
    def extract_resource_to_file(self, resource_id: str) -> Optional[str]:
        """Extract a resource to a file"""
        try:
            resource_file = f"{int(resource_id):06d}"
            if resource_file in self.zipfile.namelist():
                data = self.zipfile.read(resource_file)
                output_path = self.extract_dir / f"{resource_file}.bin"
                with open(output_path, 'wb') as f:
                    f.write(data)
                return str(output_path)
        except:
            pass
        return None
    
    def extract_resource_data(self, resource_id: str) -> Optional[bytes]:
        """Extract resource data as bytes"""
        try:
            resource_file = f"{int(resource_id):06d}"
            if resource_file in self.zipfile.namelist():
                return self.zipfile.read(resource_file)
        except:
            pass
        return None

    @staticmethod
    def _image_to_thumbnail_base64(img) -> str:
        img.thumbnail((128, 128), Image.Resampling.LANCZOS)
        if img.mode not in ('RGB', 'RGBA', 'L'):
            img = img.convert('RGBA')
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        img_base64 = base64.b64encode(buffer.getvalue()).decode()
        return f"data:image/png;base64,{img_base64}"

    @staticmethod
    def _build_dds_header(width: int, height: int, fourcc: str, linear_size: int) -> bytes:
        header = bytearray()
        header.extend(b'DDS ')
        header.extend(struct.pack('<I', 124))  # dwSize
        header.extend(struct.pack('<I', 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000))  # dwFlags
        header.extend(struct.pack('<I', height))
        header.extend(struct.pack('<I', width))
        header.extend(struct.pack('<I', linear_size))
        header.extend(struct.pack('<I', 0))  # dwDepth
        header.extend(struct.pack('<I', 1))  # dwMipMapCount
        header.extend(struct.pack('<11I', *([0] * 11)))  # dwReserved1
        header.extend(struct.pack('<I', 32))  # ddspf.dwSize
        header.extend(struct.pack('<I', 0x4))  # ddspf.dwFlags = DDPF_FOURCC
        header.extend(fourcc.encode('ascii'))
        header.extend(struct.pack('<5I', 0, 0, 0, 0, 0))
        header.extend(struct.pack('<I', 0x1000))  # dwCaps = DDSCAPS_TEXTURE
        header.extend(struct.pack('<4I', 0, 0, 0, 0))
        return bytes(header)

    @staticmethod
    def _float_to_u8(value: float) -> int:
        if value != value:
            value = 0.0
        value = max(0.0, value)
        value = value / (1.0 + value)
        return max(0, min(255, int(value * 255.0 + 0.5)))

    @staticmethod
    def _unorm_to_u8(value: int, bits: int) -> int:
        max_value = (1 << bits) - 1
        if max_value <= 0:
            return 0
        return max(0, min(255, int((value / max_value) * 255.0 + 0.5)))

    @staticmethod
    def _infer_row_pitch(data_length: int, width: int, height: int, bytes_per_pixel: int) -> int:
        minimum = width * bytes_per_pixel
        if data_length < minimum or height <= 0:
            return 0
        if data_length == minimum * height:
            return minimum
        if data_length % height == 0:
            pitch = data_length // height
            if pitch >= minimum:
                return pitch
        return 0

    @staticmethod
    def _snorm_to_u8(value: int, bits: int) -> int:
        max_positive = (1 << (bits - 1)) - 1
        min_negative = -(1 << (bits - 1))
        if value >= (1 << (bits - 1)):
            value -= (1 << bits)
        normalized = value / max_positive if value > min_negative else -1.0
        normalized = max(-1.0, min(1.0, normalized))
        return max(0, min(255, int((normalized * 0.5 + 0.5) * 255.0 + 0.5)))

    @staticmethod
    def _decode_unsigned_float(bits: int, exponent_bits: int, mantissa_bits: int) -> float:
        exponent_mask = (1 << exponent_bits) - 1
        mantissa_mask = (1 << mantissa_bits) - 1
        exponent = (bits >> mantissa_bits) & exponent_mask
        mantissa = bits & mantissa_mask
        bias = (1 << (exponent_bits - 1)) - 1

        if exponent == 0:
            if mantissa == 0:
                return 0.0
            return (mantissa / (1 << mantissa_bits)) * (2 ** (1 - bias))
        if exponent == exponent_mask:
            return 65504.0 if mantissa == 0 else 0.0
        return (1.0 + mantissa / (1 << mantissa_bits)) * (2 ** (exponent - bias))

    @staticmethod
    def _build_dds_dx10_header(width: int, height: int, dxgi_format: int, linear_size: int) -> bytes:
        header = bytearray()
        header.extend(b'DDS ')
        header.extend(struct.pack('<I', 124))
        header.extend(struct.pack('<I', 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000))
        header.extend(struct.pack('<I', height))
        header.extend(struct.pack('<I', width))
        header.extend(struct.pack('<I', linear_size))
        header.extend(struct.pack('<I', 0))
        header.extend(struct.pack('<I', 1))
        header.extend(struct.pack('<11I', *([0] * 11)))
        header.extend(struct.pack('<I', 32))
        header.extend(struct.pack('<I', 0x4))
        header.extend(b'DX10')
        header.extend(struct.pack('<5I', 0, 0, 0, 0, 0))
        header.extend(struct.pack('<I', 0x1000))
        header.extend(struct.pack('<4I', 0, 0, 0, 0))
        header.extend(struct.pack('<5I', dxgi_format, 3, 0, 1, 0))
        return bytes(header)

    def _create_d3d11_thumbnail(self, tex_info: TextureInfo, data: bytes) -> str:
        fmt = (tex_info.format or "").upper()
        width = tex_info.width
        height = tex_info.height
        explicit_row_pitch = tex_info.data_row_pitch

        if width <= 0 or height <= 0 or not data:
            return ""

        compressed_fourcc = {
            'DXGI_FORMAT_BC1_': 'DXT1',
            'DXGI_FORMAT_BC2_': 'DXT3',
            'DXGI_FORMAT_BC3_': 'DXT5',
            'DXGI_FORMAT_BC4_': 'ATI1',
            'DXGI_FORMAT_BC5_': 'ATI2',
        }

        for prefix, fourcc in compressed_fourcc.items():
            if not fmt.startswith(prefix):
                continue
            try:
                dds_data = self._build_dds_header(width, height, fourcc, len(data)) + data
                img = Image.open(io.BytesIO(dds_data))
                return self._image_to_thumbnail_base64(img)
            except Exception:
                break

        dx10_formats = {
            'DXGI_FORMAT_BC6H_TYPELESS': 94,
            'DXGI_FORMAT_BC6H_UF16': 95,
            'DXGI_FORMAT_BC6H_SF16': 96,
            'DXGI_FORMAT_BC7_TYPELESS': 97,
            'DXGI_FORMAT_BC7_UNORM': 98,
            'DXGI_FORMAT_BC7_UNORM_SRGB': 99,
        }
        if fmt in dx10_formats:
            try:
                dds_data = self._build_dds_dx10_header(width, height, dx10_formats[fmt], len(data)) + data
                img = Image.open(io.BytesIO(dds_data))
                return self._image_to_thumbnail_base64(img)
            except Exception:
                pass

        try:
            pixel_count = width * height

            if fmt in {'DXGI_FORMAT_A8_UNORM', 'DXGI_FORMAT_R8_UNORM'} and len(data) >= pixel_count:
                img = Image.frombytes('L', (width, height), data[:pixel_count])
                return self._image_to_thumbnail_base64(img)

            if fmt.startswith('DXGI_FORMAT_R8G8B8A8_'):
                row_pitch = explicit_row_pitch or self._infer_row_pitch(len(data), width, height, 4)
                if row_pitch:
                    rgba = bytearray(pixel_count * 4)
                    for y in range(height):
                        src = y * row_pitch
                        dst = y * width * 4
                        rgba[dst:dst + width * 4] = data[src:src + width * 4]
                    img = Image.frombytes('RGBA', (width, height), bytes(rgba), 'raw', 'RGBA')
                    return self._image_to_thumbnail_base64(img)

            if fmt.startswith('DXGI_FORMAT_B8G8R8A8_'):
                row_pitch = explicit_row_pitch or self._infer_row_pitch(len(data), width, height, 4)
                if row_pitch:
                    bgra = bytearray(pixel_count * 4)
                    for y in range(height):
                        src = y * row_pitch
                        dst = y * width * 4
                        bgra[dst:dst + width * 4] = data[src:src + width * 4]
                    img = Image.frombytes('RGBA', (width, height), bytes(bgra), 'raw', 'BGRA')
                    return self._image_to_thumbnail_base64(img)

            if fmt.startswith('DXGI_FORMAT_R16_TYPELESS') or fmt.startswith('DXGI_FORMAT_R16_UNORM'):
                row_pitch = explicit_row_pitch or self._infer_row_pitch(len(data), width, height, 2)
                if row_pitch:
                    gray = bytearray(pixel_count)
                    for y in range(height):
                        row_start = y * row_pitch
                        for x in range(width):
                            src = row_start + x * 2
                            gray[y * width + x] = self._unorm_to_u8(struct.unpack_from('<H', data, src)[0], 16)
                    img = Image.frombytes('L', (width, height), bytes(gray))
                    return self._image_to_thumbnail_base64(img)

            if fmt.startswith('DXGI_FORMAT_R16_FLOAT'):
                row_pitch = explicit_row_pitch or self._infer_row_pitch(len(data), width, height, 2)
                if row_pitch:
                    gray = bytearray(pixel_count)
                    for y in range(height):
                        row_start = y * row_pitch
                        for x in range(width):
                            src = row_start + x * 2
                            gray[y * width + x] = self._float_to_u8(struct.unpack_from('<e', data, src)[0])
                    img = Image.frombytes('L', (width, height), bytes(gray))
                    return self._image_to_thumbnail_base64(img)

            if fmt.startswith('DXGI_FORMAT_R16G16B16A16_'):
                row_pitch = explicit_row_pitch or self._infer_row_pitch(len(data), width, height, 8)
                if row_pitch:
                    rgba = bytearray(pixel_count * 4)
                    for y in range(height):
                        row_start = y * row_pitch
                        for x in range(width):
                            src = row_start + x * 8
                            base = (y * width + x) * 4
                            if 'FLOAT' in fmt:
                                r = struct.unpack_from('<e', data, src)[0]
                                g = struct.unpack_from('<e', data, src + 2)[0]
                                b = struct.unpack_from('<e', data, src + 4)[0]
                                a = struct.unpack_from('<e', data, src + 6)[0]
                                rgba[base + 0] = self._float_to_u8(r)
                                rgba[base + 1] = self._float_to_u8(g)
                                rgba[base + 2] = self._float_to_u8(b)
                                rgba[base + 3] = self._float_to_u8(a)
                            elif 'SNORM' in fmt:
                                rgba[base + 0] = self._snorm_to_u8(struct.unpack_from('<H', data, src)[0], 16)
                                rgba[base + 1] = self._snorm_to_u8(struct.unpack_from('<H', data, src + 2)[0], 16)
                                rgba[base + 2] = self._snorm_to_u8(struct.unpack_from('<H', data, src + 4)[0], 16)
                                rgba[base + 3] = self._snorm_to_u8(struct.unpack_from('<H', data, src + 6)[0], 16)
                            else:
                                rgba[base + 0] = self._unorm_to_u8(struct.unpack_from('<H', data, src)[0], 16)
                                rgba[base + 1] = self._unorm_to_u8(struct.unpack_from('<H', data, src + 2)[0], 16)
                                rgba[base + 2] = self._unorm_to_u8(struct.unpack_from('<H', data, src + 4)[0], 16)
                                rgba[base + 3] = self._unorm_to_u8(struct.unpack_from('<H', data, src + 6)[0], 16)
                    img = Image.frombytes('RGBA', (width, height), bytes(rgba))
                    return self._image_to_thumbnail_base64(img)

            if fmt.startswith('DXGI_FORMAT_R10G10B10A2_'):
                row_pitch = explicit_row_pitch or self._infer_row_pitch(len(data), width, height, 4)
                if row_pitch:
                    rgba = bytearray(pixel_count * 4)
                    for y in range(height):
                        row_start = y * row_pitch
                        for x in range(width):
                            packed = struct.unpack_from('<I', data, row_start + x * 4)[0]
                            r = packed & 0x3FF
                            g = (packed >> 10) & 0x3FF
                            b = (packed >> 20) & 0x3FF
                            a = (packed >> 30) & 0x3
                            base = (y * width + x) * 4
                            rgba[base + 0] = self._unorm_to_u8(r, 10)
                            rgba[base + 1] = self._unorm_to_u8(g, 10)
                            rgba[base + 2] = self._unorm_to_u8(b, 10)
                            rgba[base + 3] = self._unorm_to_u8(a, 2)
                    img = Image.frombytes('RGBA', (width, height), bytes(rgba))
                    return self._image_to_thumbnail_base64(img)

            if fmt.startswith('DXGI_FORMAT_R11G11B10_'):
                row_pitch = explicit_row_pitch or self._infer_row_pitch(len(data), width, height, 4)
                if row_pitch:
                    rgba = bytearray(pixel_count * 4)
                    for y in range(height):
                        row_start = y * row_pitch
                        for x in range(width):
                            packed = struct.unpack_from('<I', data, row_start + x * 4)[0]
                            r = packed & 0x7FF
                            g = (packed >> 11) & 0x7FF
                            b = (packed >> 22) & 0x3FF
                            base = (y * width + x) * 4
                            rgba[base + 0] = self._float_to_u8(self._decode_unsigned_float(r, 5, 6))
                            rgba[base + 1] = self._float_to_u8(self._decode_unsigned_float(g, 5, 6))
                            rgba[base + 2] = self._float_to_u8(self._decode_unsigned_float(b, 5, 5))
                            rgba[base + 3] = 255
                    img = Image.frombytes('RGBA', (width, height), bytes(rgba))
                    return self._image_to_thumbnail_base64(img)
        except Exception:
            pass

        return ""
        
    def create_texture_thumbnail(self, tex_info: TextureInfo) -> str:
        """Create base64-encoded thumbnail for HTML embedding with ASTC support"""
        if not PIL_AVAILABLE:
            return ""
            
        # Determine which resource ID to use
        # For ASTC: prefer data_buffer_id (from glCompressedTexSubImage2D), fallback to resource_id
        # For other textures: use data_buffer_id if available, otherwise resource_id
        resource_id = tex_info.data_buffer_id if tex_info.data_buffer_id else tex_info.resource_id
        if not resource_id:
            return ""
        
        try:
            resource_file = f"{int(resource_id):06d}"
            if resource_file not in self.zipfile.namelist():
                return ""  # Texture was allocated but never uploaded
                
            data = self.zipfile.read(resource_file)
            
            if tex_info.is_astc:
                # Decode ASTC texture
                png_path = self.extract_dir / f"thumb_{tex_info.resource_id}.png"
                
                success = ASTCDecoder.decode_astc_to_png(
                    data,
                    tex_info.width,
                    tex_info.height,
                    tex_info.block_width,
                    tex_info.block_height,
                    tex_info.is_srgb,
                    str(png_path)
                )
                
                if success:
                    try:
                        img = Image.open(png_path)
                        result = self._image_to_thumbnail_base64(img)
                        # Clean up
                        png_path.unlink()
                        return result
                    except Exception:
                        if png_path.exists():
                            png_path.unlink()
            else:
                if self.is_d3d11:
                    preview = self._create_d3d11_thumbnail(tex_info, data)
                    if preview:
                        return preview

                # Try to load as an already-encoded image blob
                try:
                    img = Image.open(io.BytesIO(data))
                    return self._image_to_thumbnail_base64(img)
                except:
                    pass
        except:
            pass
            
        return ""
        
    def extract_textures(self) -> Dict[str, TextureInfo]:
        """Extract all texture resources with ASTC support"""
        textures = {}
        current_texture = None  # Track currently bound texture
        
        # First pass: find textures and their formats
        for chunk in self.root.findall('.//chunk'):
            chunk_name = chunk.get('name', '')
            
            # Track texture binding
            if 'glBindTexture' in chunk_name:
                for elem in chunk:
                    if elem.get('name') == 'texture':
                        tex_id = elem.text
                        if tex_id and tex_id != "0":
                            current_texture = tex_id
                            if tex_id not in textures:
                                textures[tex_id] = TextureInfo(resource_id=tex_id)
            
            # glTexStorage2D / glCompressedTexImage2D - get format and dimensions
            elif any(cmd in chunk_name for cmd in ['glTexStorage2D', 'glCompressedTexImage2D', 'glTexImage2D']):
                if not current_texture:
                    continue
                    
                width = 0
                height = 0
                format_str = ""
                
                for elem in chunk:
                    elem_name = elem.get('name', '')
                    if 'width' in elem_name:
                        try:
                            width = int(elem.text)
                        except:
                            pass
                    elif 'height' in elem_name:
                        try:
                            height = int(elem.text)
                        except:
                            pass
                    elif 'internalformat' in elem_name or 'format' in elem_name:
                        format_str = elem.get('string', '')
                
                if current_texture in textures:
                    tex = textures[current_texture]
                    tex.width = width or tex.width
                    tex.height = height or tex.height
                    tex.format = format_str or tex.format
                    
                    # Check if ASTC
                    if format_str in ASTCDecoder.ASTC_FORMATS:
                        block_x, block_y, is_srgb = ASTCDecoder.ASTC_FORMATS[format_str]
                        tex.is_astc = True
                        tex.block_width = block_x
                        tex.block_height = block_y
                        tex.is_srgb = is_srgb
            
            # glCompressedTexSubImage2D - get data buffer reference
            elif 'glCompressedTexSubImage2D' in chunk_name:
                # Extract which texture this upload is for
                upload_texture_id = None
                mip_level = 999  # Default to high number
                mip_width = 0
                mip_height = 0
                buffer_id = None
                
                for elem in chunk:
                    elem_name = elem.get('name', '')
                    if elem_name == 'texture':
                        upload_texture_id = elem.text
                    elif elem_name == 'level':
                        try:
                            mip_level = int(elem.text)
                        except:
                            pass
                    elif 'width' in elem_name:
                        try:
                            mip_width = int(elem.text)
                        except:
                            pass
                    elif 'height' in elem_name:
                        try:
                            mip_height = int(elem.text)
                        except:
                            pass
                    elif elem.get('name') == 'pixels' and elem.tag == 'buffer':
                        buffer_id = elem.text
                
                # Skip if we couldn't find the texture ID or it's not tracked
                if not upload_texture_id or upload_texture_id not in textures:
                    continue
                
                # Only use mip level 0 buffer that MATCHES the declared dimensions
                if buffer_id and mip_level == 0:
                    tex = textures[upload_texture_id]  # Use the texture from THIS upload, not current_texture!
                    
                    # If dimensions are already set by glTexStorage2D, verify they match
                    if tex.width > 0 and tex.height > 0:
                        # Only accept buffer if dimensions match exactly
                        if mip_width == tex.width and mip_height == tex.height:
                            tex.data_buffer_id = buffer_id
                    else:
                        # No dimensions set yet, use this buffer and set dimensions
                        tex.data_buffer_id = buffer_id
                        if mip_width > 0 and mip_height > 0:
                            tex.width = mip_width
                            tex.height = mip_height
        
        # Compute MD5s and create thumbnails
        for tex_id, tex_info in textures.items():
            # For ASTC: prefer data_buffer_id (mip 0 from glCompressedTexSubImage2D), fallback to resource_id
            # For other textures: use data_buffer_id if available, otherwise resource_id
            resource_id = tex_info.data_buffer_id if tex_info.data_buffer_id else tex_id
            tex_info.md5 = self.compute_resource_md5(resource_id)
            tex_info.extracted_path = self.extract_resource_to_file(resource_id) or ""
            
            # Only create thumbnail if we have valid dimensions
            if tex_info.width > 0 and tex_info.height > 0:
                tex_info.thumbnail_base64 = self.create_texture_thumbnail(tex_info)
            
        return textures
    
    def extract_textures_vulkan(self) -> Dict[str, TextureInfo]:
        """Extract Vulkan image resources"""
        textures = {}
        # Skip depth/stencil and render target formats
        skip_formats = {'VK_FORMAT_D16_UNORM', 'VK_FORMAT_D24_UNORM_S8_UINT',
                        'VK_FORMAT_D32_SFLOAT', 'VK_FORMAT_D32_SFLOAT_S8_UINT',
                        'VK_FORMAT_S8_UINT'}
        
        for chunk in self.root.findall('.//chunk'):
            if chunk.get('name', '') != 'vkCreateImage':
                continue
            
            img_id_elem = chunk.find('.//ResourceId[@name="Image"]')
            if img_id_elem is None:
                continue
            img_id = img_id_elem.text
            
            create_info = chunk.find('.//struct[@typename="VkImageCreateInfo"]')
            if create_info is None:
                continue
            
            format_elem = create_info.find('.//enum[@name="format"]')
            format_str = format_elem.get('string', '') if format_elem is not None else ''
            
            if format_str in skip_formats:
                continue
            
            width = 0
            height = 0
            extent = create_info.find('.//struct[@typename="VkExtent3D"]')
            if extent is not None:
                w_elem = extent.find('.//*[@name="width"]')
                h_elem = extent.find('.//*[@name="height"]')
                if w_elem is not None and w_elem.text:
                    try: width = int(w_elem.text)
                    except: pass
                if h_elem is not None and h_elem.text:
                    try: height = int(h_elem.text)
                    except: pass
            
            # Skip tiny images (likely dummy/placeholder)
            if width <= 1 and height <= 1:
                continue
            
            tex = TextureInfo(resource_id=img_id)
            tex.width = width
            tex.height = height
            tex.format = format_str
            
            if format_str in ASTCDecoder.ASTC_FORMATS:
                block_x, block_y, is_srgb = ASTCDecoder.ASTC_FORMATS[format_str]
                tex.is_astc = True
                tex.block_width = block_x
                tex.block_height = block_y
                tex.is_srgb = is_srgb
            
            textures[img_id] = tex
        
        # Compute MD5s
        for tex_id, tex_info in textures.items():
            resource_id = tex_info.data_buffer_id if tex_info.data_buffer_id else tex_id
            tex_info.md5 = self.compute_resource_md5(resource_id)
            tex_info.extracted_path = self.extract_resource_to_file(resource_id) or ""
            if tex_info.width > 0 and tex_info.height > 0:
                tex_info.thumbnail_base64 = self.create_texture_thumbnail(tex_info)
        
        return textures

    def extract_textures_d3d11(self) -> Dict[str, TextureInfo]:
        """Extract D3D11 texture resources mapped by SRV ID"""
        textures_by_resource: Dict[str, TextureInfo] = {}
        resource_uploads: Dict[Tuple[str, int], Tuple[str, int]] = {}
        resource_views: Dict[str, Tuple[str, str, int]] = {}

        for chunk in self.root.findall('.//chunk'):
            name = chunk.get('name', '')

            if name == 'ID3D11Device::CreateTexture2D':
                resource_elem = chunk.find('.//ResourceId[@name="pTexture"]')
                desc = chunk.find('.//struct[@typename="D3D11_TEXTURE2D_DESC"]')
                if resource_elem is None or desc is None:
                    continue

                resource_id = resource_elem.text
                tex = TextureInfo(resource_id=resource_id)
                width_elem = desc.find('.//*[@name="Width"]')
                height_elem = desc.find('.//*[@name="Height"]')
                mip_levels_elem = desc.find('.//*[@name="MipLevels"]')
                format_elem = desc.find('.//*[@name="Format"]')
                if width_elem is not None and width_elem.text:
                    try:
                        tex.width = int(width_elem.text)
                    except Exception:
                        pass
                if height_elem is not None and height_elem.text:
                    try:
                        tex.height = int(height_elem.text)
                    except Exception:
                        pass
                if mip_levels_elem is not None and mip_levels_elem.text:
                    try:
                        tex.mip_levels = max(1, int(mip_levels_elem.text))
                    except Exception:
                        pass
                if format_elem is not None:
                    tex.format = format_elem.get('string', '') or ''
                textures_by_resource[resource_id] = tex

                initial_buffer = chunk.find('.//buffer[@name="SubresourceContents"]')
                if initial_buffer is not None and initial_buffer.text:
                    resource_uploads[(resource_id, 0)] = (initial_buffer.text or '', 0)
                else:
                    initial_buffer = chunk.find('.//buffer[@name="InitialData"]')
                    if initial_buffer is not None and initial_buffer.text:
                        resource_uploads[(resource_id, 0)] = (initial_buffer.text or '', 0)
                    else:
                        initial_buffer = chunk.find('.//buffer[@name="pSysMem"]')
                        if initial_buffer is not None and initial_buffer.text:
                            resource_uploads[(resource_id, 0)] = (initial_buffer.text or '', 0)

            elif name == 'ID3D11DeviceContext::UpdateSubresource':
                dst_resource = chunk.find('.//ResourceId[@name="pDstResource"]')
                subresource = chunk.find('.//*[@name="DstSubresource"]')
                contents = chunk.find('.//buffer[@name="Contents"]')
                if dst_resource is None or contents is None:
                    continue
                subresource_index = 0
                if subresource is not None and subresource.text:
                    try:
                        subresource_index = int(subresource.text)
                    except Exception:
                        subresource_index = 0
                row_pitch = 0
                row_pitch_elem = chunk.find('.//*[@name="SrcRowPitch"]')
                if row_pitch_elem is not None and row_pitch_elem.text:
                    try:
                        row_pitch = int(row_pitch_elem.text)
                    except Exception:
                        row_pitch = 0
                resource_uploads[(dst_resource.text, subresource_index)] = (contents.text or '', row_pitch)

            elif name == 'ID3D11Device::CreateShaderResourceView':
                resource_elem = chunk.find('.//ResourceId[@name="pResource"]')
                view_elem = chunk.find('.//ResourceId[@name="pView"]')
                if resource_elem is None or view_elem is None:
                    continue
                resource_id = resource_elem.text
                view_id = view_elem.text
                if resource_id not in textures_by_resource:
                    continue

                view_desc = chunk.find('.//struct[@typename="D3D11_SHADER_RESOURCE_VIEW_DESC"]')
                view_format = ""
                most_detailed_mip = 0
                if view_desc is not None:
                    view_format_elem = view_desc.find('.//*[@name="Format"]')
                    if view_format_elem is not None:
                        view_format = view_format_elem.get('string', '') or ''
                    mip_elem = view_desc.find('.//*[@name="MostDetailedMip"]')
                    if mip_elem is not None and mip_elem.text:
                        try:
                            most_detailed_mip = int(mip_elem.text)
                        except Exception:
                            most_detailed_mip = 0
                resource_views[view_id] = (resource_id, view_format, most_detailed_mip)

            elif name == 'Internal::Initial Contents':
                resource_type = chunk.find('.//*[@name="type"]')
                resource_elem = chunk.find('.//ResourceId[@name="id"]')
                omitted_elem = chunk.find('.//*[@name="OmittedContents"]')
                if resource_type is None or resource_elem is None:
                    continue
                if resource_type.get('string', '') != 'Resource_Texture2D':
                    continue
                if omitted_elem is not None and (omitted_elem.text or '').lower() == 'true':
                    continue
                row_pitch_values = []
                for row_pitch_elem in chunk.findall('.//*[@name="RowPitch"]'):
                    if row_pitch_elem.text:
                        try:
                            row_pitch_values.append(int(row_pitch_elem.text))
                        except Exception:
                            row_pitch_values.append(0)
                buffers = chunk.findall('.//buffer[@name="SubresourceContents"]')
                for subresource_index, buffer_elem in enumerate(buffers):
                    if buffer_elem.text:
                        row_pitch = row_pitch_values[subresource_index] if subresource_index < len(row_pitch_values) else 0
                        resource_uploads[(resource_elem.text, subresource_index)] = (buffer_elem.text or '', row_pitch)

        textures_by_srv: Dict[str, TextureInfo] = {}
        for view_id, (resource_id, view_format, most_detailed_mip) in resource_views.items():
            source_tex = textures_by_resource.get(resource_id)
            if source_tex is None:
                continue

            tex = TextureInfo(resource_id=resource_id)
            tex.mip_levels = source_tex.mip_levels
            tex.width = max(1, source_tex.width >> most_detailed_mip)
            tex.height = max(1, source_tex.height >> most_detailed_mip)
            tex.format = view_format or source_tex.format
            upload = resource_uploads.get((resource_id, most_detailed_mip))
            if upload is None and most_detailed_mip != 0:
                upload = resource_uploads.get((resource_id, 0))
            if upload is not None:
                tex.data_buffer_id = upload[0]
                tex.data_row_pitch = upload[1]
            textures_by_srv[view_id] = tex

        for srv_id, tex_info in textures_by_srv.items():
            resource_id = tex_info.data_buffer_id if tex_info.data_buffer_id else tex_info.resource_id
            tex_info.md5 = self.compute_resource_md5(resource_id)
            tex_info.extracted_path = self.extract_resource_to_file(resource_id) or ""
            if tex_info.width > 0 and tex_info.height > 0:
                tex_info.thumbnail_base64 = self.create_texture_thumbnail(tex_info)

        return textures_by_srv
    
    def extract_shaders_vulkan(self) -> Dict[str, ShaderInfo]:
        """Extract Vulkan shader modules and graphics pipelines"""
        # Map shader module resource ID -> SPIR-V binary MD5
        shader_module_md5 = {}
        shader_module_code_ref = {}
        
        for chunk in self.root.findall('.//chunk'):
            if chunk.get('name', '') != 'vkCreateShaderModule':
                continue
            mod_id_elem = chunk.find('.//ResourceId[@name="ShaderModule"]')
            if mod_id_elem is None:
                continue
            mod_id = mod_id_elem.text
            
            create_info = chunk.find('.//struct[@typename="VkShaderModuleCreateInfo"]')
            if create_info is None:
                continue
            code_elem = create_info.find('.//buffer[@name="pCode"]')
            if code_elem is not None and code_elem.text:
                code_ref = code_elem.text
                shader_module_code_ref[mod_id] = code_ref
                shader_module_md5[mod_id] = self.compute_resource_md5(code_ref)
        
        # Parse pipelines: map pipeline ID -> ShaderInfo
        shaders = {}
        for chunk in self.root.findall('.//chunk'):
            if chunk.get('name', '') != 'vkCreateGraphicsPipelines':
                continue
            
            pipeline_id_elem = chunk.find('.//ResourceId[@name="Pipeline"]')
            if pipeline_id_elem is None:
                continue
            pipeline_id = pipeline_id_elem.text
            
            stages = chunk.find('.//array[@name="pStages"]')
            if stages is None:
                continue
            
            shader_info = ShaderInfo(resource_id=pipeline_id)
            
            for stage in stages:
                stage_bits_elem = stage.find('.//enum[@name="stage"]')
                module_elem = stage.find('.//ResourceId[@name="module"]')
                if stage_bits_elem is None or module_elem is None:
                    continue
                
                stage_val = stage_bits_elem.text or ''
                mod_id = module_elem.text
                
                # VkShaderStageFlagBits: vertex=1, fragment=16
                if stage_val == '1':
                    shader_info.vertex_id = mod_id
                    shader_info.vertex_md5 = shader_module_md5.get(mod_id, '')
                    # SPIR-V binary, no source text
                    shader_info.vertex_source = f"[SPIR-V binary, resource={shader_module_code_ref.get(mod_id,'')}]"
                elif stage_val == '16':
                    shader_info.fragment_id = mod_id
                    shader_info.fragment_md5 = shader_module_md5.get(mod_id, '')
                    shader_info.fragment_source = f"[SPIR-V binary, resource={shader_module_code_ref.get(mod_id,'')}]"
            
            if shader_info.vertex_md5 or shader_info.fragment_md5:
                shaders[pipeline_id] = shader_info
        
        return shaders
    
    def extract_drawcalls_vulkan(self, textures: Dict, shaders: Dict) -> List[DrawCall]:
        """Extract Vulkan draw calls"""
        drawcalls = []
        draw_index = 0
        
        current_pipeline = None
        marker_stack = []
        
        chunk_has_index = False
        first_chunk = self.root.find('.//chunk')
        if first_chunk is not None and first_chunk.get('chunkIndex') is not None:
            chunk_has_index = True
        
        # Find first action for EID offset calculation
        vk_action_patterns = ['vkCmdDraw', 'vkCmdDrawIndexed', 'vkCmdClearAttachments',
                              'vkCmdDispatch', 'vkCmdCopyBuffer', 'vkCmdBlitImage',
                              'vkCmdBeginRenderPass']
        
        first_action_chunk_idx = None
        sim_idx = 0
        for chunk in self.root.findall('.//chunk'):
            name = chunk.get('name', '')
            if chunk_has_index:
                cidx = int(chunk.get('chunkIndex', 0))
            else:
                cidx = sim_idx
                sim_idx += 1
            if any(p in name for p in vk_action_patterns):
                first_action_chunk_idx = cidx
                break
        
        eid_offset = (first_action_chunk_idx - 10) if first_action_chunk_idx else 0
        
        # Second pass: extract draw calls
        sim_idx = 0
        # Track bound descriptor sets for texture binding
        bound_images = {}  # pipeline -> set of image resource IDs
        
        for chunk in self.root.findall('.//chunk'):
            name = chunk.get('name', '')
            
            if chunk_has_index:
                chunk_idx = int(chunk.get('chunkIndex', 0))
            else:
                chunk_idx = sim_idx
                sim_idx += 1
            
            timestamp = int(chunk.get('timestamp', 0))
            
            if name == 'vkCmdBindPipeline':
                bind_point_elem = chunk.find('.//enum[@name="pipelineBindPoint"]')
                pipeline_elem = chunk.find('.//ResourceId[@name="pipeline"]')
                if bind_point_elem is not None and pipeline_elem is not None:
                    bp_str = bind_point_elem.get('string', '')
                    if 'GRAPHICS' in bp_str or bind_point_elem.text == '0':
                        current_pipeline = pipeline_elem.text
            
            elif name == 'vkCmdDebugMarkerBeginEXT':
                marker_name_elem = chunk.find('.//string[@name="pMarkerName"]')
                if marker_name_elem is not None and marker_name_elem.text:
                    marker_stack.append(marker_name_elem.text)
            
            elif name == 'vkCmdDebugMarkerEndEXT':
                if marker_stack:
                    marker_stack.pop()
            
            elif name in ('vkCmdDrawIndexed', 'vkCmdDraw'):
                eid = str(chunk_idx - eid_offset)
                marker_name = marker_stack[-1] if marker_stack else name
                
                drawcall = DrawCall(
                    index=draw_index,
                    chunk_index=chunk_idx,
                    eid=eid,
                    name=marker_name,
                    timestamp=timestamp,
                    fbo=""
                )
                
                if name == 'vkCmdDrawIndexed':
                    idx_count_elem = chunk.find('.//*[@name="indexCount"]')
                    inst_count_elem = chunk.find('.//*[@name="instanceCount"]')
                    if idx_count_elem is not None and idx_count_elem.text:
                        try:
                            drawcall.vertex_count = int(idx_count_elem.text)
                        except: pass
                    if inst_count_elem is not None and inst_count_elem.text:
                        try:
                            drawcall.instance_count = int(inst_count_elem.text)
                        except: pass
                else:  # vkCmdDraw
                    vert_count_elem = chunk.find('.//*[@name="vertexCount"]')
                    inst_count_elem = chunk.find('.//*[@name="instanceCount"]')
                    if vert_count_elem is not None and vert_count_elem.text:
                        try:
                            drawcall.vertex_count = int(vert_count_elem.text)
                        except: pass
                    if inst_count_elem is not None and inst_count_elem.text:
                        try:
                            drawcall.instance_count = int(inst_count_elem.text)
                        except: pass
                
                if drawcall.vertex_count > 0:
                    drawcall.primitive_count = drawcall.vertex_count // 3
                
                if current_pipeline and current_pipeline in shaders:
                    drawcall.shader_program = shaders[current_pipeline]
                
                # Build state hash
                sp = drawcall.shader_program
                state_components = [
                    current_pipeline or "",
                    sp.vertex_md5 if sp else "",
                    sp.fragment_md5 if sp else "",
                ]
                drawcall.state_hash = hashlib.md5("|".join(state_components).encode()).hexdigest()
                
                drawcalls.append(drawcall)
                draw_index += 1
        
        return drawcalls

    def extract_shaders_d3d11(self) -> Dict[str, Dict[str, str]]:
        """Extract D3D11 shader bytecode hashes keyed by shader resource id"""
        shaders = {}
        for chunk in self.root.findall('.//chunk'):
            name = chunk.get('name', '')
            stage = None
            shader_type = None
            if name == 'ID3D11Device::CreateVertexShader':
                stage = 'vertex'
                shader_type = 'ID3D11VertexShader'
            elif name == 'ID3D11Device::CreatePixelShader':
                stage = 'fragment'
                shader_type = 'ID3D11PixelShader'
            else:
                continue

            bytecode_elem = chunk.find('.//buffer[@name="pShaderBytecode"]')
            shader_elem = chunk.find(f'.//ResourceId[@name="pShader"]')
            if bytecode_elem is None or shader_elem is None or not bytecode_elem.text:
                continue

            bytecode_ref = bytecode_elem.text
            shader_id = shader_elem.text
            shaders[shader_id] = {
                'stage': stage,
                'md5': self.compute_resource_md5(bytecode_ref),
                'source': f"[DXBC bytecode, resource={bytecode_ref}]",
                'type': shader_type,
            }

        return shaders

    def extract_drawcalls_d3d11(self, textures: Dict, shader_db: Dict[str, Dict[str, str]]) -> List[DrawCall]:
        """Extract D3D11 draw calls"""
        drawcalls = []
        draw_index = 0
        current_vs = None
        current_ps = None
        current_rtvs = []
        ps_srvs = defaultdict(str)
        vs_srvs = defaultdict(str)
        marker_stack = []
        combined_shader_cache: Dict[str, ShaderInfo] = {}

        first_action_chunk_idx = None
        action_patterns = [
            'ID3D11DeviceContext::Draw',
            'ID3D11DeviceContext::DrawIndexed',
            'ID3D11DeviceContext::Dispatch',
            'ID3D11DeviceContext::ClearRenderTargetView',
            'ID3D11DeviceContext::ClearDepthStencilView',
        ]
        for chunk in self.root.findall('.//chunk'):
            name = chunk.get('name', '')
            chunk_idx = int(chunk.get('chunkIndex', 0))
            if any(pattern in name for pattern in action_patterns):
                first_action_chunk_idx = chunk_idx
                break
        eid_offset = first_action_chunk_idx - 10 if first_action_chunk_idx else 0

        for chunk in self.root.findall('.//chunk'):
            name = chunk.get('name', '')
            chunk_idx = int(chunk.get('chunkIndex', 0))
            timestamp = int(chunk.get('timestamp', 0))

            if name == 'ID3DUserDefinedAnnotation::BeginEvent':
                marker_elem = chunk.find('.//string[@name="MarkerName"]')
                if marker_elem is not None and marker_elem.text:
                    marker_stack.append(marker_elem.text)

            elif name == 'ID3DUserDefinedAnnotation::EndEvent':
                if marker_stack:
                    marker_stack.pop()

            elif name == 'ID3D11DeviceContext::VSSetShader':
                shader_elem = chunk.find('.//ResourceId[@name="pShader"]')
                if shader_elem is not None:
                    current_vs = shader_elem.text

            elif name == 'ID3D11DeviceContext::PSSetShader':
                shader_elem = chunk.find('.//ResourceId[@name="pShader"]')
                if shader_elem is not None:
                    current_ps = shader_elem.text

            elif name == 'ID3D11DeviceContext::PSSetShaderResources':
                start_slot_elem = chunk.find('.//*[@name="StartSlot"]')
                views = chunk.find('.//array[@name="ppShaderResourceViews"]')
                if start_slot_elem is None or views is None or not start_slot_elem.text:
                    continue
                try:
                    start_slot = int(start_slot_elem.text)
                except Exception:
                    continue
                for idx, view in enumerate(list(views)):
                    if view.text:
                        ps_srvs[start_slot + idx] = view.text

            elif name == 'ID3D11DeviceContext::VSSetShaderResources':
                start_slot_elem = chunk.find('.//*[@name="StartSlot"]')
                views = chunk.find('.//array[@name="ppShaderResourceViews"]')
                if start_slot_elem is None or views is None or not start_slot_elem.text:
                    continue
                try:
                    start_slot = int(start_slot_elem.text)
                except Exception:
                    continue
                for idx, view in enumerate(list(views)):
                    if view.text:
                        vs_srvs[start_slot + idx] = view.text

            elif name == 'ID3D11DeviceContext::OMSetRenderTargets':
                current_rtvs = []
                views = chunk.find('.//array[@name="ppRenderTargetViews"]')
                if views is not None:
                    for view in list(views):
                        if view.text:
                            current_rtvs.append(view.text)

            elif name in ('ID3D11DeviceContext::DrawIndexed', 'ID3D11DeviceContext::Draw'):
                eid = str(chunk_idx - eid_offset)
                marker_name = marker_stack[-1] if marker_stack else name
                drawcall = DrawCall(
                    index=draw_index,
                    chunk_index=chunk_idx,
                    eid=eid,
                    name=marker_name,
                    timestamp=timestamp,
                    fbo="|".join(current_rtvs),
                )

                count_elem = chunk.find('.//*[@name="IndexCount"]')
                if count_elem is None:
                    count_elem = chunk.find('.//*[@name="VertexCount"]')
                if count_elem is not None and count_elem.text:
                    try:
                        drawcall.vertex_count = int(count_elem.text)
                    except Exception:
                        pass
                inst_elem = chunk.find('.//*[@name="InstanceCount"]')
                if inst_elem is not None and inst_elem.text:
                    try:
                        drawcall.instance_count = int(inst_elem.text)
                    except Exception:
                        pass

                if drawcall.vertex_count > 0:
                    drawcall.primitive_count = drawcall.vertex_count // 3

                shader_key = f"vs:{current_vs or '0'}|ps:{current_ps or '0'}"
                if shader_key not in combined_shader_cache:
                    shader_info = ShaderInfo(resource_id=shader_key)
                    if current_vs and current_vs in shader_db:
                        shader_info.vertex_id = current_vs
                        shader_info.vertex_md5 = shader_db[current_vs].get('md5', '')
                        shader_info.vertex_source = shader_db[current_vs].get('source', '')
                    if current_ps and current_ps in shader_db:
                        shader_info.fragment_id = current_ps
                        shader_info.fragment_md5 = shader_db[current_ps].get('md5', '')
                        shader_info.fragment_source = shader_db[current_ps].get('source', '')
                    combined_shader_cache[shader_key] = shader_info
                drawcall.shader_program = combined_shader_cache[shader_key]

                for _, srv_id in sorted(ps_srvs.items()):
                    if srv_id and srv_id in textures:
                        drawcall.bound_textures.append(textures[srv_id])
                for _, srv_id in sorted(vs_srvs.items()):
                    if srv_id and srv_id in textures:
                        drawcall.bound_textures.append(textures[srv_id])

                tex_md5s = sorted([t.md5 for t in drawcall.bound_textures if t.md5])
                state_components = [
                    shader_key,
                    drawcall.shader_program.vertex_md5 if drawcall.shader_program else "",
                    drawcall.shader_program.fragment_md5 if drawcall.shader_program else "",
                    "|".join(tex_md5s),
                ]
                drawcall.state_hash = hashlib.md5("|".join(state_components).encode()).hexdigest()

                drawcalls.append(drawcall)
                draw_index += 1

        return drawcalls
        
    def extract_shaders(self) -> Dict[str, ShaderInfo]:
        """Extract all shader programs"""
        shaders = {}
        shader_types = {}
        shader_sources = {}
        shader_to_program = {}
        program_shaders = defaultdict(dict)
        
        for chunk in self.root.findall('.//chunk'):
            name = chunk.get('name', '')
            
            if 'glCreateShader' in name:
                shader_id = None
                shader_type = None
                for elem in chunk:
                    elem_name = elem.get('name', '')
                    if elem_name == 'Shader':
                        shader_id = elem.text
                    elif 'type' in elem_name:
                        shader_type = elem.get('string', '')
                        
                if shader_id and shader_type:
                    shader_types[shader_id] = shader_type
                    
            elif 'glShaderSource' in name:
                shader_id = None
                source = ""
                for elem in chunk:
                    elem_name = elem.get('name', '')
                    if elem_name == 'shader':
                        shader_id = elem.text
                    elif elem_name == 'sources':
                        for child in elem:
                            if child.text:
                                source = child.text
                                break
                                
                if shader_id and source:
                    shader_sources[shader_id] = source
                    
            elif 'glAttachShader' in name:
                program_id = None
                shader_id = None
                for elem in chunk:
                    elem_name = elem.get('name', '')
                    if elem_name == 'program':
                        program_id = elem.text
                    elif elem_name == 'shader':
                        shader_id = elem.text
                        
                if program_id and shader_id:
                    shader_to_program[shader_id] = program_id
                    
        # Build program associations
        for shader_id, program_id in shader_to_program.items():
            if shader_id in shader_types:
                shader_type = shader_types[shader_id]
                if 'VERTEX' in shader_type:
                    program_shaders[program_id]['vertex_id'] = shader_id
                elif 'FRAGMENT' in shader_type:
                    program_shaders[program_id]['fragment_id'] = shader_id
                    
        # Create ShaderInfo objects
        for program_id, shader_ids in program_shaders.items():
            shader_info = ShaderInfo(resource_id=program_id)
            
            if 'vertex_id' in shader_ids:
                shader_info.vertex_id = shader_ids['vertex_id']
                shader_info.vertex_source = shader_sources.get(shader_ids['vertex_id'], "")
                if shader_info.vertex_source:
                    shader_info.vertex_md5 = hashlib.md5(shader_info.vertex_source.encode()).hexdigest()
                    
            if 'fragment_id' in shader_ids:
                shader_info.fragment_id = shader_ids['fragment_id']
                shader_info.fragment_source = shader_sources.get(shader_ids['fragment_id'], "")
                if shader_info.fragment_source:
                    shader_info.fragment_md5 = hashlib.md5(shader_info.fragment_source.encode()).hexdigest()
                    
            if shader_info.vertex_source or shader_info.fragment_source:
                shaders[program_id] = shader_info
                
        return shaders
        
    def _find_malioc(self) -> Optional[Path]:
        """Find Mali Offline Compiler executable (cross-platform)"""
        # User-specified override takes highest priority
        if self.malioc_override and self.malioc_override.exists():
            if self.verbose:
                print(f"    [malioc] Using user-specified: {self.malioc_override}")
            if platform.system() != "Windows":
                self._ensure_executable(self.malioc_override)
            return self.malioc_override
        
        is_windows = platform.system() == "Windows"
        platform_dir = "windows" if is_windows else "linux"
        exe_name = "malioc.exe" if is_windows else "malioc"

        script_dir = Path(__file__).resolve().parent
        # Check tools/mali_offline_compiler/<platform>/
        candidate = script_dir / "tools" / "mali_offline_compiler" / platform_dir / exe_name
        if candidate.exists():
            if not is_windows:
                self._ensure_executable(candidate)
            if self.verbose:
                print(f"    [malioc] Found at: {candidate}")
            return candidate
        elif self.verbose:
            print(f"    [malioc] Not found at: {candidate}")
        # Fallback: tools/mali_offline_compiler/ root (legacy layout)
        candidate = script_dir / "tools" / "mali_offline_compiler" / exe_name
        if candidate.exists():
            if not is_windows:
                self._ensure_executable(candidate)
            if self.verbose:
                print(f"    [malioc] Found at (legacy): {candidate}")
            return candidate
        elif self.verbose:
            print(f"    [malioc] Not found at (legacy): {candidate}")
        # Check PATH
        result = shutil.which("malioc") or shutil.which(exe_name)
        if result:
            if self.verbose:
                print(f"    [malioc] Found in PATH: {result}")
            return Path(result)
        if self.verbose:
            print(f"    [malioc] WARNING: malioc not found anywhere!")
        return None
    
    @staticmethod
    def _ensure_executable(filepath: Path):
        """Ensure a file has executable permission on Linux/macOS"""
        import stat
        current = filepath.stat().st_mode
        if not (current & stat.S_IXUSR):
            try:
                filepath.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass  # Best effort; may fail on read-only filesystems
        # Also fix permissions for glslang in external/ directory (malioc dependency)
        external_dir = filepath.parent / "external"
        if external_dir.exists():
            for child in external_dir.iterdir():
                if child.is_file():
                    child_mode = child.stat().st_mode
                    if not (child_mode & stat.S_IXUSR):
                        try:
                            child.chmod(child_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                        except OSError:
                            pass

    def analyze_shader_with_mali(self, source: str, shader_type: str) -> Optional[ShaderComplexity]:
        """Analyze shader using Mali offline compiler with proper shader type"""
        mali_compiler = self._find_malioc()
        if not mali_compiler:
            return None
            
        try:
            # Save shader to temp file
            suffix = '.vert' if shader_type == 'vertex' else '.frag'
            with tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False, encoding='utf-8') as f:
                shader_code = source
                
                # Ensure version directive
                if '#version' not in shader_code:
                    shader_code = '#version 320 es\n' + shader_code
                if shader_type == 'fragment' and 'precision' not in shader_code.lower():
                    shader_code = shader_code.replace('#version 320 es\n', 
                                                      '#version 320 es\nprecision highp float;\n')
                    
                f.write(shader_code)
                shader_file = f.name
                
            # Run Mali compiler with correct flag
            shader_flag = '--vertex' if shader_type == 'vertex' else '--fragment'
            env = os.environ.copy()
            mali_dir = mali_compiler.parent
            if platform.system() != "Windows":
                # malioc on Linux needs graphics/ libs in LD_LIBRARY_PATH
                graphics_dir = mali_dir / "graphics"
                ld_path = env.get("LD_LIBRARY_PATH", "")
                extra = f"{mali_dir}:{graphics_dir}" if graphics_dir.exists() else str(mali_dir)
                env["LD_LIBRARY_PATH"] = f"{extra}:{ld_path}" if ld_path else extra
            result = subprocess.run(
                [str(mali_compiler), shader_flag, shader_file],
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
                encoding='utf-8',
                errors='replace'
            )
            
            output = result.stdout + result.stderr
            
            if self.verbose and result.returncode != 0:
                print(f"    [malioc] FAILED ({shader_type}) returncode={result.returncode}")
                # Print first 5 lines of shader for context
                first_lines = shader_code.split('\n')[:5]
                print(f"    [malioc] Shader starts with: {first_lines}")
                print(f"    [malioc] stdout: {result.stdout[:500]}")
                print(f"    [malioc] stderr: {result.stderr[:500]}")
            
            complexity = ShaderComplexity()
            
            # Parse Mali output
            # Work registers: 64 (100% used at 50% occupancy)
            work_reg_match = re.search(r'Work registers:\s*(\d+)', output)
            if work_reg_match:
                complexity.work_registers = int(work_reg_match.group(1))
                
            # Uniform registers: 128 (100% used)
            uniform_reg_match = re.search(r'Uniform registers:\s*(\d+)', output)
            if uniform_reg_match:
                complexity.uniform_registers = int(uniform_reg_match.group(1))
                
            # 16-bit arithmetic: 64%
            arith_16_match = re.search(r'16-bit arithmetic:\s*(\d+)%', output)
            if arith_16_match:
                complexity.arithmetic_16bit = float(arith_16_match.group(1))
                
            # Total instruction cycles:    6.36    9.00    0.69    2.00       LS
            cycles_match = re.search(r'Total instruction cycles:\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\w+)', output)
            if cycles_match:
                complexity.alu_cycles = float(cycles_match.group(1))
                complexity.ls_cycles = float(cycles_match.group(2))
                complexity.varying_cycles = float(cycles_match.group(3))
                complexity.texture_cycles = float(cycles_match.group(4))
                complexity.bound_unit = cycles_match.group(5)
                complexity.total_cycles = max(complexity.alu_cycles, complexity.ls_cycles,
                                             complexity.varying_cycles, complexity.texture_cycles)
            
            os.unlink(shader_file)
            return complexity
            
        except Exception as e:
            if self.verbose:
                print(f"    [malioc] Exception analyzing {shader_type} shader: {e}")
            return None
        
    def extract_drawcalls(self, textures: Dict, shaders: Dict) -> List[DrawCall]:
        """Extract all draw calls"""
        drawcalls = []
        draw_index = 0
        
        current_program = None
        current_fbo = "0"
        active_texture_unit = 0
        texture_unit_bindings = defaultdict(str)
        
        # Simulate chunkIndex if missing in XML
        # IMPORTANT FIX: Some RDC exports don't include chunkIndex attribute in <chunk> elements
        # This causes incorrect EID (Event ID) calculation which breaks draw call matching
        # Solution: Detect if chunkIndex is present, and if not, simulate sequential indices (0, 1, 2, ...)
        # The simulated indices maintain proper ordering and enable correct EID calculation
        simulated_chunk_index = 0
        chunk_has_index = False
        
        # Check if XML has chunkIndex attributes
        first_chunk = self.root.find('.//chunk')
        if first_chunk is not None and first_chunk.get('chunkIndex') is not None:
            chunk_has_index = True
        else:
            print("    [WARN] XML missing chunkIndex attributes - simulating indices for EID calculation")
        
        # Calculate EID offset: EID = chunkIndex - offset
        # The offset is the chunkIndex where RenderDoc starts assigning EIDs
        # Find the first real action (draw/clear) to calibrate
        action_patterns = ['glDrawArrays', 'glDrawElements', 'glDrawRangeElements', 
                          'glClearBuffer', 'glDispatch', 'glBlit', 'glCopy', 'glResolve']
        
        # From RenderDoc export analysis, the first action (glClearBufferiv) is typically around EID 10
        # and appears after ~4949 initialization chunks
        # We'll find the first action and calculate offset from there
        
        first_action_chunk_idx = None
        simulated_chunk_index = 0  # Reset for first pass
        for chunk in self.root.findall('.//chunk'):
            name = chunk.get('name', '')
            
            # Get chunk index (real or simulated)
            if chunk_has_index:
                current_chunk_idx = int(chunk.get('chunkIndex', 0))
            else:
                current_chunk_idx = simulated_chunk_index
                simulated_chunk_index += 1
            
            if any(pattern in name for pattern in action_patterns):
                first_action_chunk_idx = current_chunk_idx
                break
        
        # Simple heuristic: EID 1-9 are typically initialization/setup
        # First action is typically EID 10
        # So: offset = first_action_chunk_idx - 10
        if first_action_chunk_idx:
            eid_offset = first_action_chunk_idx - 10
        else:
            eid_offset = 0
        
        # Now extract draw calls
        simulated_chunk_index = 0  # Reset for second pass
        for chunk in self.root.findall('.//chunk'):
            name = chunk.get('name', '')
            
            # Get chunk index (real or simulated)
            if chunk_has_index:
                chunk_idx = int(chunk.get('chunkIndex', 0))
            else:
                chunk_idx = simulated_chunk_index
                simulated_chunk_index += 1
            
            timestamp = int(chunk.get('timestamp', 0))
            
            if 'glUseProgram' in name:
                for elem in chunk:
                    if elem.get('name') == 'program':
                        current_program = elem.text
                        
            elif 'glActiveTexture' in name:
                for elem in chunk:
                    if 'texture' in elem.get('name', ''):
                        unit_str = elem.get('string', '')
                        if 'TEXTURE' in unit_str:
                            try:
                                active_texture_unit = int(unit_str.replace('GL_TEXTURE', ''))
                            except:
                                pass
                                
            elif 'glBindTexture' in name:
                tex_id = None
                for elem in chunk:
                    if elem.get('name') == 'texture':
                        tex_id = elem.text
                if tex_id:
                    texture_unit_bindings[active_texture_unit] = tex_id
                    
            elif 'glBindFramebuffer' in name:
                for elem in chunk:
                    if 'framebuffer' in elem.get('name', ''):
                        current_fbo = elem.text
                        
            elif any(cmd in name for cmd in ['glDrawArrays', 'glDrawElements']):
                # Calculate EID using the offset
                eid = str(chunk_idx - eid_offset)
                
                drawcall = DrawCall(
                    index=draw_index,
                    chunk_index=chunk_idx,
                    eid=eid,
                    name=name,
                    timestamp=timestamp,
                    fbo=current_fbo
                )
                
                for elem in chunk:
                    elem_name = elem.get('name', '')
                    if 'count' in elem_name and 'instance' not in elem_name:
                        try:
                            drawcall.vertex_count = int(elem.text)
                        except:
                            pass
                    elif 'instancecount' in elem_name.lower() or 'primcount' in elem_name.lower():
                        try:
                            drawcall.instance_count = int(elem.text)
                        except:
                            pass
                            
                if drawcall.vertex_count > 0:
                    drawcall.primitive_count = drawcall.vertex_count // 3
                    
                if current_program and current_program in shaders:
                    drawcall.shader_program = shaders[current_program]
                    
                for unit, tex_id in texture_unit_bindings.items():
                    if tex_id and tex_id != "0" and tex_id in textures:
                        drawcall.bound_textures.append(textures[tex_id])
                        
                tex_md5s = sorted([t.md5 for t in drawcall.bound_textures if t.md5])
                state_components = [
                    current_program or "",
                    drawcall.shader_program.vertex_md5 if drawcall.shader_program else "",
                    drawcall.shader_program.fragment_md5 if drawcall.shader_program else "",
                    "|".join(tex_md5s),
                ]
                drawcall.state_hash = hashlib.md5("|".join(state_components).encode()).hexdigest()
                
                drawcalls.append(drawcall)
                draw_index += 1
                
        return drawcalls
        
    def analyze(self) -> CaptureData:
        """Perform complete analysis"""
        print(f"\nAnalyzing {self.name} capture...")
        
        data = CaptureData(xml_path=self.xml_path, zip_path=self.zip_path)
        
        data.driver, data.machine_ident = self.extract_header_info()
        print(f"  Driver: {data.driver}, Machine: {data.machine_ident}")
        
        if self.is_vulkan:
            print("  [Vulkan mode]")
            
            print("  Extracting textures...")
            data.textures = self.extract_textures_vulkan()
            astc_count = sum(1 for t in data.textures.values() if t.is_astc)
            print(f"    Found {len(data.textures)} textures ({astc_count} ASTC)")
            
            print("  Extracting shaders...")
            data.shaders = self.extract_shaders_vulkan()
            print(f"    Found {len(data.shaders)} shader programs (pipelines)")
            
            print("  Extracting draw calls...")
            data.drawcalls = self.extract_drawcalls_vulkan(data.textures, data.shaders)
            print(f"    Found {len(data.drawcalls)} draw calls")
        elif self.is_d3d11:
            print("  [D3D11 mode]")

            print("  Extracting textures...")
            data.textures = self.extract_textures_d3d11()
            print(f"    Found {len(data.textures)} textures")

            print("  Extracting shaders...")
            shader_db = self.extract_shaders_d3d11()
            print(f"    Found {len(shader_db)} shader stages")

            print("  Extracting draw calls...")
            data.drawcalls = self.extract_drawcalls_d3d11(data.textures, shader_db)
            print(f"    Found {len(data.drawcalls)} draw calls")
        else:
            print("  [OpenGL mode]")
            
            print("  Extracting textures...")
            data.textures = self.extract_textures()
            astc_count = sum(1 for t in data.textures.values() if t.is_astc)
            print(f"    Found {len(data.textures)} textures ({astc_count} ASTC)")
            
            print("  Extracting shaders...")
            data.shaders = self.extract_shaders()
            print(f"    Found {len(data.shaders)} shader programs")
            
            print("  Analyzing shader complexity with Mali compiler...")
            vertex_analyzed = 0
            fragment_analyzed = 0
            for shader in data.shaders.values():
                if shader.vertex_source:
                    complexity = self.analyze_shader_with_mali(shader.vertex_source, 'vertex')
                    if complexity:
                        shader.vertex_complexity = complexity
                        vertex_analyzed += 1
                        
                if shader.fragment_source:
                    complexity = self.analyze_shader_with_mali(shader.fragment_source, 'fragment')
                    if complexity:
                        shader.fragment_complexity = complexity
                        fragment_analyzed += 1
                        
            print(f"    Analyzed {vertex_analyzed} vertex shaders, {fragment_analyzed} fragment shaders")
            
            print("  Extracting draw calls...")
            data.drawcalls = self.extract_drawcalls(data.textures, data.shaders)
            print(f"    Found {len(data.drawcalls)} draw calls")
        
        data.total_primitives = sum(dc.primitive_count for dc in data.drawcalls)
        data.total_vertices = sum(dc.vertex_count for dc in data.drawcalls)
        
        return data


class HTMLReportGenerator:
    """Generates HTML report with embedded images"""
    
    def __init__(self, base_data: CaptureData, new_data: CaptureData, 
                 output_dir: str, strict_mode: bool):
        self.base = base_data
        self.new = new_data
        self.output_dir = Path(output_dir)
        self.strict_mode = strict_mode
        self.html_lines = []
        
    def add_html(self, content: str):
        """Add HTML content"""
        self.html_lines.append(content)
        
    def generate_header(self):
        """Generate HTML header"""
        self.add_html("""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>RenderDoc Capture Comparison Report</title>""")
        
        self.add_html("""
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
        }
        h1 { margin: 0 0 10px 0; }
        h2 {
            background: #667eea;
            color: white;
            padding: 15px;
            border-radius: 5px;
            margin-top: 30px;
        }
        h3 {
            color: #667eea;
            border-bottom: 2px solid #667eea;
            padding-bottom: 5px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            background: white;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin: 20px 0;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        th {
            background: #667eea;
            color: white;
            font-weight: bold;
        }
        tr:hover {
            background: #f5f5f5;
        }
        .metric-positive { color: #27ae60; font-weight: bold; }
        .metric-negative { color: #e74c3c; font-weight: bold; }
        .metric-neutral { color: #95a5a6; }
        .drawcall-card {
            background: white;
            padding: 20px;
            margin: 15px 0;
            border-radius: 5px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .texture-grid {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin: 10px 0;
        }
        .texture-item {
            text-align: center;
        }
        .texture-item img {
            border: 2px solid #ddd;
            border-radius: 5px;
            max-width: 128px;
            max-height: 128px;
        }
        .texture-item span {
            display: block;
            font-size: 11px;
            color: #666;
            margin-top: 5px;
        }
        .shader-comparison {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin: 20px 0;
        }
        .shader-box {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            border-left: 4px solid #667eea;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin: 20px 0;
        }
        .stat-card {
            background: white;
            padding: 20px;
            border-radius: 5px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            text-align: center;
        }
        .stat-value {
            font-size: 32px;
            font-weight: bold;
            color: #667eea;
        }
        .stat-label {
            color: #666;
            margin-top: 5px;
        }
        .similarity-badge {
            display: inline-block;
            padding: 5px 10px;
            border-radius: 15px;
            font-weight: bold;
            font-size: 14px;
        }
        .similarity-perfect { background: #27ae60; color: white; }
        .similarity-good { background: #3498db; color: white; }
        .similarity-partial { background: #f39c12; color: white; }
        code {
            background: #f4f4f4;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
        }
    </style>
</head>
<body>""")
        
        self.add_html(f"""
<div class="header">
    <h1>🎮 RenderDoc Capture Comparison Report</h1>
    <p><strong>Mode:</strong> {'Strict' if self.strict_mode else 'Loose'} Comparison</p>
    <p><strong>Base:</strong> {Path(self.base.xml_path).stem} | <strong>New:</strong> {Path(self.new.xml_path).stem}</p>
    <p><strong>Generated:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</div>
""")
        
    def generate_overall_stats(self):
        """Generate overall statistics section"""
        self.add_html("<h2>📊 Overall Statistics</h2>")
        
        self.add_html('<div class="stats-grid">')
        
        metrics = [
            ("Draw Calls", len(self.base.drawcalls), len(self.new.drawcalls)),
            ("Primitives", self.base.total_primitives, self.new.total_primitives),
            ("Vertices", self.base.total_vertices, self.new.total_vertices),
            ("Textures", len(self.base.textures), len(self.new.textures)),
            ("Shaders", len(self.base.shaders), len(self.new.shaders)),
        ]
        
        for name, base_val, new_val in metrics:
            change = new_val - base_val
            change_pct = (change / base_val * 100) if base_val > 0 else 0
            
            if change > 0:
                color_class = "metric-negative" if "Draw" in name or "Primitives" in name else "metric-positive"
                change_str = f"+{change} (+{change_pct:.1f}%)"
            elif change < 0:
                color_class = "metric-positive" if "Draw" in name or "Primitives" in name else "metric-negative"
                change_str = f"{change} ({change_pct:.1f}%)"
            else:
                color_class = "metric-neutral"
                change_str = "No change"
                
            self.add_html(f"""
<div class="stat-card">
    <div class="stat-value">{new_val:,}</div>
    <div class="stat-label">{name}</div>
    <div class="{color_class}">{change_str}</div>
</div>
""")
                
        self.add_html('</div>')
        
        # Detailed table
        self.add_html("""
<table>
    <tr>
        <th>Metric</th>
        <th>Base</th>
        <th>New</th>
        <th>Change</th>
    </tr>
""")
        
        for name, base_val, new_val in metrics:
            change = new_val - base_val
            change_pct = (change / base_val * 100) if base_val > 0 else 0
            
            if change > 0:
                change_str = f'<span class="metric-negative">+{change} (+{change_pct:.1f}%)</span>'
            elif change < 0:
                change_str = f'<span class="metric-positive">{change} ({change_pct:.1f}%)</span>'
            else:
                change_str = '<span class="metric-neutral">0 (0.0%)</span>'
                
            self.add_html(f"""
    <tr>
        <td><strong>{name}</strong></td>
        <td>{base_val:,}</td>
        <td>{new_val:,}</td>
        <td>{change_str}</td>
    </tr>
""")
            
        self.add_html("</table>")
        
    def compute_drawcall_similarity(self, dc1: DrawCall, dc2: DrawCall) -> float:
        """
        Compute similarity score with geometry-first prioritization.
        
        Scoring weights (total = 1.0):
        - Primitive Count (50%): Must match geometry to be same object
        - Shader MD5 (30%): Same rendering logic
        - Texture MD5 (15%): Same texture data
        - Draw Call Name (5%): Same GL function
        """
        score = 0.0
        
        # HIGHEST PRIORITY: Primitive Count (50%)
        # If geometry differs significantly, it's likely a different object!
        if dc1.primitive_count > 0 and dc2.primitive_count > 0:
            ratio = min(dc1.primitive_count, dc2.primitive_count) / max(dc1.primitive_count, dc2.primitive_count)
            score += ratio * 0.5
        elif dc1.primitive_count == dc2.primitive_count:
            score += 0.5
        
        # SECOND PRIORITY: Shader Match (30%)
        if dc1.shader_program and dc2.shader_program:
            if (dc1.shader_program.vertex_md5 == dc2.shader_program.vertex_md5 and
                dc1.shader_program.fragment_md5 == dc2.shader_program.fragment_md5):
                score += 0.3
            else:
                score += 0.05  # Partial credit if both have shaders but different
        elif not self.strict_mode and (dc1.shader_program is None) == (dc2.shader_program is None):
            score += 0.1
            
        # THIRD PRIORITY: Texture Match (15%)
        tex1_md5s = set(t.md5 for t in dc1.bound_textures if t.md5)
        tex2_md5s = set(t.md5 for t in dc2.bound_textures if t.md5)
        if tex1_md5s or tex2_md5s:
            matching = len(tex1_md5s & tex2_md5s)
            total = max(len(tex1_md5s), len(tex2_md5s))
            score += (matching / total) * 0.15 if total > 0 else 0
        else:
            score += 0.15
            
        # LOWEST PRIORITY: Draw Call Name (5%)
        if dc1.name == dc2.name:
            score += 0.05
            
        return score
        
    def match_drawcalls(self) -> List[Tuple[Optional[DrawCall], Optional[DrawCall], float]]:
        """Match draw calls"""
        matches = []
        used_new = set()
        threshold = 0.8 if self.strict_mode else 0.6
        
        for base_dc in self.base.drawcalls:
            best_match = None
            best_score = 0.0
            
            for new_idx, new_dc in enumerate(self.new.drawcalls):
                if new_idx in used_new:
                    continue
                    
                score = self.compute_drawcall_similarity(base_dc, new_dc)
                
                if score > best_score and score >= threshold:
                    best_score = score
                    best_match = new_idx
                    
            if best_match is not None:
                used_new.add(best_match)
                matches.append((base_dc, self.new.drawcalls[best_match], best_score))
            else:
                matches.append((base_dc, None, 0.0))
                
        for new_idx, new_dc in enumerate(self.new.drawcalls):
            if new_idx not in used_new:
                matches.append((None, new_dc, 0.0))
                
        return matches
    
    def render_drawcall_details(self, base_dc, new_dc):
        """Helper method to render draw call details with textures and 3D geometry"""
        # Base draw call
        if base_dc:
            self.add_html(f"""
<p><strong>Base:</strong> {base_dc.name} <code>EID {base_dc.eid}</code> <code>Chunk {base_dc.chunk_index}</code></p>
<p>Primitives: {base_dc.primitive_count:,} | Vertices: {base_dc.vertex_count:,} | Instance: {base_dc.instance_count}</p>
""")
            
            if base_dc.bound_textures:
                self.add_html(f'<p><strong>Textures ({len(base_dc.bound_textures)}):</strong></p><div class="texture-grid">')
                for tex in base_dc.bound_textures:
                    if tex.thumbnail_base64:
                        self.add_html(f"""
<div class="texture-item">
    <img src="{tex.thumbnail_base64}" alt="Texture {tex.resource_id}">
    <span>{tex.width}x{tex.height}<br>{tex.format[:25]}</span>
</div>
""")
                    else:
                        self.add_html(f"""
<div class="texture-item">
    <div style="width:128px;height:128px;background:#ddd;border:2px solid #999;border-radius:5px;display:flex;align-items:center;justify-content:center;color:#666;font-size:12px;">No Preview</div>
    <span>{tex.width}x{tex.height}<br>{tex.format[:25]}</span>
</div>
""")
                self.add_html('</div>')
        
        # New draw call
        if new_dc:
            self.add_html(f"""
<p><strong>New:</strong> {new_dc.name} <code>EID {new_dc.eid}</code> <code>Chunk {new_dc.chunk_index}</code></p>
<p>Primitives: {new_dc.primitive_count:,} | Vertices: {new_dc.vertex_count:,} | Instance: {new_dc.instance_count}</p>
""")
            
            if new_dc.bound_textures:
                self.add_html(f'<p><strong>Textures ({len(new_dc.bound_textures)}):</strong></p><div class="texture-grid">')
                for tex in new_dc.bound_textures:
                    if tex.thumbnail_base64:
                        self.add_html(f"""
<div class="texture-item">
    <img src="{tex.thumbnail_base64}" alt="Texture {tex.resource_id}">
    <span>{tex.width}x{tex.height}<br>{tex.format[:25]}</span>
</div>
""")
                    else:
                        self.add_html(f"""
<div class="texture-item">
    <div style="width:128px;height:128px;background:#ddd;border:2px solid #999;border-radius:5px;display:flex;align-items:center;justify-content:center;color:#666;font-size:12px;">No Preview</div>
    <span>{tex.width}x{tex.height}<br>{tex.format[:25]}</span>
</div>
""")
                self.add_html('</div>')
        
    def render_shader_stats(self, base_dc, new_dc):
        """Helper method to render shader performance comparison"""
        if not base_dc or not new_dc or not base_dc.shader_program or not new_dc.shader_program:
            # Handle single-sided cases (Added/Removed)
            if base_dc and base_dc.shader_program:
                base_frag = base_dc.shader_program.fragment_complexity
                if base_frag:
                    self.add_html('<div class="shader-comparison">')
                    self.add_html(f"""
<div class="shader-box">
    <h4>Base Shader Performance</h4>
    <p><strong>Cycles:</strong> A={base_frag.alu_cycles:.2f} | LS={base_frag.ls_cycles:.2f} | V={base_frag.varying_cycles:.2f} | T={base_frag.texture_cycles:.2f}</p>
    <p><strong>Total:</strong> {base_frag.total_cycles:.2f} cycles | <strong>Bound:</strong> {base_frag.bound_unit}</p>
    <p><strong>Registers:</strong> Work={base_frag.work_registers} | Uniform={base_frag.uniform_registers} | <strong>16-bit:</strong> {base_frag.arithmetic_16bit:.1f}%</p>
</div>
""")
                    self.add_html('</div>')
            elif new_dc and new_dc.shader_program:
                new_frag = new_dc.shader_program.fragment_complexity
                if new_frag:
                    self.add_html('<div class="shader-comparison">')
                    self.add_html(f"""
<div class="shader-box">
    <h4>New Shader Performance</h4>
    <p><strong>Cycles:</strong> A={new_frag.alu_cycles:.2f} | LS={new_frag.ls_cycles:.2f} | V={new_frag.varying_cycles:.2f} | T={new_frag.texture_cycles:.2f}</p>
    <p><strong>Total:</strong> {new_frag.total_cycles:.2f} cycles | <strong>Bound:</strong> {new_frag.bound_unit}</p>
    <p><strong>Registers:</strong> Work={new_frag.work_registers} | Uniform={new_frag.uniform_registers} | <strong>16-bit:</strong> {new_frag.arithmetic_16bit:.1f}%</p>
</div>
""")
                    self.add_html('</div>')
            return
        
        # Both sides exist - show comparison
        base_frag = base_dc.shader_program.fragment_complexity
        new_frag = new_dc.shader_program.fragment_complexity
        
        if base_frag or new_frag:
            self.add_html('<div class="shader-comparison">')
            
            # Base shader stats
            if base_frag:
                self.add_html(f"""
<div class="shader-box">
    <h4>Base Shader Performance</h4>
    <p><strong>Cycles:</strong> A={base_frag.alu_cycles:.2f} | LS={base_frag.ls_cycles:.2f} | V={base_frag.varying_cycles:.2f} | T={base_frag.texture_cycles:.2f}</p>
    <p><strong>Total:</strong> {base_frag.total_cycles:.2f} cycles | <strong>Bound:</strong> {base_frag.bound_unit}</p>
    <p><strong>Registers:</strong> Work={base_frag.work_registers} | Uniform={base_frag.uniform_registers} | <strong>16-bit:</strong> {base_frag.arithmetic_16bit:.1f}%</p>
</div>
""")
            else:
                self.add_html('<div class="shader-box"><p>Base: No complexity data</p></div>')
            
            # New shader stats with comparison
            if new_frag:
                # Calculate performance change
                if base_frag:
                    cycle_diff = new_frag.total_cycles - base_frag.total_cycles
                    cycle_pct = (cycle_diff / base_frag.total_cycles * 100) if base_frag.total_cycles > 0 else 0
                    
                    if cycle_diff < 0:
                        perf_indicator = f'<span class="metric-positive">▼ {abs(cycle_diff):.2f} cycles ({abs(cycle_pct):.1f}% faster)</span>'
                    elif cycle_diff > 0:
                        perf_indicator = f'<span class="metric-negative">▲ {cycle_diff:.2f} cycles ({cycle_pct:.1f}% slower)</span>'
                    else:
                        perf_indicator = '<span class="metric-neutral">No change</span>'
                else:
                    perf_indicator = ''
                
                self.add_html(f"""
<div class="shader-box">
    <h4>New Shader Performance {perf_indicator}</h4>
    <p><strong>Cycles:</strong> A={new_frag.alu_cycles:.2f} | LS={new_frag.ls_cycles:.2f} | V={new_frag.varying_cycles:.2f} | T={new_frag.texture_cycles:.2f}</p>
    <p><strong>Total:</strong> {new_frag.total_cycles:.2f} cycles | <strong>Bound:</strong> {new_frag.bound_unit}</p>
    <p><strong>Registers:</strong> Work={new_frag.work_registers} | Uniform={new_frag.uniform_registers} | <strong>16-bit:</strong> {new_frag.arithmetic_16bit:.1f}%</p>
</div>
""")
            else:
                self.add_html('<div class="shader-box"><p>New: No complexity data</p></div>')
            
            self.add_html('</div>')
    
    def get_shader_performance_delta(self, base_dc: Optional[DrawCall], new_dc: Optional[DrawCall]) -> float:
        """
        Calculate shader performance delta (new - base total cycles).
        Returns positive value for slowdowns (higher cycles), negative for speedups.
        Returns -inf if no shader data available (will be sorted last).
        """
        if not base_dc or not new_dc:
            return float('-inf')
        
        if not base_dc.shader_program or not new_dc.shader_program:
            return float('-inf')
        
        base_shader = base_dc.shader_program
        new_shader = new_dc.shader_program
        
        # Get fragment shader cycles (more impactful than vertex)
        base_cycles = 0.0
        new_cycles = 0.0
        
        if base_shader.fragment_complexity:
            base_cycles = base_shader.fragment_complexity.total_cycles
        if new_shader.fragment_complexity:
            new_cycles = new_shader.fragment_complexity.total_cycles
        
        # If no fragment shader data, try vertex shader
        if base_cycles == 0.0 and base_shader.vertex_complexity:
            base_cycles = base_shader.vertex_complexity.total_cycles
        if new_cycles == 0.0 and new_shader.vertex_complexity:
            new_cycles = new_shader.vertex_complexity.total_cycles
        
        # If still no data, return -inf
        if base_cycles == 0.0 and new_cycles == 0.0:
            return float('-inf')
        
        # Return delta (positive = slowdown, negative = speedup)
        return new_cycles - base_cycles
    
    def sort_by_performance_then_vertices(self, matches: List[Tuple[Optional[DrawCall], Optional[DrawCall], float]]) -> List[Tuple[Optional[DrawCall], Optional[DrawCall], float]]:
        """
        Sort matches by shader performance delta (slowdowns first), then by vertex count.
        """
        def sort_key(match):
            base_dc, new_dc, score = match
            
            # Primary: Shader performance delta (descending, slowdowns first)
            perf_delta = self.get_shader_performance_delta(base_dc, new_dc)
            
            # Secondary: Vertex count (descending)
            vertex_count = max(
                base_dc.vertex_count if base_dc else 0,
                new_dc.vertex_count if new_dc else 0
            )
            
            # Return tuple: (-perf_delta for descending, -vertex_count for descending)
            # Items with -inf perf_delta will be sorted by vertex count
            return (-perf_delta, -vertex_count)
        
        return sorted(matches, key=sort_key)
    
    def generate_drawcall_comparison(self):
        """Generate draw call comparison section"""
        self.add_html("<h2>🎯 Draw Call Comparison</h2>")
        
        matches = self.match_drawcalls()
        
        perfect = sum(1 for _, _, s in matches if s >= 0.95)
        good = sum(1 for _, _, s in matches if 0.8 <= s < 0.95)
        partial = sum(1 for _, _, s in matches if 0.6 <= s < 0.8)
        removed = sum(1 for b, n, _ in matches if b and not n)
        added = sum(1 for b, n, _ in matches if n and not b)
        
        self.add_html('<div class="stats-grid">')
        for label, count, badge_class in [
            ("✅ Perfect Matches", perfect, "similarity-perfect"),
            ("✔️ Good Matches", good, "similarity-good"),
            ("⚠️ Partial Matches", partial, "similarity-partial"),
            ("❌ Removed", removed, ""),
            ("🆕 Added", added, "")
        ]:
            self.add_html(f"""
<div class="stat-card">
    <div class="stat-value">{count}</div>
    <div class="stat-label">{label}</div>
</div>
""")
        self.add_html('</div>')
        
        # Add explanation of similarity scoring
        self.add_html("""
<div style="background:#e8f5e9;padding:15px;border-radius:5px;margin:20px 0;border-left:4px solid #4caf50;">
    <h4 style="margin:0 0 10px 0;color:#2e7d32;">📊 Similarity Scoring Explained (Geometry-First Matching)</h4>
    <p style="margin:5px 0;"><strong>✅ Perfect Match (≥95%):</strong> Same geometry (primitive count) + Same shaders + Same textures</p>
    <p style="margin:5px 0;"><strong>✔️ Good Match (80-95%):</strong> Geometry matches, minor shader or texture differences</p>
    <p style="margin:5px 0;"><strong>⚠️ Partial Match (60-80%):</strong> Some geometry/shader/texture differences</p>
    <p style="margin:5px 0;font-size:12px;color:#666;"><em>Scoring: <strong>Primitive Count (50%)</strong> + Shader MD5 (30%) + Texture MD5 (15%) + Draw Call Name (5%)</em></p>
    <p style="margin:5px 0;font-size:11px;color:#888;background:#fff;padding:8px;border-radius:3px;">
    <strong>💡 Why geometry first?</strong> If primitive counts differ significantly, they're likely rendering different objects entirely, 
    even if shaders/textures match. Matching geometry first ensures we compare apples-to-apples.
    </p>
</div>
<div style="background:#fff3cd;padding:15px;border-radius:5px;margin:20px 0;border-left:4px solid #ffc107;">
    <h4 style="margin:0 0 10px 0;color:#f57c00;">🔄 Report Sorting (Performance-First)</h4>
    <p style="margin:5px 0;"><strong>Primary Sort:</strong> Shader performance delta (slowdowns first) - draws with worse performance appear at the top</p>
    <p style="margin:5px 0;"><strong>Secondary Sort:</strong> Vertex count (highest first) - for draws without shader data or equal performance</p>
    <p style="margin:5px 0;font-size:11px;color:#888;background:#fff;padding:8px;border-radius:3px;">
    <strong>🎯 Why performance first?</strong> Performance regressions (slower shaders) have the highest visual impact and should be 
    addressed first. Draw calls with significant performance slowdowns appear at the top of each category.
    </p>
</div>
""")
        
        # Separate matches by type (in display order: Good → Partial → Added → Removed)
        perfect_matches = [(b, n, s) for b, n, s in matches if s >= 0.95]
        good_matches = [(b, n, s) for b, n, s in matches if 0.8 <= s < 0.95]
        partial_matches = [(b, n, s) for b, n, s in matches if 0.6 <= s < 0.8]
        removed_matches = [(b, n, s) for b, n, s in matches if b and not n]
        added_matches = [(b, n, s) for b, n, s in matches if n and not b]
        
        # 1. Good Matches (80-95%)
        if good_matches:
            good_matches = self.sort_by_performance_then_vertices(good_matches)
            self.add_html(f"<h3>✔️ Good Matches ({len(good_matches)} draw calls, 80-95% similarity)</h3>")
            self.add_html("""
<p style="background:#e8f5e9;padding:10px;border-radius:5px;border-left:4px solid #4caf50;">
<strong>Good matches</strong> have matching geometry with minor shader or texture differences.
<em>Sorted by shader performance impact (slowdowns first), then vertex count.</em>
</p>
""")
            # Show top 10 good matches
            for idx, (base_dc, new_dc, score) in enumerate(good_matches[:10], 1):
                self.add_html(f'<div class="drawcall-card">')
                self.add_html(f'<h4>#{idx} <span class="similarity-badge similarity-good">Good Match: {score*100:.1f}%</span></h4>')
                self.render_drawcall_details(base_dc, new_dc)
                self.render_shader_stats(base_dc, new_dc)
                self.add_html('</div>')
            if len(good_matches) > 10:
                self.add_html(f'<p style="color:#666;font-style:italic;">... and {len(good_matches)-10} more good matches</p>')
        
        # 2. Partial matches (60-80%)
        if partial_matches:
            # Sort by shader performance (slowdowns first), then vertex count
            partial_matches = self.sort_by_performance_then_vertices(partial_matches)
            
            self.add_html(f"<h3>⚠️ Partial Matches ({len(partial_matches)} draw calls)</h3>")
            self.add_html("""
<p style="background:#fff3cd;padding:10px;border-radius:5px;border-left:4px solid #ffc107;">
<strong>Note:</strong> Partial matches (60-95% similarity) indicate draw calls with some differences in shaders, textures, or geometry.
<em>Sorted by shader performance impact (slowdowns first), then vertex count to prioritize high-impact changes.</em>
</p>
""")
            
            for idx, (base_dc, new_dc, score) in enumerate(partial_matches, 1):
                self.add_html(f'<div class="drawcall-card">')
                self.add_html(f'<h4>#{idx} <span class="similarity-badge similarity-partial">Partial Match: {score*100:.1f}%</span></h4>')
                self.render_drawcall_details(base_dc, new_dc)
                self.render_shader_stats(base_dc, new_dc)
                self.add_html('</div>')
        
        # 3. Added Draw Calls  
        if added_matches:
            added_matches.sort(key=lambda x: x[1].vertex_count if x[1] else 0, reverse=True)
            self.add_html(f"<h3>🆕 Added Draw Calls ({len(added_matches)})</h3>")
            self.add_html("""
<p style="background:#e3f2fd;padding:10px;border-radius:5px;border-left:4px solid #2196f3;">
<strong>Added draw calls</strong> exist in the new capture but not in the base capture.
</p>
""")
            # Show top 15 added
            for idx, (base_dc, new_dc, score) in enumerate(added_matches[:15], 1):
                self.add_html(f'<div class="drawcall-card">')
                self.add_html(f'<h4>#{idx} <span class="similarity-badge" style="background:#2196f3;">Added</span></h4>')
                self.render_drawcall_details(base_dc, new_dc)
                self.render_shader_stats(base_dc, new_dc)
                self.add_html('</div>')
            if len(added_matches) > 15:
                self.add_html(f'<p style="color:#666;font-style:italic;">... and {len(added_matches)-15} more added draw calls</p>')
        
        # 4. Removed Draw Calls
        if removed_matches:
            removed_matches.sort(key=lambda x: x[0].vertex_count if x[0] else 0, reverse=True)
            self.add_html(f"<h3>❌ Removed Draw Calls ({len(removed_matches)})</h3>")
            self.add_html("""
<p style="background:#ffebee;padding:10px;border-radius:5px;border-left:4px solid #f44336;">
<strong>Removed draw calls</strong> exist in the base capture but not in the new capture.
</p>
""")
            # Show top 15 removed
            for idx, (base_dc, new_dc, score) in enumerate(removed_matches[:15], 1):
                self.add_html(f'<div class="drawcall-card">')
                self.add_html(f'<h4>#{idx} <span class="similarity-badge" style="background:#f44336;">Removed</span></h4>')
                self.render_drawcall_details(base_dc, new_dc)
                self.render_shader_stats(base_dc, new_dc)
                self.add_html('</div>')
            if len(removed_matches) > 15:
                self.add_html(f'<p style="color:#666;font-style:italic;">... and {len(removed_matches)-15} more removed draw calls</p>')
                
    def generate_shader_comparison(self):
        """Generate shader comparison section"""
        self.add_html("<h2>🔧 Shader Analysis & Complexity Comparison</h2>")
        
        base_sig = {(s.vertex_md5, s.fragment_md5): s for s in self.base.shaders.values()}
        new_sig = {(s.vertex_md5, s.fragment_md5): s for s in self.new.shaders.values()}
        
        same = len(set(base_sig.keys()) & set(new_sig.keys()))
        removed = len(set(base_sig.keys()) - set(new_sig.keys()))
        added = len(set(new_sig.keys()) - set(base_sig.keys()))
        
        self.add_html(f"""
<p>✅ <strong>Unchanged shaders:</strong> {same}</p>
<p>⚠️ <strong>Modified/Removed shaders:</strong> {removed}</p>
<p>🆕 <strong>New shaders:</strong> {added}</p>
""")
        
        # Unchanged shader complexity table
        matching_shaders = []
        for sig in sorted(set(base_sig.keys()) & set(new_sig.keys())):
            base_shader = base_sig[sig]
            new_shader = new_sig[sig]
            if base_shader.fragment_complexity and new_shader.fragment_complexity:
                matching_shaders.append((base_shader, new_shader))
        
        # Sort by resource_id for deterministic output across platforms
        matching_shaders.sort(key=lambda pair: int(pair[0].resource_id) if pair[0].resource_id.isdigit() else pair[0].resource_id)
                
        if matching_shaders:
            self.add_html("<h3>Unchanged Shader Complexity (Sample)</h3>")
            self.add_html("""
<table>
    <tr>
        <th>Shader Program</th>
        <th>ALU Cycles</th>
        <th>LS Cycles</th>
        <th>Texture Cycles</th>
        <th>Total Cycles</th>
        <th>Bound Unit</th>
        <th>Work Regs</th>
    </tr>
""")
            
            for base_shader, new_shader in matching_shaders:
                c = base_shader.fragment_complexity
                self.add_html(f"""
    <tr>
        <td>Program {base_shader.resource_id}</td>
        <td>{c.alu_cycles:.2f}</td>
        <td>{c.ls_cycles:.2f}</td>
        <td>{c.texture_cycles:.2f}</td>
        <td><strong>{c.total_cycles:.2f}</strong></td>
        <td><code>{c.bound_unit}</code></td>
        <td>{c.work_registers}</td>
    </tr>
""")
                
            self.add_html("</table>")
            
    def generate_footer(self):
        """Generate HTML footer"""
        self.add_html("""
<div style="margin-top: 50px; padding: 20px; background: #f8f9fa; border-radius: 5px; text-align: center;">
    <p>Report generated by RenderDoc Capture Comparison Tool</p>
    <p style="color: #666;">For questions or issues, check the documentation</p>
</div>
</body>
</html>
""")
        
    def generate(self) -> str:
        """Generate complete HTML report"""
        print("\nGenerating HTML report...")
        
        self.generate_header()
        self.generate_overall_stats()
        self.generate_drawcall_comparison()
        self.generate_shader_comparison()
        self.generate_footer()
        
        html = "\n".join(self.html_lines)
        
        html_path = self.output_dir / "comparison_report.html"
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html)
            
        print(f"[OK] HTML report saved: {html_path}")
        return str(html_path)


def main():
    """Main entry point"""
    if len(sys.argv) < 3:
        print("Usage: python rdc_compare_ultimate.py <base.rdc> <new.rdc> [--strict] [--renderdoc PATH] [--malioc PATH]")
        print("\nExamples:")
        print("  python rdc_compare_ultimate.py base.rdc new.rdc")
        print("  python rdc_compare_ultimate.py base.rdc new.rdc --strict")
        print("  python rdc_compare_ultimate.py base.rdc new.rdc --renderdoc G:\\Software\\RenderDoc_1.38_64")
        print("  python rdc_compare_ultimate.py base.rdc new.rdc --malioc /path/to/malioc")
        sys.exit(1)
        
    base_input = sys.argv[1]
    new_input = sys.argv[2]
    strict_mode = '--strict' in sys.argv
    verbose_mode = '--verbose' in sys.argv
    
    # Find renderdoc path
    renderdoc_path = None
    malioc_path = None
    for idx, arg in enumerate(sys.argv):
        if arg == '--renderdoc' and idx + 1 < len(sys.argv):
            renderdoc_path = sys.argv[idx + 1]
        if arg == '--malioc' and idx + 1 < len(sys.argv):
            malioc_path = sys.argv[idx + 1]
            
    output_dir = "output/rdc_comparison_output"
    Path(output_dir).mkdir(exist_ok=True, parents=True)
    
    print("=" * 70)
    print("RenderDoc Capture Comparison Tool - Ultimate Edition")
    print("=" * 70)
    
    # Convert if needed
    converter = RDCConverter(renderdoc_path)
    
    if base_input.endswith('.rdc'):
        print(f"\n=== Converting Base Capture ===")
        base_xml, base_zip = converter.convert(base_input, output_dir)
    else:
        base_xml = base_input
        base_zip = base_input.replace('.zip.xml', '.zip')
        
    if new_input.endswith('.rdc'):
        print(f"\n=== Converting New Capture ===")
        new_xml, new_zip = converter.convert(new_input, output_dir)
    else:
        new_xml = new_input
        new_zip = new_input.replace('.zip.xml', '.zip')
        
    # Analyze
    base_analyzer = RDCAnalyzer(base_xml, base_zip, output_dir, "base", verbose=verbose_mode, malioc_path=malioc_path)
    base_data = base_analyzer.analyze()
    
    new_analyzer = RDCAnalyzer(new_xml, new_zip, output_dir, "new", verbose=verbose_mode, malioc_path=malioc_path)
    new_data = new_analyzer.analyze()
    
    # Generate HTML report
    html_gen = HTMLReportGenerator(base_data, new_data, output_dir, strict_mode)
    html_path = html_gen.generate()
    
    print("\n" + "=" * 70)
    print("Analysis Complete!")
    print("=" * 70)
    print(f"Mode: {'Strict' if strict_mode else 'Loose'}")
    print(f"Output: {output_dir}/")
    print(f"  [OK] HTML Report: comparison_report.html")
    print(f"\nOpen report: {html_path}")
    

if __name__ == "__main__":
    main()
