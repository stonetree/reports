# 开发人员技术串讲 PPT 设计排版与文风规范标准

本规范沉淀自《统一异构 KVCache 存储池 关键技术提前验证方案串讲》PPTX 套件的实际迭代与交付经验。专门用于后续为开发人员、架构评审与技术攻坚团队制作类似的技术方案、原型验证、架构设计串讲演示文稿。

---

## 一、文风基调与表达方式规范 (Tone & Text Style)

面向一线研发与架构技术人员时，语言必须以**务实朴实、平实直白、数据驱动、严谨落地**为主，彻底杜绝行政化套话、公关宣传口号与过度渲染的修饰词。

### 1. 常见过度修饰词 vs 推荐平实表达对照库

| 序号 | ❌ 不合适表达（过于修饰/生涩/夸张） | ✅ 推荐平实表达（工程师语境） | 修正理由与使用场景说明 |
| :---: | :--- | :--- | :--- |
| **1** | **竞争力咬合** | **对整体项目的支撑 / 验证目标与价值** | 去除空洞夸大词汇，用平实工程语言讲清该项验证对整体系统收益的作用。 |
| **2** | **四大证据门体系** | **四个核心验证阶段与目标 (E0~E3)** | 避免行政化生硬口吻，按研发工程演进清晰划分为收益、传输、调度、端到端四个阶段。 |
| **3** | **Host CPU 正文触碰严格为 0** | **Host CPU 零数据拷贝 (CPU 仅负责下发控制指令，不参与正文搬运)** | 用业界通用的“零拷贝”标准术语替换不知所谓的“正文触碰”，明确交代 CPU 职责。 |
| **4** | **硬件 CapabilityMatrix 探针能力表** | **硬件能力矩阵（即在运行时自动探测各通信链路的带宽、时延等物理参数表）** | 针对专有英文增加直白的功能释义，让开发人员一目了然其用途是供调度决策读取。 |
| **5** | **SemanticQoS 干扰包络** | **前后台服务质量保障策略（确保后台换出时不影响前台在线推理的请求时延）** | 去除晦涩学术词，用直白的大模型推理业务场景说明其目标是保护前台时延。 |
| **6** | **TP=8 RankConsensus 空间共识 P99** | **张量并行 (TP=8) 多卡状态同步耗时 P99 < 100µs** | 用开发熟悉的“张量并行”与“多卡状态同步”替代过于学术的“空间共识”。 |
| **7** | **购买不可辩驳的第一方工程事实** | **为后续系统实现提供扎实的技术依据** | 严禁使用“购买/采购事实”等不通顺且夸张的表达，回归工程师务实对齐。 |
| **8** | **绝不自嗨 / 铸就第一方护城河 / 誓师号角** | **摸清技术边界 / 以客观实测数据为依据 / 保障后续顺利落地** | 封面和尾页去除口号式渲染，平实交代串讲目的、实施准则与预期交付。 |
| **9** | **研发团队排期 / RACI 矩阵 / 人日预算** | **（串讲技术方案时完全剥离，仅保留技术方案与量化门限）** | 方案串讲聚焦在软硬件环境、Benchmark 方案与指标判定，不混入项目管理排期。 |

### 2. 专有英文术语使用规范
- 任何技术缩写或专有构词在幻灯片中首次出现时，必须在括号内补充一句话**直白功能释义**：
  - *例*：`Direct-View（远端直读）`
  - *例*：`Copy-to-HBM（拷贝到本地显存）`
  - *例*：`CostEvaluator（成本预估模型）`
  - *例*：`Saved-Prefill（首字生成复用节省）`
  - *例*：`Staging Fanout（软件树状分层转发）`

---

## 二、视觉设计系统与色彩 Token (Design Tokens)

全套幻灯片统一采用**全白底极简科技感**风格，确保投影仪与高分屏阅读清晰、清爽通透。

| Token 名称 | 颜色色值 (HEX) | PPTX RGB 定义 | 适用元素与设计语义 |
| :--- | :--- | :--- | :--- |
| `BG_WHITE` | `#FFFFFF` | `RGBColor(255, 255, 255)` | **页面背景全白底**，包括封面页、全景页、内容页与尾页。 |
| `CARD_WHITE` | `#FFFFFF` | `RGBColor(255, 255, 255)` | 容器卡片纯白底色。 |
| `BORDER_BLUE` | `#BFDBFE` | `RGBColor(191, 219, 254)` | 卡片浅蓝描边（线宽 `Pt(1.0)` ~ `Pt(1.2)`），界定卡片边界。 |
| `NAVY_PRIMARY` | `#1E3A8A` | `RGBColor(30, 58, 138)` | **皇家藏蓝**：幻灯片主标题、常规卡片 Header 背景、底部彩条。 |
| `NAVY_HEADER` | `#0F172A` | `RGBColor(15, 23, 42)` | 深沉藏青：结论条副标头。 |
| `BLUE_ACCENT` | `#2563EB` | `RGBColor(37, 99, 235)` | **经典蓝**：封面分类英文、页码角标、结论横条标头高亮。 |
| `TEXT_MAIN` | `#1E293B` | `RGBColor(30, 41, 59)` | **正文深灰**：正文列表与描述主体文字。 |
| `TEXT_MUTED` | `#475569` | `RGBColor(71, 85, 105)` | 次级副标题、页面注脚、辅助解释文字。 |
| `TEXT_WHITE` | `#FFFFFF` | `RGBColor(255, 255, 255)` | 卡片 Header 内的主标题文字。 |
| `RED_HIGHLIGHT` | `#DC2626` | `RGBColor(220, 38, 38)` | **鲜红强调**：核心指标、门禁阈值、关键性能量化数据。 |
| `EMERALD_GREEN`| `#059669` | `RGBColor(5, 150, 105)` | 翡翠绿：达标指标、次级高亮。 |
| `PURPLE_HEADER` | `#6D28D9` | `RGBColor(109, 40, 217)` | 典雅紫：第 4 卡片（达标要求/判定标准）Header 背景。 |

---

## 三、排版体系与字号间距规范 (Typography & Spacing)

### 1. 字体体系
- **中文字体**：`Microsoft YaHei`（微软雅黑）。
- **英文字体/代码/协议名**：`Consolas`（或 `Microsoft YaHei` 混排）。

### 2. 严格的层级字号标准
- **封面大标题 (Cover Title)**：`26pt Bold`（皇家藏蓝 `#1E3A8A`）
- **页面主标题 (Slide Header Title)**：`19pt ~ 20pt Bold`（皇家藏蓝 `#1E3A8A`）
- **页面副标题 (Slide Subtitle)**：`11pt ~ 12pt Regular`（石板灰 `#475569`）
- **页码角标 (Page Number Badge)**：`11.5pt Bold`（经典蓝 `#2563EB`，右对齐）
- **卡片 Header 标题**：`12pt Bold`（白色文字，`word_wrap = False` 杜绝多行折断）
- **卡片 Header Tag 标签**：`10pt Bold`（浅蓝色 `#BFDBFE`，格式 `[阶段目标]`）
- **正文列表文字 (Body Bullet Points)**：**`11.5pt`**（统一基准：`11pt ~ 12pt`）
  - 列表引导词（Prefix）：`11.5pt Bold`（皇家藏蓝）
  - 正文内容：`11.5pt Regular`（深灰 `#1E293B`）
  - 重点量化数据：`11.5pt Bold`（鲜红 `#DC2626` 或 经典蓝 `#2563EB`）
  - 接口/代码片段：`11pt Consolas Bold`
- **底部核心结论横条 (Takeaway Banner)**：
  - 结论标头：`12pt Bold`（经典蓝 + 藏青）
  - 结论正文：`11.5pt Bold`，核心数字 `11.5pt Bold 鲜红 #DC2626`

### 3. 行间距与段落间隔规范
- **列表项段落后间距 (Paragraph Space After)**：**`space_after = Pt(6)`**（每个 `•` 列表项之间必须留出 6pt 呼吸空间）。
- **文本行距 (Line Spacing)**：**`line_spacing = 1.20`**。
- **文本框边距 (Margins)**：显式清除默认边距 `tf.margin_left = tf.margin_top = tf.margin_right = tf.margin_bottom = 0`，防止系统默认内边距引起意外折行与文字遮挡。

---

## 四、三大经典幻灯片版式模型 (Layout Grid Models)

### 模型 1：2×2 对称四卡片标准方案页（用于单个技术验证项）

用于呈现具体验证项的完整方案设计，画布尺寸 13.333" × 7.50"。

```
+-----------------------------------------------------------------------------------------+
| [Header] 页面主标题 (19pt Bold)                                       第 XX / 14 页 (11.5pt) |
|          页面副标题与技术目标说明 (11pt Regular)                                            |
+----------------------------------------------------+------------------------------------+
| 1. 验证背景、必要性与目标定位 [阶段目标] (藏青Header) | 2. 所需软硬件环境与技术规范 [环境依赖] (藏青)  |
| • 验证背景：... (11.5pt, 6pt spacing)              | • 硬件环境：... (11.5pt, 6pt spacing)       |
| • 验证目的：...                                    | • 驱动与 SDK 规范：...                     |
| • 对整体项目的支撑：...                             | • 编译参数：...                             |
| (Left=0.50", Top=1.15", W=6.05", H=2.45")          | (Left=6.78", Top=1.15", W=6.05", H=2.45")  |
+----------------------------------------------------+------------------------------------+
| 3. 测试数据集与 Benchmark 方案 [测试方法] (藏青)    | 4. 输出数据要求与判定标准 [达标要求] (紫色Header)|
| • 测试数据集构造：... (11.5pt, 6pt spacing)         | • 关键交付物：... (11.5pt, 6pt spacing)     |
| • 微基准测试：...                                  | • 判定达标要求 (Go/No-Go)：                 |
| • 框架源码打点：...                                |   1. 核心指标一 (鲜红加粗)                   |
| (Left=0.50", Top=3.70", W=6.05", H=2.58")          |   2. 核心指标二 (鲜红加粗)                   |
|                                                    | (Left=6.78", Top=3.70", W=6.05", H=2.58")  |
+----------------------------------------------------+------------------------------------+
| [|] 【方案核心结论】 标题：量化结论文字 (11.5pt Bold, 核心数据鲜红 #DC2626 高亮)            |
| (Left=0.50", Top=6.38", Width=12.333", Height=0.76", 左侧0.08"皇家蓝色条)                |
+-----------------------------------------------------------------------------------------+
```

### 模型 2：三栏卡片页（用于封面页与尾页）
- **封面页 (Cover Slide)**：
  - 左侧纵向皇家蓝色条：`Left=0.60", Top=0.80", Width=0.20", Height=5.90"`
  - 顶部英文 Badge：`11pt Consolas Bold`
  - 主标题：`26pt Bold Navy`（两行）+ 副标题：`12pt Slate`
  - 底部 3 个并排卡片：`Width=3.70", Height=2.20", Top=3.45", Gap=0.35"`
- **总结页 (Closing Slide)**：
  - 顶部主副标题：`24pt Bold Navy` + `12pt Slate`
  - 中间 3 个大卡片：`Width=3.80", Height=3.90", Top=1.95", Gap=0.466"`
  - 底部总结横条：`Left=0.60", Top=6.20", Width=12.133", Height=0.78"`

### 模型 3：架构全景页（用于顶层总体路线与分层）
- 左侧全高卡片（四大验证阶段 E0~E3 映射）：`Width=6.05", Height=5.13", Top=1.15"`
- 右上卡片（核心技术组件与分层）：`Width=6.05", Height=2.45", Top=1.15"`
- 右下卡片（方案验证实施原则）：`Width=6.05", Height=2.55", Top=3.73"`
- 底部总体路线结论横条：`Left=0.50", Top=6.38", Width=12.333", Height=0.76"`

---

## 五、可直接复用的 Python 渲染代码模板

以下为自动化构建该标准 PPTX 的核心 Python 驱动模板：

```python
# -*- coding: utf-8 -*-
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.enum.text import PP_ALIGN
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE

# 视觉色彩 Token
BG_WHITE = RGBColor(255, 255, 255)
CARD_WHITE = RGBColor(255, 255, 255)
BORDER_BLUE = RGBColor(191, 219, 254)
NAVY_PRIMARY = RGBColor(30, 58, 138)
NAVY_HEADER = RGBColor(15, 23, 42)
BLUE_ACCENT = RGBColor(37, 99, 235)
TEXT_MAIN = RGBColor(30, 41, 59)
TEXT_MUTED = RGBColor(71, 85, 105)
TEXT_WHITE = RGBColor(255, 255, 255)
RED_HIGHLIGHT = RGBColor(220, 38, 38)
PURPLE_HEADER = RGBColor(109, 40, 217)

FONT_MAIN = "Microsoft YaHei"
FONT_CODE = "Consolas"

def create_base_presentation():
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.50)
    return prs

def set_slide_background(slide):
    background = slide.background
    fill = background.fill
    fill.solid()
    fill.fore_color.rgb = BG_WHITE

def add_rect_card(slide, left, top, width, height, bg_color=CARD_WHITE, border_color=BORDER_BLUE, border_width=Pt(1.0)):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = bg_color
    if border_color:
        shape.line.color.rgb = border_color
        shape.line.width = border_width
    else:
        shape.line.fill.background()
    return shape

def add_card_header(slide, left, top, width, height, title, tag_text=None, bg_color=NAVY_PRIMARY, text_color=TEXT_WHITE):
    add_rect_card(slide, left, top, width, height, bg_color=bg_color, border_color=None)
    tb = slide.shapes.add_textbox(left + Inches(0.12), top + Inches(0.04), width - Inches(0.24), height - Inches(0.08))
    tf = tb.text_frame
    tf.word_wrap = False
    tf.margin_left = tf.margin_top = tf.margin_right = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.text = title
    p.font.name = FONT_MAIN
    p.font.size = Pt(12)
    p.font.bold = True
    p.font.color.rgb = text_color
    if tag_text:
        run_tag = p.add_run()
        run_tag.text = f"  [{tag_text}]"
        run_tag.font.name = FONT_MAIN
        run_tag.font.size = Pt(10)
        run_tag.font.bold = True
        run_tag.font.color.rgb = RGBColor(191, 219, 254)
    return tb

def add_bullet_points(text_frame, items, default_size=11.5, space_after=6):
    for i, item in enumerate(items):
        p = text_frame.paragraphs[0] if i == 0 else text_frame.add_paragraph()
        p.space_after = Pt(space_after)
        p.line_spacing = 1.20
        
        prefix = item.get("prefix", "")
        if prefix:
            r = p.add_run()
            r.text = prefix
            r.font.name = FONT_MAIN
            r.font.size = Pt(default_size)
            r.font.bold = True
            r.font.color.rgb = NAVY_PRIMARY
            
        for text, style in item.get("parts", []):
            r = p.add_run()
            r.text = text
            r.font.size = Pt(default_size)
            if style == "red":
                r.font.name = FONT_MAIN
                r.font.bold = True
                r.font.color.rgb = RED_HIGHLIGHT
            elif style == "blue":
                r.font.name = FONT_MAIN
                r.font.bold = True
                r.font.color.rgb = BLUE_ACCENT
            elif style == "code_bold":
                r.font.name = FONT_CODE
                r.font.bold = True
                r.font.color.rgb = NAVY_PRIMARY
            else:
                r.font.name = FONT_MAIN
                r.font.color.rgb = TEXT_MAIN

def add_takeaway_banner(slide, left, top, width, height, title, segments):
    add_rect_card(slide, left, top, width, height, bg_color=CARD_WHITE, border_color=BORDER_BLUE, border_width=Pt(1.2))
    add_rect_card(slide, left, top, Inches(0.08), height, bg_color=NAVY_PRIMARY, border_color=None)
    tb = slide.shapes.add_textbox(left + Inches(0.18), top + Inches(0.08), width - Inches(0.30), height - Inches(0.14))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_top = tf.margin_right = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.line_spacing = 1.22
    
    r_badge = p.add_run()
    r_badge.text = "【方案核心结论】 "
    r_badge.font.name = FONT_MAIN
    r_badge.font.size = Pt(12)
    r_badge.font.bold = True
    r_badge.font.color.rgb = BLUE_ACCENT
    
    r_title = p.add_run()
    r_title.text = f"{title}："
    r_title.font.name = FONT_MAIN
    r_title.font.size = Pt(12)
    r_title.font.bold = True
    r_title.font.color.rgb = NAVY_HEADER
    
    for seg_text, seg_style in segments:
        r = p.add_run()
        r.text = seg_text
        r.font.size = Pt(11.5)
        if seg_style == "red":
            r.font.name = FONT_MAIN
            r.font.bold = True
            r.font.color.rgb = RED_HIGHLIGHT
        else:
            r.font.name = FONT_MAIN
            r.font.color.rgb = TEXT_MAIN
```

---

## 六、交付物双版本归档准则 (Dual-Version Export)

为开发团队与技术评审输出 PPT 时，应始终配套输出两个版本：
1. **正式技术串讲版 (`*_完整版.pptx`)**：包含逐页 250~400 字的口语化讲稿备注（Speaker Notes），供主讲人排练与串讲使用。
2. **纯净预审版 (`*_预审版.pptx`)**：通过自动化脚本清空讲稿备注，方便向评审专家与各方分发，界面干净无冗余。
3. **高清 PNG 导图验证 (`png_previews/`)**：通过 PowerPoint COM API 自动导出 1920×1080 图像，借助图片查看工具验证有无折行截断或格式溢出，确保 100% 视觉无瑕疵。
