# RenderDoc Comparison Tool - CodeBuddy Guide

## Project Overview

**RenderDoc Comparison Tool** (`renderdoccmp`) is a Python-based tool for comparing RenderDoc captures to detect rendering differences between game builds. It analyzes draw calls, shaders, textures, and performance metrics.

### Workflow Diagram

For a complete visual overview of how renderdoccmp works, see **[WORKFLOW.md](WORKFLOW.md)** which includes:
- Step-by-step process flow from .rdc input to HTML output
- Texture extraction and ASTC decoding pipeline
- Shader analysis with Mali compiler integration
- Draw call matching algorithm with similarity scoring
- Performance-first sorting logic

**Quick Overview:**
```
.rdc files → renderdoccmd → .zip.xml → RDCAnalyzer
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
              Textures (ASTC)         Shaders (Mali)        Draw Calls
                    │                      │                      │
                    └──────────────────────┼──────────────────────┘
                                           │
                                  Similarity Matching
                                           │
                                  Sort by Performance
                                           │
                                      HTML Report
```

## Quick Facts

- **Language**: Python 3.12+
- **Main Script**: `rdc_compare_ultimate.py`
- **Dependencies**: PIL (Pillow), RenderDoc, ARM Mali Offline Compiler (optional), ASTC Encoder
- **Output**: HTML report with embedded thumbnails and performance analysis

## Directory Structure

```
renderdoccmp/
├── rdc_compare_ultimate.py       # Main comparison tool (USE THIS)
├── rdc_compare_astc_fixed.py     # ASTC testing tool (development only)
├── sample/                        # Sample RDC files for testing
│   ├── base.rdc
│   └── 7475.rdc
├── tools/                         # External tools
│   ├── astcenc-sse4.1.exe        # ASTC texture decoder
│   └── mali_offline_compiler/     # ARM Mali shader analyzer
│       └── malioc.exe
├── output/                        # Generated reports (gitignored)
│   └── rdc_comparison_output/
└── docs/                          # Documentation
    ├── QUICK_START.md             # Quick start guide
    ├── ASTC_*.md                  # ASTC texture decoding docs
    ├── DRAWCALL_*.md              # Draw call matching docs
    ├── GEOMETRY_*.md              # Scoring algorithm docs
    └── HTML_*.md                  # HTML report docs
```

## Key Components

### Main Script: `rdc_compare_ultimate.py`

**Purpose**: Compare two RenderDoc captures and generate HTML report

**Key Classes**:
- `RDCConverter`: Converts .rdc files to .zip.xml format
- `RDCAnalyzer`: Parses XML and extracts draw calls, shaders, textures
- `ASTCDecoder`: Decodes ASTC compressed textures
- `HTMLReportGenerator`: Generates comparison HTML report

**Usage**:
```bash
python rdc_compare_ultimate.py base.rdc new.rdc [--strict] [--renderdoc PATH]
```

### Similarity Scoring (Geometry-First)

**Algorithm** (`compute_drawcall_similarity()` at line ~975):
- **Primitive Count (50%)**: Geometry must match for same object
- **Shader MD5 (30%)**: Vertex + fragment shader source code hash
- **Texture MD5 (15%)**: Texture data hash
- **Draw Call Name (5%)**: OpenGL function name

**Match Categories**:
- Perfect Match (≥95%): Geometry + shaders + textures match
- Good Match (80-95%): Geometry matches, minor differences
- Partial Match (60-80%): Some differences
- No Match (<60%): Different objects

### ASTC Texture Decoding

**Location**: `ASTCDecoder` class (line ~48-136)

**How it works**:
1. Detect ASTC format from `glCompressedTexImage2D` calls
2. Extract compressed data from ZIP
3. Create ASTC header (16 bytes: magic + block dims + image dims)
4. Decode with `astcenc-sse4.1.exe` (`-ds` for sRGB, `-dl` for linear)
5. Generate 128x128 thumbnail
6. Convert to base64 for HTML embedding

**Supported Formats**: All ASTC block sizes (4x4 to 12x12), sRGB and Linear

## Common Development Tasks

### Adding New Comparison Metrics

1. **Add to `DrawCall` dataclass** (line ~80-93)
2. **Extract in `extract_drawcalls()`** (line ~608-696)
3. **Compare in `compute_drawcall_similarity()`** (line ~975-1032)
4. **Display in `generate_drawcall_comparison()`** (line ~1042-1204)

### Modifying Scoring Weights

**Location**: `compute_drawcall_similarity()` function (line ~975)

```python
# Current weights (total = 1.0):
WEIGHT_GEOMETRY = 0.5   # Primitive count
WEIGHT_SHADER = 0.3     # Shader MD5
WEIGHT_TEXTURE = 0.15   # Texture MD5
WEIGHT_FUNCTION = 0.05  # Draw call name
```

**Why geometry first?**: Different primitive counts = different objects, even with same shaders.

### Adding New Texture Formats

1. **Add to `ASTCDecoder.ASTC_FORMATS`** (line ~51-80)
   ```python
   'GL_COMPRESSED_FORMAT_NAME': (block_x, block_y, is_srgb)
   ```

2. **Ensure decoder supports block size** (astcenc does by default)

3. **Test with sample texture**

### Debugging ASTC Decoding

**Enable debug output** in `create_texture_thumbnail()` (line ~311):
```python
print(f"Decoding ASTC: {tex_info.width}x{tex_info.height}, "
      f"block {tex_info.block_width}x{tex_info.block_height}, "
      f"sRGB={tex_info.is_srgb}, data={len(data)} bytes")
```

**Common issues**:
- Missing ZIP entry → Texture never uploaded
- 16-byte data → Header only, no texture data
- Decode fails → Invalid ASTC data or wrong block size

### Modifying HTML Report

**Structure**:
1. `generate_header()`: Title, metadata, CSS
2. `generate_overall_stats()`: Summary cards
3. `generate_drawcall_comparison()`: Partial matches with textures/shaders
4. `generate_shader_comparison()`: Shader complexity table
5. `generate_footer()`: Credits

**CSS**: Inline in `generate_header()` (line ~756-912)

**Add new section**:
```python
def generate_my_section(self):
    self.add_html("<h2>My New Section</h2>")
    self.add_html("<p>Content here</p>")

# Call in generate():
self.generate_my_section()
```

## Testing

### Run Full Comparison

```bash
cd renderdoccmp
python rdc_compare_ultimate.py sample/base.rdc sample/7475.rdc
```

**Expected output**:
- Converts RDC → XML
- Analyzes ~190 textures (160+ ASTC)
- Analyzes ~40 shader programs
- Finds ~230-236 draw calls
- Generates HTML report in `output/rdc_comparison_output/`

### Test ASTC Decoding

```bash
python rdc_compare_astc_fixed.py
```

**Expected**: Decodes first 5 ASTC textures, generates test HTML files

### Verify Output

```bash
start output/rdc_comparison_output/comparison_report.html
```

**Check for**:
- Scoring explanation visible
- Partial matches sorted by vertex count
- Shader performance comparisons shown
- Texture thumbnails embedded (10+)
- "No Preview" for textures without data

## File Paths

### Input Paths (Relative to `renderdoccmp/`)
- Sample RDCs: `sample/base.rdc`, `sample/7475.rdc`
- Tools: `tools/astcenc-sse4.1.exe`, `tools/mali_offline_compiler/malioc.exe`

### Output Paths (Relative to `renderdoccmp/`)
- Default output: `output/rdc_comparison_output/`
- Converted XML: `output/rdc_comparison_output/base.zip.xml`
- HTML report: `output/rdc_comparison_output/comparison_report.html`

### Path Updates in Code

**Important**: All paths in the code are relative to where you run the script.

**Example path changes needed** (already done):
```python
# Old (from repo root):
astcenc = Path("tools/astcenc-sse4.1.exe")

# New (from renderdoccmp/):
astcenc = Path("tools/astcenc-sse4.1.exe")  # Same! Relative to CWD
```

Run script from `renderdoccmp/` directory:
```bash
cd renderdoccmp
python rdc_compare_ultimate.py sample/base.rdc sample/7475.rdc
```

## Dependencies

### Required
- **Python 3.12+**
- **Pillow (PIL)**: `pip install Pillow`
- **RenderDoc**: For .rdc → .xml conversion
  - Must have `renderdoccmd.exe` in PATH or specify with `--renderdoc`

### Optional
- **ARM Mali Offline Compiler**: For shader complexity analysis
  - Location: `tools/mali_offline_compiler/malioc.exe`
  - Tool still works without it (skips shader analysis)
  
- **ASTC Encoder**: For ASTC texture decoding
  - Location: `tools/astcenc-sse4.1.exe`
  - Required for ASTC texture thumbnails
  - Download from: https://github.com/ARM-software/astc-encoder

## Code Style

### Python Conventions
- **Type hints**: Use for function signatures
- **Docstrings**: Required for classes and public methods
- **Line length**: 100 characters max
- **Naming**: snake_case for functions/variables, PascalCase for classes

### Example
```python
def extract_textures(self) -> Dict[str, TextureInfo]:
    """Extract all texture resources with ASTC support"""
    textures = {}
    # ... implementation
    return textures
```

## Important Notes

### "No Preview" Textures
- **Normal behavior**: 50-70% of textures show "No Preview"
- **Reasons**: Render targets, uninitialized textures, lazy loading
- **Not an error**: Only textures with data in ZIP can show previews
- **See**: `docs/ASTC_NO_PREVIEW_EXPLAINED.md`

### Missing chunkIndex in XML
- **Issue**: Some RenderDoc XML exports don't include `chunkIndex` attributes in `<chunk>` elements
- **Solution**: Tool automatically simulates chunk indices (0, 1, 2, ...) when missing
- **Detection**: Checks first chunk element for `chunkIndex` attribute presence
- **Impact**: Ensures correct EID (Event ID) calculation for draw call matching
- **Warning**: Tool prints "⚠️  XML missing chunkIndex attributes - simulating indices" when this occurs

### Geometry-First Scoring
- **Geometry (50%) > Shaders (30%) > Textures (15%)**
- **Rationale**: Different geometry = different objects
- **Example**: 100-tri cube vs 10,000-tri character won't match even with same shader
- **See**: `docs/GEOMETRY_FIRST_SCORING.md`

### Partial Match Sorting
- **Primary sort**: Shader performance delta (slowdowns first)
- **Secondary sort**: Vertex count (largest first) 
- **Rationale**: Performance regressions have highest visual impact and should be addressed first
- **Shows ALL matches** (not limited to top 20)
- **Draw calls with shader slowdowns appear at the top of each category**

## Troubleshooting

### "renderdoccmd not found"
```bash
python rdc_compare_ultimate.py base.rdc new.rdc --renderdoc "C:/Path/To/RenderDoc"
```

### "astcenc not found"
- Check `tools/astcenc-sse4.1.exe` exists
- ASTC thumbnails will be skipped if missing (shows "No Preview")

### Incorrect EID or draw call matching
- **Symptom**: Draw calls not matching correctly between captures
- **Cause**: XML may be missing `chunkIndex` attributes
- **Check**: Look for warning "⚠️  XML missing chunkIndex attributes - simulating indices" in output
- **Fix**: Tool automatically handles this - no action needed
- **Why it matters**: EID (Event ID) is calculated from chunkIndex for accurate draw call matching

### "PIL not available"
```bash
pip install Pillow
```

### Paths don't work
- Run from `renderdoccmp/` directory:
  ```bash
  cd renderdoccmp
  python rdc_compare_ultimate.py sample/base.rdc sample/7475.rdc
  ```

## Performance

### Typical Runtime
- **Small capture** (100 draw calls): ~10-20 seconds
- **Medium capture** (236 draw calls, 191 textures): ~30-60 seconds
- **Large capture** (500+ draw calls): ~2-5 minutes

### Bottlenecks
1. **ASTC decoding**: ~50-200ms per texture
2. **Mali shader analysis**: ~100ms per shader
3. **XML parsing**: ~1-2 seconds

### Optimization Tips
- Skip shader analysis: Comment out Mali calls (line ~714-730)
- Reduce thumbnail count: Limit in `generate_drawcall_comparison()`
- Use converted XML: Keep .zip.xml files to skip reconversion

## Documentation

### Quick Reference
- **Quick Start**: `docs/QUICK_START.md` - Basic usage
- **ASTC Decoding**: `docs/ASTC_INTEGRATION_COMPLETE.md` - How ASTC works
- **Draw Call Matching**: `docs/DRAWCALL_MATCHING_EXPLAINED.md` - Scoring details
- **Geometry Scoring**: `docs/GEOMETRY_FIRST_SCORING.md` - Why geometry first
- **HTML Report**: `docs/HTML_REPORT_IMPROVEMENTS.md` - Report structure

### For Users
Start with `docs/QUICK_START.md` for basic usage and examples.

### For Developers
Read `docs/GEOMETRY_FIRST_SCORING.md` to understand the scoring algorithm, then review the main script (`rdc_compare_ultimate.py`).

## Common Modifications

### Change Output Directory
```python
# Line ~1327 in main():
output_dir = "my_output_dir"
```

### Change Match Threshold
```python
# Line ~1043 in match_drawcalls():
threshold = 0.8 if self.strict_mode else 0.6  # Change these values
```

### Add New Shader Metric
```python
# 1. Add to ShaderComplexity dataclass (line ~49)
new_metric: float = 0.0

# 2. Parse in analyze_shader_with_mali() (line ~541)
new_match = re.search(r'New metric:\s+([\d.]+)', output)
if new_match:
    complexity.new_metric = float(new_match.group(1))

# 3. Display in generate_drawcall_comparison() (line ~1154)
<p><strong>New Metric:</strong> {c.new_metric:.2f}</p>
```

## License

MIT License - See root `LICENSE.md`

## Contact

- **Issues**: Report in root repo issue tracker
- **Questions**: Check `docs/` first, then ask in issues

---

*This is a focused guide for the RenderDoc Comparison Tool. For RenderDoc itself, see root `CODEBUDDY.md`.*
