# RenderDoc Comparison Tool - Workflow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                   RENDERDOCCMP WORKFLOW - ULTIMATE EDITION                  │
└─────────────────────────────────────────────────────────────────────────────┘

                            USER INPUT
                                │
                    ┌───────────┴───────────┐
                    │                       │
              ┌─────▼─────┐           ┌────▼─────┐
              │ base.rdc  │           │ new.rdc  │
              └─────┬─────┘           └────┬─────┘
                    │                      │
                    │  [RDCConverter]      │
                    │  renderdoccmd        │
                    │  --convert           │
                    │                      │
              ┌─────▼─────┐           ┌────▼─────┐
              │base.zip.xml│          │new.zip.xml│
              │base.zip    │          │new.zip    │
              └─────┬─────┘           └────┬─────┘
                    │                      │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼──────────┐
                    │   RDCAnalyzer       │
                    │   (Parse XML/ZIP)   │
                    └──────────┬──────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
┌───────▼───────┐     ┌────────▼────────┐    ┌───────▼───────┐
│   TEXTURES    │     │    SHADERS      │    │  DRAW CALLS   │
└───────┬───────┘     └────────┬────────┘    └───────┬───────┘
        │                      │                      │
        │                      │                      │
┌───────▼────────────────────────────────────────────▼───────┐
│                   TEXTURE EXTRACTION                        │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  • Parse XML for glCompressedTexImage2D calls        │  │
│  │  • Extract format (ASTC 4x4, 5x5, etc.)             │  │
│  │  • Read texture data from ZIP (*.bin files)          │  │
│  │  • Compute MD5 hash for comparison                   │  │
│  │  • Store: width, height, format, block size          │  │
│  └──────────────────────────────────────────────────────┘  │
│                           │                                 │
│                  ┌────────▼─────────┐                       │
│                  │  ASTC DECODING   │                       │
│                  │  (ASTCDecoder)   │                       │
│                  └────────┬─────────┘                       │
│                           │                                 │
│                  ┌────────▼─────────────────────────┐       │
│                  │  1. Create ASTC header (16 bytes)│       │
│                  │     - Magic: 0x13AB A15C         │       │
│                  │     - Block dims (e.g., 4x4)     │       │
│                  │     - Image dims (width, height) │       │
│                  │  2. Append compressed data        │       │
│                  │  3. Call astcenc-sse4.1.exe      │       │
│                  │     -ds (sRGB) or -dl (linear)   │       │
│                  │  4. Generate 128x128 thumbnail   │       │
│                  │  5. Convert to base64 for HTML   │       │
│                  └──────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                   SHADER EXTRACTION                         │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  • Parse XML for shader programs                     │  │
│  │  • Extract vertex & fragment shader source (GLSL)   │  │
│  │  • Compute MD5 hash for each shader                  │  │
│  │  • Link shader IDs to programs                       │  │
│  └──────────────────────────────────────────────────────┘  │
│                           │                                 │
│                  ┌────────▼──────────┐                      │
│                  │  MALI COMPILER    │                      │
│                  │  (malioc.exe)     │                      │
│                  └────────┬──────────┘                      │
│                           │                                 │
│                  ┌────────▼──────────────────────────┐      │
│                  │  Analyze shader complexity:       │      │
│                  │  • Work registers                 │      │
│                  │  • Uniform registers              │      │
│                  │  • ALU cycles                     │      │
│                  │  • Load/Store cycles              │      │
│                  │  • Varying cycles                 │      │
│                  │  • Texture cycles                 │      │
│                  │  • Total cycles (performance!)    │      │
│                  │  • Bound unit (bottleneck)        │      │
│                  └───────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                   DRAW CALL EXTRACTION                      │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  • Parse XML for draw chunks (glDrawElements, etc.) │  │
│  │  • Extract geometry:                                 │  │
│  │    - Primitive count (triangles)                     │  │
│  │    - Vertex count                                    │  │
│  │    - Instance count                                  │  │
│  │  • Link bound textures (texture unit → resource ID) │  │
│  │  • Link shader program (current program)            │  │
│  │  • Extract state: FBO, timestamp, name              │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘

                    ┌──────────────────┐
                    │  CAPTURE DATA    │
                    │  (Complete Info) │
                    └────────┬─────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
        ┌─────▼─────┐                 ┌─────▼─────┐
        │ BASE DATA │                 │  NEW DATA │
        └─────┬─────┘                 └─────┬─────┘
              │                             │
              └──────────┬──────────────────┘
                         │
              ┌──────────▼──────────┐
              │  DRAW CALL MATCHING │
              │  (Similarity Score) │
              └──────────┬──────────┘
                         │
        ┌────────────────┼────────────────┐
        │                │                │
┌───────▼───────┐ ┌──────▼──────┐ ┌──────▼──────┐
│  GEOMETRY     │ │   SHADERS   │ │  TEXTURES   │
│  (50% weight) │ │ (30% weight)│ │ (15% weight)│
└───────┬───────┘ └──────┬──────┘ └──────┬──────┘
        │                │                │
        │   Primitive    │   Vertex MD5   │  Texture MD5
        │   count match  │   Fragment MD5 │  hash match
        │                │                │
        └────────────────┴────────┬───────┘
                                  │
                         ┌────────▼────────┐
                         │ + Function Name │
                         │   (5% weight)   │
                         └────────┬────────┘
                                  │
                         ┌────────▼────────────────┐
                         │  SIMILARITY SCORE       │
                         │  ≥95% = Perfect Match   │
                         │  80-95% = Good Match    │
                         │  60-80% = Partial Match │
                         │  <60% = No Match        │
                         └────────┬────────────────┘
                                  │
                         ┌────────▼──────────┐
                         │   SORT RESULTS    │
                         │   (Performance-   │
                         │    First!)        │
                         └────────┬──────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
          ┌─────────▼─────────┐    ┌───────────▼──────────┐
          │ Primary Sort:     │    │ Secondary Sort:      │
          │ Shader Perf Delta │    │ Vertex Count         │
          │ (Slowdowns First!)│    │ (Largest First)      │
          └─────────┬─────────┘    └───────────┬──────────┘
                    │                           │
                    └───────────┬───────────────┘
                                │
                    ┌───────────▼───────────┐
                    │  HTML REPORT          │
                    │  GENERATOR            │
                    └───────────┬───────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
┌───────▼───────┐      ┌────────▼────────┐     ┌───────▼──────┐
│  STATISTICS   │      │  DRAW CALL      │     │   SHADER     │
│               │      │  COMPARISON     │     │  COMPARISON  │
│ • Perfect:115 │      │                 │     │              │
│ • Good: 10    │      │ ✔️ Good Matches │     │ Performance: │
│ • Partial:115 │      │ ⚠️  Partial     │     │ • Unchanged  │
│ • Removed: 6  │      │ 🆕 Added        │     │ • Modified   │
│ • Added: 0    │      │ ❌ Removed      │     │ • Cycle comp │
└───────────────┘      └─────────────────┘     └──────────────┘
        │                       │                       │
        └───────────────────────┼───────────────────────┘
                                │
                    ┌───────────▼────────────┐
                    │  FOR EACH DRAW CALL:   │
                    │  ┌─────────────────┐   │
                    │  │ Draw Call Info  │   │
                    │  │ • Name, EID     │   │
                    │  │ • Geometry      │   │
                    │  │ • Similarity %  │   │
                    │  └─────────────────┘   │
                    │  ┌─────────────────┐   │
                    │  │ Texture Preview │   │
                    │  │ [Base] [New]    │   │
                    │  │ 128x128 thumb   │   │
                    │  └─────────────────┘   │
                    │  ┌─────────────────┐   │
                    │  │ Shader Stats    │   │
                    │  │ Base: 5.75c     │   │
                    │  │ New:  5.37c ✅  │   │
                    │  │ (6.6% faster)   │   │
                    │  └─────────────────┘   │
                    └────────────────────────┘
                                │
                    ┌───────────▼────────────┐
                    │ comparison_report.html │
                    │ (Open in browser!)     │
                    └────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                        KEY FEATURES                         │
├─────────────────────────────────────────────────────────────┤
│ ✅ Direct .rdc input (automatic conversion)                 │
│ ✅ ASTC texture decoding (all formats)                      │
│ ✅ Mali shader complexity analysis                          │
│ ✅ Geometry-first similarity matching                       │
│ ✅ Performance-first report sorting                         │
│ ✅ Interactive HTML with embedded images                    │
│ ✅ Performance regression detection                         │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                      PERFORMANCE TIPS                       │
├─────────────────────────────────────────────────────────────┤
│ 🔥 Slowdowns appear at the top of each category             │
│ 📊 Fragment shader cycles are primary performance metric    │
│ 🎯 Draws without shader data sorted by vertex count         │
│ ⚡ High vertex count = high visual impact                   │
└─────────────────────────────────────────────────────────────┘
```

## Workflow Summary

### Stage 1: Conversion (RDCConverter)
- Input: `base.rdc` and `new.rdc` files
- Tool: `renderdoccmd --convert`
- Output: `.zip.xml` (structured XML) + `.zip` (resource data)

### Stage 2: Analysis (RDCAnalyzer)
- **Texture Extraction**: Parse XML for texture calls, extract ASTC data
- **Shader Extraction**: Extract GLSL source, compute MD5 hashes
- **Draw Call Extraction**: Link geometry, textures, and shaders
- **Mali Analysis**: Compute shader complexity metrics (cycles, registers)
- **ASTC Decoding**: Convert compressed textures to PNG thumbnails

### Stage 3: Comparison (HTMLReportGenerator)
- **Draw Call Matching**: Compute similarity scores (geometry-first)
- **Categorization**: Perfect (≥95%), Good (80-95%), Partial (60-80%)
- **Sorting**: Performance-first (slowdowns at top), then vertex count
- **Report Generation**: HTML with embedded images and performance data

### Stage 4: Output
- Interactive HTML report with:
  - Overall statistics
  - Draw call comparisons (sorted by performance impact)
  - Texture previews (side-by-side)
  - Shader performance analysis
  - Performance regression highlights
