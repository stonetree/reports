# AI Agent 文档智能处理环境安装与自动配置全指南 (PDF / PPTX / Excel / OCR)

本指南旨在为 **Antigravity**、**Codex** 及其他 AI Agent 提供一套工业级、开箱即用的本地文档分析与生成环境。在新机器上，无论是由用户手动执行，还是直接将本指南投喂给新机器上的 Agent，均可按照本流程实现**一键式标准化部署**。

---

## 目录
1. [核心软件与依赖清单](#一核心软件与依赖清单)
2. [底层二进制工具安装与路径推荐](#二底层二进制工具安装与路径推荐)
3. [Windows 环境变量自动化配置](#三windows-环境变量自动化配置)
4. [Python 全局环境与依赖包安装](#四python-全局环境与依赖包安装)
5. [Agent 自动化技能与策略注入 (Antigravity & Codex)](#五agent-自动化技能与策略注入)
6. [一键自动化配置 PowerShell 脚本 (推荐)](#六一键自动化配置-powershell-脚本)
7. [环境验收与连通性验证](#七环境验收与连通性验证)

---

## 一、核心软件与依赖清单

| 类别 | 工具/库名 | 核心作用与使用场景 |
| :--- | :--- | :--- |
| **二进制工具** | **Poppler for Windows** | PDF 页面转 SVG/PNG、提取内嵌原图、PDF 合并/拆分、提取排版文字 |
| **二进制工具** | **Tesseract-OCR** | 本地 OCR 核心引擎，支持中英双语识别（`chi_sim`, `eng`） |
| **系统级 CLI** | **ImageMagick** (可选) | PDF 裁出图表的**秒级白边自动切除**、背景透明化处理 |
| **系统级 CLI** | **Pandoc** (可选) | Markdown 与 PPTX / Docx 万能跨文档双向转换 |
| **系统级应用** | **Microsoft Office** | PPTX / Excel 视觉渲染、COM 自动化排版校验与无头转图 |
| **Python: PDF/OCR** | `pytesseract`, `pdf2image`, `PyMuPDF (fitz)`, `pdfplumber`, `docling` | PDF 毫秒级解析、自动版面切图、OCR 提取、多线表格抽数 |
| **Python: PPTX/办公** | `python-pptx`, `pywin32` | 生成 PPTX 原生可编辑图表、架构图，执行 Windows COM 自动化 |
| **Python: 数据分析** | `pandas`, `openpyxl`, `xlsxwriter`, `seaborn`, `matplotlib` | Excel 数据深度处理、高管商务风数据图表生成 |

---

## 二、底层二进制工具安装与路径推荐

> [!TIP]
> 建议将二进制工具安装在**非临时、不会被清理软件误删的固定目录**（如 `C:\Program Files\` 或 `D:\Tools\`）。

### 1. 安装 Tesseract-OCR
* **官方/推荐下载**：[UB-Mannheim Tesseract Windows Installer](https://github.com/UB-Mannheim/tesseract/wiki) 或通过 winget 安装：
  ```powershell
  winget install UB-Mannheim.TesseractOCR
  ```
* **默认安装路径**：`C:\Program Files\Tesseract-OCR`
* **必备语言包**：确保安装或下载了以下 `.traineddata` 文件放入 `C:\Program Files\Tesseract-OCR\tessdata\`：
  * `chi_sim.traineddata`（简体中文）
  * `eng.traineddata`（英文）
  * `osd.traineddata`（方向检测）

### 2. 安装 Poppler for Windows
* **官方/推荐下载**：[Poppler for Windows (Release 版)](https://github.com/oschwartz10612/poppler-windows/releases)
* **推荐安装路径**：解压至 `C:\Tools\poppler` 或 `D:\Tools\poppler`
  * 核心可执行文件目录为：`<poppler_dir>\Library\bin`（包含 `pdftoppm.exe`, `pdftocairo.exe`, `pdfimages.exe`, `pdftotext.exe`, `pdfunite.exe` 等）

### 3. 安装辅助 CLI 工具（可选推荐）
```powershell
winget install ImageMagick.ImageMagick
winget install JohnMacFarlane.Pandoc
```

---

## 三、Windows 环境变量自动化配置

AI Agent 及其派生的子终端通过读取环境变量 `PATH` 和 `TESSDATA_PREFIX` 来定位工具。

以普通权限运行以下 PowerShell 脚本，自动将路径追加至**当前用户级环境变量**（免管理员权限、不污染系统关键目录）：

```powershell
# ==================== 1. 定义工具路径 ====================
# 请根据实际安装路径微调以下变量：
$popplerBin = "C:\Tools\poppler\Library\bin"
$tesseractDir = "C:\Program Files\Tesseract-OCR"
$tessdataDir = "$tesseractDir\tessdata"

# ==================== 2. 设置 TESSDATA_PREFIX ====================
[System.Environment]::SetEnvironmentVariable("TESSDATA_PREFIX", $tessdataDir, "User")
Write-Host "✅ 已配置 TESSDATA_PREFIX -> $tessdataDir"

# ==================== 3. 将工具目录添加至用户 PATH ====================
$currentUserPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
$pathsToAdd = @($popplerBin, $tesseractDir)

foreach ($p in $pathsToAdd) {
    if (Test-Path $p) {
        if ($currentUserPath -notlike "*$p*") {
            $currentUserPath = "$currentUserPath;$p".TrimStart(";")
            Write-Host "✅ 成功添加至 PATH: $p"
        } else {
            Write-Host "ℹ️ 已存在于 PATH: $p"
        }
    } else {
        Write-Warning "⚠️ 路径不存在，请检查: $p"
    }
}

[System.Environment]::SetEnvironmentVariable("Path", $currentUserPath, "User")
Write-Host "🎉 环境变量持久化更新完成！"
```

> [!IMPORTANT]
> **生效说明**：修改环境变量后，必须**完全退出并重启** IDE（Antigravity、VS Code）、终端（PowerShell）或 Codex 客户端，新启动的进程才会继承新 PATH。

---

## 四、Python 全局环境与依赖包安装

为了避免虚拟环境隔离导致 Agent 每次都重复报错，推荐在**全局 Host Python 环境**中安装以下库：

```powershell
# 1. 核心图像与 OCR 接口
pip install pytesseract pdf2image pillow

# 2. 高效 PDF 解析与版面分析
pip install pymupdf pdfplumber pypdf docling

# 3. PPTX 与 Excel 原生交互
pip install python-pptx pywin32 openpyxl xlsxwriter

# 4. 数据分析与高质感图表绘制
pip install pandas numpy seaborn matplotlib
```

---

## 五、Agent 自动化技能与策略注入

为了让 Agent **自发、无感地在后台调用这些工具**（而不是向用户询问命令），必须配置 Agent 的技能与规则文件。

### 1. Antigravity 技能配置

#### (1) 创建全局 OCR 技能文件
创建文件：`~/.gemini/config/skills/ocr/SKILL.md`
```markdown
---
name: ocr
description: Automatic OCR text extraction and image/scanned-PDF processing. Activate whenever the user asks to extract text from images (PNG, JPG, JPEG, WEBP, BMP, TIFF), photos, screenshots, or scanned PDF documents, or asks for "OCR", "文字识别", "识别图片", "读取截图", "提取扫描件内容". Automatically uses local Tesseract-OCR (chi_sim, eng) and Poppler (pdftoppm, pdf2image) without asking the user for commands or tool paths.
---

# Local OCR & Image Text Extraction Guide

## Local Environment Status
- **Tesseract-OCR**: Available in PATH (`tesseract.exe`) with `chi_sim`, `eng`, `osd`.
- **Poppler**: Available in PATH (`pdftoppm.exe`, `pdftocairo.exe`, `pdfimages.exe`, `pdftotext.exe`).
- **Python Libraries**: `pytesseract`, `pdf2image`, `Pillow` are installed globally.

## Golden Rules
1. **Zero User Overhead**: Never ask the user what command to run, where Tesseract is installed, or which parameters to use. Execute automatically.
2. **Dual-Language Default**: Always specify `lang='chi_sim+eng'` unless the user explicitly requests another language.
3. **Automatic Fallback for PDFs**: If digital extraction yields empty text, immediately assume it is a scanned document and run the PDF-to-Image OCR pipeline.

## Quick Python Invocation
```python
import pytesseract
from PIL import Image
from pdf2image import convert_from_path

# Image OCR
text = pytesseract.image_to_string(Image.open("pic.png"), lang="chi_sim+eng")

# Scanned PDF OCR
images = convert_from_path("scanned.pdf", dpi=300)
pdf_text = "\n\n".join([pytesseract.image_to_string(img, lang="chi_sim+eng") for img in images])
```
```

#### (2) 更新全局 PDF 技能回退规则
在 `~/.gemini/config/skills/pdf/SKILL.md` 中增加以下约束：
```markdown
> [!IMPORTANT]
> **Automatic Scanned PDF Fallback Rule**:
> The local machine is equipped with **Poppler** (`pdftoppm`, `pdftotext`) and **Tesseract-OCR** (`chi_sim`, `eng`), along with `pdf2image` and `pytesseract`.
> If digital text extraction (`pdfplumber` or `pypdf`) returns empty or sparse content (typical for scanned documents), **NEVER tell the user the PDF is blank and NEVER ask the user how to proceed**. Automatically and immediately fall back to the OCR pipeline using `pdf2image` and `pytesseract` (`lang='chi_sim+eng'`).
```

---

### 2. Codex 全局策略配置

#### (1) 同步创建 Codex OCR 技能
将上述相同的 `SKILL.md` 写入到：`~/.codex/skills/ocr/SKILL.md`。

#### (2) 追加全局策略至 `~/.codex/AGENTS.md`
在 `~/.codex/AGENTS.md` 末尾追加以下章节：
```markdown
---

# Local Tool Execution Policy: Poppler & Tesseract-OCR

The host environment has permanently configured the following deterministic local tools:

1. **Poppler**: Available globally in PATH (`pdftoppm`, `pdftocairo`, `pdfimages`, `pdftotext`).
2. **Tesseract-OCR**: Available globally in PATH (`tesseract.exe`) with `chi_sim`, `eng`, and `osd` traineddata.
3. **Python Libraries**: `pdf2image`, `pytesseract`, and `Pillow` are installed globally.

### Autonomous Invocation Directives:
* **Zero User Friction**: Never ask the user for command syntax, tool paths, or manual intervention. Call the tools automatically.
* **Scanned PDF Auto-Fallback**: When reading a PDF, if digital extraction returns empty or sparse text, automatically convert pages to images via `pdf2image` and OCR each page with `pytesseract` (`lang='chi_sim+eng'`).
* **Image OCR**: When processing images (PNG, JPG, screenshots, receipts), run `pytesseract` or `tesseract` directly with `chi_sim+eng`.
```

---

## 六、一键自动化配置 PowerShell 脚本

将以下脚本保存为 `setup_agent_doc_env.ps1`，在新机器上以普通 PowerShell 运行，即可**全自动完成上述全部步骤**：

```powershell
<#
.SYNOPSIS
    AI Agent 本地文档智能处理环境一键初始化脚本
#>

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " 🚀 正在初始化 AI Agent 文档智能环境 (PDF/PPTX/Excel/OCR)... " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. 探测关键工具路径
$tessPath = "C:\Program Files\Tesseract-OCR"
$popplerCandidates = @(
    "C:\Tools\poppler\Library\bin",
    "D:\Tools\poppler\Library\bin",
    "D:\Downloads\poppler-26.07.0\Library\bin"
)

$popplerPath = ""
foreach ($cand in $popplerCandidates) {
    if (Test-Path "$cand\pdftoppm.exe") {
        $popplerPath = $cand
        break
    }
}

# 2. 配置环境变量
$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
$modified = $false

if (Test-Path $tessPath) {
    [System.Environment]::SetEnvironmentVariable("TESSDATA_PREFIX", "$tessPath\tessdata", "User")
    if ($userPath -notlike "*$tessPath*") {
        $userPath = "$userPath;$tessPath".TrimStart(";")
        $modified = $true
    }
    Write-Host "✅ Tesseract 环境变量已配置" -ForegroundColor Green
} else {
    Write-Warning "⚠️ 未找到 Tesseract 默认路径，请确认是否已安装至 C:\Program Files\Tesseract-OCR"
}

if ($popplerPath -ne "") {
    if ($userPath -notlike "*$popplerPath*") {
        $userPath = "$userPath;$popplerPath".TrimStart(";")
        $modified = $true
    }
    Write-Host "✅ Poppler 环境变量已配置 ($popplerPath)" -ForegroundColor Green
} else {
    Write-Warning "⚠️ 未找到 Poppler，请下载解压后将其 Library\bin 目录配置到 PATH"
}

if ($modified) {
    [System.Environment]::SetEnvironmentVariable("Path", $userPath, "User")
}

# 3. 安装 Python 核心依赖
Write-Host "`n📦 正在安装 Python 依赖库..." -ForegroundColor Cyan
pip install pytesseract pdf2image pillow pymupdf pdfplumber python-pptx pywin32 openpyxl xlsxwriter pandas seaborn matplotlib

# 4. 配置 Agent 技能目录与规则
Write-Host "`n⚙️ 正在写入 Agent Skills 与规则..." -ForegroundColor Cyan
$userProfile = [System.Environment]::GetFolderPath("UserProfile")

$ocrSkillContent = @'
---
name: ocr
description: Automatic OCR text extraction and image/scanned-PDF processing. Activate whenever the user asks to extract text from images (PNG, JPG, JPEG, WEBP, BMP, TIFF), photos, screenshots, or scanned PDF documents, or asks for "OCR", "文字识别", "识别图片", "读取截图", "提取扫描件内容". Automatically uses local Tesseract-OCR (chi_sim, eng) and Poppler (pdftoppm, pdf2image) without asking the user for commands or tool paths.
---

# Local OCR & Image Text Extraction Guide

## Local Environment Status
- **Tesseract-OCR**: Available in PATH (`tesseract.exe`) with `chi_sim`, `eng`, `osd`.
- **Poppler**: Available in PATH (`pdftoppm.exe`, `pdftocairo.exe`, `pdfimages.exe`, `pdftotext.exe`).
- **Python Libraries**: `pytesseract`, `pdf2image`, `Pillow` are installed globally.

## Golden Rules
1. **Zero User Overhead**: Never ask the user what command to run, where Tesseract is installed, or which parameters to use. Execute automatically.
2. **Dual-Language Default**: Always specify `lang='chi_sim+eng'` unless the user explicitly requests another language.
3. **Automatic Fallback for PDFs**: If digital extraction yields empty text, immediately assume it is a scanned document and run the PDF-to-Image OCR pipeline.
'@

# 写入 Antigravity 技能
$geminiSkillDir = "$userProfile\.gemini\config\skills\ocr"
New-Item -ItemType Directory -Force -Path $geminiSkillDir | Out-Null
Set-Content -Path "$geminiSkillDir\SKILL.md" -Value $ocrSkillContent -Encoding UTF8
Write-Host "✅ 已写入 Antigravity OCR 技能" -ForegroundColor Green

# 写入 Codex 技能
$codexSkillDir = "$userProfile\.codex\skills\ocr"
New-Item -ItemType Directory -Force -Path $codexSkillDir | Out-Null
Set-Content -Path "$codexSkillDir\SKILL.md" -Value $ocrSkillContent -Encoding UTF8
Write-Host "✅ 已写入 Codex OCR 技能" -ForegroundColor Green

Write-Host "`n🎉 环境初始化全部完成！请重启终端或 IDE 以生效。" -ForegroundColor Yellow
```

---

## 七、环境验收与连通性验证

新终端打开后，运行以下三组命令进行自动化闭环验证：

### 1. CLI 命令行验证
```powershell
pdftoppm -v
pdftotext -v
tesseract --version
tesseract --list-langs
```
*预期输出*：均正常显示版本号，且 `tesseract --list-langs` 包含 `chi_sim`、`eng`。

### 2. Python 联动 OCR 端到端验证
```powershell
python -c "from PIL import Image, ImageDraw; img = Image.new('RGB', (200, 60), color='white'); d = ImageDraw.Draw(img); d.text((10, 10), 'Hello OCR 123', fill='black'); import pytesseract; print('OCR 识别成功:', pytesseract.image_to_string(img).strip())"
```
*预期输出*：`OCR 识别成功: Hello OCR 123`

### 3. Agent 实际调用测试
向 Antigravity 或 Codex 输入测试 Prompt：
> *“帮我用本地 OCR 识别当前目录下的测试图片/扫描 PDF，并告诉我里面包含什么文字。”*

如果 Agent 在不向您索要路径或命令的前提下，自主调用 Python 或命令行给出识别文本，即证明部署大功告成！
