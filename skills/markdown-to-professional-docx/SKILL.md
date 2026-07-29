---
name: markdown-to-professional-docx
description: Converts Markdown requirement analysis and technical design documents into executive-grade professional Word (.docx) documents matching the design standards, typography, corporate navy color palette, section breaks, cover page layout, zebra-striped tables, left-bordered callouts, code blocks, and running headers/footers of '统一异构KVCache存储池总体架构与SRS评审导读_正式版_专业排版.docx'.
---

# Technical & Requirement Analysis MD to Professional DOCX Conversion Skill

This skill documents the exact design system, visual styling rules, structural transformations, and technical implementation required to convert Markdown (`.md`) requirement specification and technical architecture documents into publication-grade executive Word (`.docx`) reports.

It is derived directly from the comparative analysis of `统一异构KVCache存储池总体架构与SRS评审导读_定版.md` and `统一异构KVCache存储池总体架构与SRS评审导读_正式版_专业排版.docx`.

---

## 1. Core Visual Architecture & Design System

### 1.1 Document Geometry & Page Setup
* **Paper Size**: Standard A4 (210mm × 297mm / 8.27" × 11.69").
* **Page Margins**:
  * Top Margin: `2.50 cm`
  * Bottom Margin: `2.30 cm`
  * Left Margin: `2.54 cm`
  * Right Margin: `2.54 cm`
* **Header / Footer Distances**: Top `1.50 cm`, Bottom `1.50 cm`.
* **Section Separation**: 3 Distinct Sections:
  1. **Section 0 (Cover Page)**: Unnumbered, suppressed header & footer (`different_first_page_header_footer = True`).
  2. **Section 1 (Table of Contents)**: Roman page numbering (`— i —`, `— ii —`).
  3. **Section 2 (Main Document Body)**: Arabic page numbering (`— 1 —`, `— 2 —`), with right-aligned running document title in top header.

---

### 1.2 Color Palette Tokens (Corporate Navy Theme)
| Design Token | Role / Applied Context | Hex Code | RGB Color |
|---|---|---|---|
| **NAVY_PRIMARY** | Main Accent: Document Title, Heading 1, Heading 2, Table Header Background, Callout Left Bar | `#17365D` | `RGBColor(23, 54, 93)` |
| **STEEL_SECONDARY** | Sub Accent: Subtitle, Category Badge, Heading 3, Heading 4, Bullet Highlights | `#2F5597` | `RGBColor(47, 85, 151)` |
| **TEXT_DARK** | Body Text, Table Cell Text, Default Run Text (Replaces harsh pure black) | `#1F1F1F` | `RGBColor(31, 31, 31)` |
| **TEXT_MUTED** | Subtitles, Captions, Header & Footer Text | `#666666` | `RGBColor(102, 102, 102)` |
| **BG_ICE_BLUE** | Metadata Box Left Column, Callout Container Fill | `#EEF4F9` | Hex XML Fill |
| **BG_ALT_ROW** | Alternating Zebra Shading for Table Data Rows (Even Rows) | `#F7F9FB` | Hex XML Fill |
| **BG_CODE** | Technical Code Block Background Shading | `#F2F4F7` | Hex XML Fill |
| **BORDER_ACCENT** | Solid Bottom Rule Divider below Heading 1 | `#D9E2F3` | Hex XML Border |
| **BORDER_GRID** | Subtle Grid Borders for Tables | `#D9D9D9` | Hex XML Border |

---

### 1.3 Typography & Font Hierarchy
* **Primary Chinese Font**: `SimSun` (宋体) for Body Text & Tables; `Microsoft YaHei` (微软雅黑) for Headings, Badges, Callouts & Code.
* **Primary Western Font**: `Arial` for Headings & Body Text; `Consolas` for Code Blocks.

| Hierarchy Element | Font Family (ASCII / EastAsia) | Font Size | Color | Style | Spacing Before / After | Line Spacing | Special Features |
|---|---|---|---|---|---|---|---|
| **Category Badge** | Arial / Microsoft YaHei | 11.0 pt | `#2F5597` | Bold | 0 pt / 20 pt | 1.0x | Upper cover label |
| **Cover Title** | Arial / Microsoft YaHei | 26.0 pt | `#17365D` | Bold | 60 pt / 12 pt | 1.0x | Primary title |
| **Metadata Label** | Arial / Microsoft YaHei | 10.0 pt | `#17365D` | Bold | 0 pt / 0 pt | 1.0x | Ice Blue Cell BG (`#EEF4F9`) |
| **Heading 1** | Arial / Microsoft YaHei | 16.0 pt | `#17365D` | Bold | 18 pt / 8 pt | 1.15x | Solid bottom border (`#D9E2F3`, 0.75pt) |
| **Heading 2** | Arial / Microsoft YaHei | 14.0 pt | `#17365D` | Bold | 14 pt / 6 pt | 1.15x | Standard sub-section title |
| **Heading 3** | Arial / Microsoft YaHei | 12.0 pt | `#2F5597` | Bold | 11 pt / 4 pt | 1.15x | Minor section title |
| **Heading 4** | Arial / Microsoft YaHei | 11.0 pt | `#2F5597` | Bold | 8 pt / 4 pt | 1.15x | Detailed sub-item heading |
| **Body Text** | Arial / SimSun | 10.5 pt | `#1F1F1F` | Regular | 0 pt / 6 pt | 1.45x | Relaxed executive readability |
| **Callout Box** | Arial / Microsoft YaHei | 10.0 pt | `#17365D` | Regular/Bold | 4 pt / 6 pt | 1.30x | Left border 3.0pt (`#17365D`), Fill `#EEF4F9` |
| **Code Block** | Consolas / Microsoft YaHei | 8.5 pt | `#1F1F1F` | Regular | 4 pt / 8 pt | 1.15x | Fill `#F2F4F7`, Left border light gray |
| **Table Header** | Arial / Microsoft YaHei | 10.0 pt | `#FFFFFF` | Bold | 2 pt / 2 pt | 1.0x | Navy Fill (`#17365D`), Center aligned |
| **Table Data** | Arial / SimSun | 9.5 pt | `#1F1F1F` | Regular | 2 pt / 2 pt | 1.0x | Alternating row tint (`#F7F9FB`) |
| **Table/Figure Caption**| Arial / SimSun | 9.0 pt | `#666666` | Regular | 5 pt / 4 pt | 1.0x | Center aligned, Gray text |

---

## 2. Element Structural Transformation Rules

### 2.1 Cover Page & Top Metadata Transformation
Markdown documents typically begin with a top header and blockquote metadata:
```markdown
# 统一异构 KVCache 存储池总体架构与 SRS 评审导读

> 文档版本：定版  
> 评审基线：《KVCache SRS需求列表 V2.1.xlsx》  
> 文档用途：帮助第三方评审专家理解项目愿景、需求设置初衷、总体架构、阶段路径与评审重点。
```
**Transformation Standard**:
1. **Title Block**: Extract `# Title` to create an executive cover title formatted with 26pt bold `#17365D`. Prepend a category badge (`技术架构与需求评审文件`) in 11pt bold `#2F5597`.
2. **Metadata Summary Box (Table 0)**: Convert the metadata blockquote lines into a structured 2-column table:
   * Left Column (Key): Width `4.0 cm`, Background `#EEF4F9`, Text bold `#17365D`.
   * Right Column (Value): Width `12.0 cm`, Background `#FFFFFF`, Text regular `#1F1F1F`.
   * Cell Paddings: Top/Bottom `5 pt`, Left/Right `7 pt`.
3. **Section Break**: Append a `NEW_PAGE` section break after the cover block to separate cover from content.

---

### 2.2 Table Transformation Standard
Markdown pipe tables (`| Col1 | Col2 |`) are transformed into executive report tables:
1. **Header Row Styling**:
   * Shading Fill: `#17365D` (Dark Executive Navy).
   * Text Color: Pure White (`#FFFFFF`), Bold, `Microsoft YaHei` 10.0pt, Center-aligned.
   * Properties: Append `<w:tblHeader/>` to force header row repetition across multi-page breaks.
2. **Data Row Styling**:
   * Alternating Row Shading: Even rows background `#F7F9FB`; Odd rows background `#FFFFFF`.
   * Text Alignment: Left aligned (or Center for short IDs/states), `SimSun` 9.5pt `#1F1F1F`.
   * Row Break Prevention: Append `<w:cantSplit/>` to each row element to prevent individual rows from splitting awkwardly across page boundaries.
3. **Table Borders & Cell Margins**:
   * Borders: Subtle horizontal and vertical gridlines in light gray `#D9D9D9` (0.5 pt).
   * Cell Padding: Top `6 pt`, Bottom `6 pt`, Left `8 pt`, Right `8 pt` (DXA values: `120`, `120`, `160`, `160`).
   * Alignment: Center table alignment, full page width responsive column fitting.

---

### 2.3 Callout Containers & Blockquotes
Markdown `> blockquote` callouts are converted into styled visual note containers:
* **Background Shading**: `#EEF4F9` (Soft Ice Blue).
* **Left Accent Bar**: `<w:pBdr><w:left w:val="single" w:sz="24" w:space="12" w:color="17365D"/></w:pBdr>` (3.0pt thick solid accent line).
* **Typography**: 10.0pt `Microsoft YaHei`, color `#17365D`.
* **Paragraph Spacing**: Space Before `4 pt`, Space After `6 pt`, Line Spacing `1.30x`.

---

### 2.4 Heading Underline Accent Rule
Heading 1 paragraphs (`# Heading 1` in MD) must feature a distinct visual bottom separator rule stretching across the text column:
* XML Attribute: `<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="3" w:color="D9E2F3"/></w:pBdr>` (0.75pt line, light blue `#D9E2F3`).

---

### 2.5 Code Blocks & Technical Formulas
Markdown fenced code blocks (```code ```) are styled as standalone technical code snippets:
* **Font**: Monospace `Consolas` (ASCII) & `Microsoft YaHei` (EastAsia), Size `8.5 pt`.
* **Background**: Shading fill `#F2F4F7`.
* **Left Border**: Subtle light slate gray border `#B0C4DE`.
* **Paragraph Spacing**: Space Before `4 pt`, Space After `8 pt`, Line Spacing `1.15x`.

---

### 2.6 Running Headers & Footers
1. **Cover Section**: No header or footer.
2. **Body Section**:
   * **Header**: Right-aligned document title in 9.0pt gray `#666666` (`Arial` / `SimSun`).
   * **Footer**: Center-aligned page number in 9.0pt gray `#666666`, formatted as `— 页码 —`.

---

## 3. Automation Implementation Guide

To execute this conversion programmatically, use the included Python script located at `scripts/convert_md_to_docx.py`.

### 3.1 Script Execution Command
```bash
python .agents/skills/markdown-to-professional-docx/scripts/convert_md_to_docx.py <input_document.md> [output_document.docx]
```

### 3.2 Key Dependencies
* `python-docx` (`pip install python-docx`)

---

## 4. Quality Verification Checklist
When converting any Markdown document to Word using this skill, verify against the following 6 checkpoints:

1. [ ] **Cover Page**: Does it have the category badge, 26pt Navy title, and 2-column Metadata Summary Box with Ice Blue background?
2. [ ] **Heading 1 Accent**: Does every H1 title have a solid bottom border line in `#D9E2F3`?
3. [ ] **Tables**: Are table headers Dark Navy (`#17365D`) with white bold text? Do data rows alternate shading (`#F7F9FB` vs `#FFFFFF`)? Are `tblHeader` and `cantSplit` enabled?
4. [ ] **Callouts**: Do `> blockquote` callouts render with soft blue background (`#EEF4F9`) and a thick left border (`#17365D`)?
5. [ ] **Body Readability**: Is text rendered in `#1F1F1F` (off-black) at 10.5pt with 1.45x line height and 6.0pt after spacing?
6. [ ] **Headers & Footers**: Is the first page clean (no header/footer)? Do body pages show the running title header and centered page footer?
