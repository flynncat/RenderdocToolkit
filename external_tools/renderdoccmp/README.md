# RenderDoc Comparison Tool

Compare two RenderDoc captures to detect rendering differences between game builds.

## Quick Start

```bash
cd renderdoccmp
python rdc_compare_ultimate.py sample/base.rdc sample/7475.rdc
```

**Output**: `output/rdc_comparison_output/comparison_report.html`

## How It Works

```
┌─────────────┐      ┌──────────────┐      ┌──────────────┐
│  base.rdc   │      │   new.rdc    │      │              │
│  new.rdc    │─────▶│ renderdoccmd │─────▶│  .zip.xml    │
│             │      │  --convert   │      │  .zip        │
└─────────────┘      └──────────────┘      └──────┬───────┘
                                                   │
                     ┌─────────────────────────────┘
                     │
         ┌───────────▼────────────┐
         │   RDCAnalyzer          │
         │   Parse & Extract:     │
         └───────────┬────────────┘
                     │
      ┌──────────────┼──────────────┐
      │              │              │
┌─────▼─────┐  ┌─────▼─────┐  ┌────▼─────┐
│ TEXTURES  │  │  SHADERS  │  │  DRAW    │
│           │  │           │  │  CALLS   │
│ • Format  │  │ • Source  │  │ • Geom   │
│ • ASTC    │  │ • Mali    │  │ • Bound  │
│   Decode  │  │   Metrics │  │   Res    │
│ • MD5     │  │ • MD5     │  │ • State  │
└─────┬─────┘  └─────┬─────┘  └────┬─────┘
      │              │              │
      └──────────────┼──────────────┘
                     │
         ┌───────────▼────────────┐
         │  Draw Call Matching    │
         │  Similarity Score:     │
         │  • Geometry (50%)      │
         │  • Shaders (30%)       │
         │  • Textures (15%)      │
         │  • Function (5%)       │
         └───────────┬────────────┘
                     │
         ┌───────────▼────────────┐
         │  Sort by Performance   │
         │  1. Shader cycles Δ    │
         │  2. Vertex count       │
         │  (Slowdowns first!)    │
         └───────────┬────────────┘
                     │
         ┌───────────▼────────────┐
         │   HTML Report with:    │
         │   • Statistics         │
         │   • Draw comparisons   │
         │   • Texture previews   │
         │   • Shader performance │
         └────────────────────────┘
```

**See [WORKFLOW.md](WORKFLOW.md) for detailed diagram.**

## Features

✅ **Cross-Platform** - Runs on both Windows and Linux with bundled binaries  
✅ **Draw Call Comparison** - Geometry-first matching algorithm  
✅ **Shader Analysis** - ARM Mali complexity metrics  
✅ **ASTC Texture Decoding** - Full support for all ASTC formats  
✅ **HTML Reports** - Interactive reports with embedded thumbnails  
✅ **Performance Tracking** - Shader cycle counts and bottleneck detection  

## Requirements

- Python 3.12+
- Pillow: `pip install Pillow`

**Bundled Tools (no install needed):**

All required tool binaries are bundled in `tools/`. The script auto-detects the current OS and uses the matching platform binary. On Linux, executable permissions (`chmod +x`) are set automatically.

| Tool | Windows | Linux | Purpose |
|------|---------|-------|---------|
| **RenderDoc CLI** | `tools/renderdoc/windows/renderdoccmd.exe` | `tools/renderdoc/linux/renderdoccmd` | .rdc → .zip.xml conversion |
| **ASTC Encoder** | `tools/astcenc/windows/astcenc-sse4.1.exe` | `tools/astcenc/linux/astcenc-sse4.1` | ASTC texture decoding |
| **Mali Offline Compiler** | `tools/mali_offline_compiler/windows/malioc.exe` | `tools/mali_offline_compiler/linux/malioc` | Shader performance analysis |

## Usage

### Basic Comparison

```bash
# Windows
python rdc_compare_ultimate.py base.rdc new.rdc

# Linux
python3 rdc_compare_ultimate.py base.rdc new.rdc
```

### Strict Mode (Tighter Matching)

```bash
python rdc_compare_ultimate.py base.rdc new.rdc --strict
```

### Specify RenderDoc Directory

Override the bundled renderdoccmd with a custom installation. **Note:** pass the **directory** path, not the exe file path.

```bash
# Windows
python rdc_compare_ultimate.py base.rdc new.rdc --renderdoc "C:/Program Files/RenderDoc"

# Linux
python3 rdc_compare_ultimate.py base.rdc new.rdc --renderdoc "/opt/renderdoc"
```

### Specify Mali Offline Compiler

Use a specific version of malioc for shader analysis:

```bash
# Windows
python rdc_compare_ultimate.py base.rdc new.rdc --malioc "C:/Program Files/Arm/mali_offline_compiler/malioc.exe"

# Linux
python3 rdc_compare_ultimate.py base.rdc new.rdc --malioc "/opt/arm/mali_offline_compiler/malioc"
```

### Verbose Mode (Debug)

Show tool paths, shader compilation errors, and other diagnostic info:

```bash
python rdc_compare_ultimate.py base.rdc new.rdc --verbose
```

### Use Pre-Converted XML

If you already have `.zip.xml` files from a previous conversion, pass them directly (no renderdoccmd needed):

```bash
python rdc_compare_ultimate.py base.zip.xml new.zip.xml
```

### Combine Options

```bash
python rdc_compare_ultimate.py base.rdc new.rdc --strict --verbose --renderdoc "/path/to/renderdoc" --malioc "/path/to/malioc"
```

## Output

The tool generates an HTML report with:

- **Overall Statistics**: Draw call, primitive, texture counts
- **Partial Matches**: All draw calls with differences (sorted by shader performance impact, then vertex count)
- **Shader Performance**: Mali cycle counts for each draw call
- **Texture Previews**: ASTC-decoded thumbnails
- **Match Categories**: Perfect (≥95%), Good (80-95%), Partial (60-80%)

### Report Sorting

Draw calls are sorted by **shader performance impact first** (slowdowns appear at the top), then by **vertex count**:
- **Primary**: Performance regressions (increased shader cycles) appear first
- **Secondary**: High vertex count draw calls (more visual impact)
- **Result**: Most critical performance issues are immediately visible at the top

## Similarity Scoring

**Geometry-First Algorithm**:
- **Primitive Count (50%)** - Must match geometry to be same object
- **Shader MD5 (30%)** - Vertex + fragment shader source code
- **Texture MD5 (15%)** - Texture data
- **Draw Call Name (5%)** - OpenGL function

**Why geometry first?** Different primitive counts indicate different objects, even with identical shaders.

## Directory Structure

```
renderdoccmp/
├── rdc_compare_ultimate.py       # Main tool (use this!)
├── sample/                        # Sample RDC files
├── tools/                         # Bundled tools (cross-platform)
│   ├── renderdoc/                 # RenderDoc CLI
│   │   ├── windows/
│   │   │   ├── renderdoccmd.exe
│   │   │   └── renderdoc.dll
│   │   └── linux/
│   │       ├── renderdoccmd
│   │       └── librenderdoc.so
│   ├── astcenc/                   # ASTC texture decoder
│   │   ├── windows/
│   │   │   └── astcenc-sse4.1.exe
│   │   └── linux/
│   │       └── astcenc-sse4.1
│   └── mali_offline_compiler/     # ARM Mali shader analyzer
│       ├── windows/
│       │   ├── malioc.exe
│       │   ├── external/glslang.exe
│       │   └── graphics/*.dll
│       └── linux/
│           ├── malioc
│           ├── external/glslang
│           └── graphics/*.so
├── output/                        # Generated reports
└── docs/                          # Documentation
```

## Documentation

- **[Quick Start Guide](docs/QUICK_START.md)** - Basic usage and examples
- **[Scoring Algorithm](docs/GEOMETRY_FIRST_SCORING.md)** - How draw calls are matched
- **[ASTC Decoding](docs/ASTC_INTEGRATION_COMPLETE.md)** - Texture compression details
- **[Draw Call Matching](docs/DRAWCALL_MATCHING_EXPLAINED.md)** - Match categories explained
- **[CODEBUDDY.md](CODEBUDDY.md)** - Developer guide for CodeBuddy Code

## Examples

### Example 1: Detect Shader Changes

```bash
python rdc_compare_ultimate.py old_version.rdc new_version.rdc
```

**Result**: Report shows which draw calls have different shaders, with performance impact.

### Example 2: Track Texture Changes

The HTML report shows texture thumbnails side-by-side for visual comparison of texture changes (resolution, compression, content).

### Example 3: Find Performance Regressions

Shader performance section shows cycle count changes:
- 🟢 Green = Faster (fewer cycles)
- 🔴 Red = Slower (more cycles)

## Common Issues

### "No Preview" Textures

**Normal!** 50-70% of textures show "No Preview" because:
- Render targets (depth buffers, FBOs)
- Lazily-loaded textures
- Textures created but never uploaded

### ASTC Decoding Fails

The ASTC encoder is bundled in `tools/astcenc/` for both platforms. To update, download from:
https://github.com/ARM-software/astc-encoder/releases

### renderdoccmd Not Found

The bundled renderdoccmd in `tools/renderdoc/` is used by default. To use a custom version, specify its **directory** (not the exe path):
```bash
# Correct - pass directory path
python rdc_compare_ultimate.py base.rdc new.rdc --renderdoc "C:/Program Files/RenderDoc"

# Wrong - do not pass the exe file path directly
python rdc_compare_ultimate.py base.rdc new.rdc --renderdoc "C:/Program Files/RenderDoc/renderdoccmd.exe"
```

### renderdoccmd DLL Missing (0xC0000135)

`renderdoccmd.exe` requires `renderdoc.dll` in the same directory. The bundled `tools/renderdoc/windows/` includes both files. If using a custom path, ensure both files are present.

### Mali Shader Cycle Counts Differ Between Platforms

Windows and Linux may use different Mali GPU model library versions (e.g. `r54p0` vs `r55p0`), causing different cycle counts. Use `--malioc` to point both platforms to the same version.

### Mali Shader Analysis Returns No Data on Linux

Usually caused by missing executable permissions on bundled binaries. The script auto-fixes this with `chmod +x`. If it still fails, run with `--verbose` to see the exact error.

### Missing chunkIndex Warning

If you see: `⚠️  XML missing chunkIndex attributes - simulating indices`

**This is normal!** Some RenderDoc exports don't include `chunkIndex` attributes. The tool automatically simulates them (0, 1, 2, ...) to ensure correct EID calculation and draw call matching. No action needed.

## Performance

- **Small capture** (100 draw calls): ~10-20 seconds
- **Medium capture** (236 draw calls): ~30-60 seconds
- **Large capture** (500+ draw calls): ~2-5 minutes

## Contributing

See [CODEBUDDY.md](CODEBUDDY.md) for development guide.

## License

MIT License - See root `LICENSE.md`

## Sample Output

Match distribution from sample captures:
```
✅ Perfect Matches: 115
⚠️ Partial Matches: 115
❌ Removed: 6
🆕 Added: 0
```

Shader performance example:
```
Base Shader: 5.75 cycles (Bound: LS)
New Shader: 5.37 cycles (6.6% faster) ✅
```

---

**Ready to compare captures!** Start with `python rdc_compare_ultimate.py sample/base.rdc sample/7475.rdc`
