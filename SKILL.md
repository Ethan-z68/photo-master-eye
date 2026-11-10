---
name: photo-master-eye
description: >
  Photographer's-eye composition reframing (二次构图) of existing local images,
  grounded in Stephen Shore's first-principles framework of attention, framing and
  picture-plane relationships. STANDING USER DEFAULT: LOCK THE ASPECT RATIO, but
  ALWAYS reframe inside it — crop the dead sky / empty foreground, choose the
  frame edge so it lands on a real structure line (floor band, cornice, column),
  never slice a unique element (sign, person, corner arc), and level the roll only
  when a vertical is actually off by more than 0.3 degrees. An export that is
  pixel-identical to the source is a BUG, not a delivery. Leave the source DNG/RAW
  byte-for-byte untouched; skip the "which format / which ratio" questions; still
  give the Shore five-question diagnosis plus 2-3 candidate framings with
  trade-offs. Deliver the final image next to the original as `<stem>_final.<ext>`
  (never an `<stem>_out/` subfolder); every process artefact (preview, grid,
  candidates, full-size decode, compare sheet, crop.json) goes to the system temp
  dir `pme_work_<stem>/` and is deleted when the task ends.
  Use when the user gives a local photo and asks for 修图、裁剪、二次构图、重新构图、
  出片、大师级审美、摄影眼、构图诊断、裁成 4:5/1:1/16:9、无损保留 DNG、
  非破坏性编辑. Does NOT generate images and NO LONGER does color grading by default
  (that module was removed; if the user asks for 调色, say so and suggest darktable /
  Lightroom / a dedicated tool).
metadata:
  version: "1.6.3"
  license: "MIT"
  requires: "Python 3.9+, Pillow 10+, numpy (survey/level/panels); macOS `sips` only for RAW"
  network: "none - fully local, no API keys"
---

# Photo Master Eye — 摄影眼构图（纯构图 / 非破坏性优先）

## 这条 skill 解决什么

**审美判断由「看图 + 提问框架」完成，边界决策由人完成，像素操作由本地脚本完成。**

v1.2.0 起本 skill 是**纯构图工具**：

- **调色模块已移除**。理由见 `references/shore-first-principles.md` §2.6：
  Shore 讲「透明性」，结构是理解的表达，不是浇在内容上的酱汁；
  重口味风格化与之相悖。默认零调色，不再询问「要不要调色」。
- **默认比例锁死，取景必须动**（v1.5.0）：保持原图长宽比，但在比例内做二次构图
  （切冗余、移取景、必要时校旋转）。**成品与原图逐像素一致 = bug**。
- **原片不动**：原 DNG / RAW 一个字节都不写；只有真的裁了才写 `crop.json` / XMP。
- **成品就在原图旁边**（v1.4.1）：写 `<原文件名>_final.<ext>`，
  同目录、不建 `_out/` 子目录；所有过程产物进系统临时目录并在收尾时删除。
- 构图判断采用 Shore 的**第一性原理框架**（注意力路径 / 边界 / 平面关系 / 透明性），
  而不是三分法打分表。理论依据见 `references/shore-first-principles.md`。

**默认交互原则：诊断照给、候选照给，但不再逐次追问格式/比例，直接出片**（见下节）。
用户当次指定比例时按当次执行，并在结果里报告所选值。

> **v1.6.0（2026-11-10）**：新增 `survey` / `level` / `panels` 三个测量命令，
> 取景流程改为「先数字、后眼睛」。原因很直接：一次任务 96.9% 的时间不在
> 像素运算上，而在往返次数和每次重读上下文的成本上，而其中近半数工具调用
> 是临时手写的测量脚本。详见「性能」节。

> 别把 Shore 做成「风格开关」。他反对把 stylization 强加给作品。
> 本 skill 只负责问对问题、讲清取舍，**不宣称自动产出大师构图**。

## 默认决策：比例锁死、构图要做、原片不动、成品放旁边（v1.5.0 重写）

> **v1.5.0 重要更正（2026-11-10，用户当面澄清）**
> 我把 v1.3.0 的「不裁」错读成了「什么都不动」，结果连续几张成品与原图逐像素一致，
> 用户的原话是：「我的意思是不改变比例，不是让你不动」。
> 「不裁」= **不改变长宽比**，不是放弃取景判断。取景必须在比例内做，不做就是 bug。

用户在 2026-11-10 设为**以后默认**，适用于之后所有照片，除非当次覆盖：

1. **比例锁死**：保持原图长宽比（4:3 就还是 4:3），不改成 16:9 / 1:1 / 4:5，
   除非用户当次明确指定。这是「不裁」的真正含义。
2. **构图必须动**：每次都要在锁定的比例内重新取景——
   切掉冗余（死天空、空地面、半截被切物体）、调整取景大小与位置
   （`--zoom` 0.8–0.95 + `--crop` 位移）、必要时旋转矫正（`--rotate`）。
   **成品与原图逐像素一样 = 失败**，不管理由多充分。
3. **原片仍然不动**：原 DNG / RAW 一个字节不写。
4. **不裁剪到全幅就不写 XMP**：只有真的裁了（或用户要非破坏性参数）才写
   `crop.json` / `<stem>.xmp`；全幅 `HasCrop=True` 是无意义噪音。
5. **跳过「导出前确认」三问**：不再问交付形态 / 成片格式 / 画幅比例，
   但仍要输出 Shore 五问诊断 + 2–3 个候选及取舍理由，然后直接出片。
6. **留存策略（v1.4.0）**：任务结束只保留**原图 + 最终成品**；
   预览 / 网格 / 候选 / 解码稿 / 对比图 / 临时目录一律自动清除，不询问。
7. **产出位置（v1.4.1）**：成品放在**原图同一目录**，`<原文件名>_final.<ext>`，
   不建 `IMG_xxxx_out/` 之类子目录。详见下一节。

**覆盖方式（仅当次有效，覆盖的是「比例」，不是「要不要构图」）**：
用户当次说「裁成 4:5」「出 16:9」「导出一张 JPG」时按当次比例执行；
用户说「别动」「原样给我」时才是真的零处理，且要明确确认一次。

## 取景怎么做：比例内的五步（v1.6.0：先数字，后眼睛）

顺序固定，每步都要能说出理由。**每一步的测量都由脚本完成，不要手搓
Pillow/numpy 代码**——v1.5.2 之前 47.5% 的工具调用是临时测量脚本，
这是整条流程最贵的一项开销。

0. **先跑 `survey`**（v1.6.0）：一条命令给出结构线位置、内容框、
   可裁预算和方案表，替代「看缩略图 → 猜坐标 → 写脚本量 → 再看」这一整圈。
   ```bash
   python3 scripts/photo_lab.py survey "/path/IMG.DNG"
   ```
   它返回：`structure_lines`（楼层带/檐口/柱的中线位置，源像素）、
   `content_box_work`（真实内容边界）、`budget`（`dead_margins_source`
   / `slack_source` / `can_cut_both_dead_ends` / `verdict`）、
   `scenarios`（每个方案的 `box`、`area_removed`、`cuts_content_px`、
   `edge_penalty`，以及可直接粘贴的 `finish --crop`）。
   **方案表故意不打总分、不排序**——按「你愿意牺牲什么」自己选，
   见 `references/composition-rules.md` 第三节。
1. **找结构线**：先找画面里客观存在的水平/垂直参考——楼层石带、檐口、柱、
   路面边线、地平线。裁切边**必须落在这些线上**，不要横穿窗户、
   不要把行人/招牌/转角切成两半。
   结构线的位置**以 `survey` 的 `structure_lines` 为准**，不要靠缩略图目测：
   目测「塔尖在 1/8 高度」实际在 3/16，误差一百多像素，顶边就会切过头。
2. **先算可裁预算**：把两个必须留住的边界换算成百分比，剩下的才是能动的额度。
   `survey` 的 `budget` 已经算好（`slack_source` 就是可用额度）。
   **竖幅尤其要算**——竖幅的同比例内接框等于原框，`dx=dy=0`，
   能动多少完全取决于这两个边界，往往上下只能保一个
   （`can_cut_both_dead_ends: false` 时不要试图两头都切）。
3. **切冗余**：切掉信息量最低的带状区域（死天空、空地面），
   优先切「重复且可推断」的内容（一排同样的窗、一段同样的路面），
   绝不切「唯一」的内容（招牌、人物、转角弧线）。
4. **定位置**：主体落在三分或黄金分割上；保留画面唯一的纵深出口
   （街道、走廊、天际线），它是平衡大面积主体的唯一力量。
5. **校旋转**：**先量，别靠眼睛**，用 `level`：
   ```bash
   python3 scripts/photo_lab.py level "/path/IMG.DNG"
   ```
   - 它量**竖边**（Hough 投票 + 越界票丢弃 + NMS 取峰），返回
     `roll_deg` / `confidence` / `suggested_rotate`（可直接喂给
     `finish --rotate`），以及 `vertical_spread`——**先看 spread 再看角度**。
   - **必须有两侧证据（v1.6.1）**：`level` 要求竖边横跨画面 ≥35% 宽度、
     且左右各至少一条，否则无论一致度多高都降为 `low`、`roll_deg: null`。
     原因（IMG_0764 实测）：12 条「竖边」全部落在 x 16–22% 的同一面立面，
     一致读到 −1.80°，但**单面立面分不清滚转与俯仰 keystone**——
     仰拍高楼时该楼的竖边本来就会倾斜，倾多少取决于它离光轴多远。
     这种情况**当作 0°**，并在汇报里说明「证据只来自单侧，测不出来」。
   - 同理，`agree` 必须是真多数而不是相对多数：一致条数 < `max(3, 40%)`
     只给 `medium`（IMG_0896 是 3/12 → medium，而其真值是手工追踪格栅得到的 2.2°，
     `level` 给 0.95°，**不能直接照抄**）。
   - **近景横线（栏杆、马路牙子、江岸）不能用来判滚转**——它们是向远方收敛的
     3D 线，成像必然斜，斜度是透视。`level` 的输出里它们被单列在
     `horizontals`，只作结构参考，并且 `horizontal_cross_check` 会提示
     哪几条互相矛盾。
   - 自然场景（没有可用竖边）`level` 会返回 `confidence: none` 并说明原因，
     此时才回退到「单线连续性追踪 + 残差 <1.5px」的老办法。
   |角度| <0.3° 当 0°；0.3°–3° 用 `--rotate` **正向**矫正（`suggested_rotate`
   已经带好符号，直接抄）。把旋转当风格化手段必须由用户明确要求。
   方法与四个坑见 `references/composition-rules.md` 第五节。

**验收**：把成品缩到长边 1500 与原图并排看一眼——如果两张几乎分不出来，
说明取景没做，回去重做第 3 步。**这一眼只能看一次**，且必须用 `compare`
生成对比图，不要把原图和成品分两次看。

## 交付位置：成品放原图旁边，过程产物进临时目录（v1.4.1）

用户 2026-11-10 追加设定，适用于之后所有任务。理由：照片库是按文件名聚合的，
多一层 `<stem>_out/` 等于每次都要心算「这张成片对应哪张原片」；
而且 Camera Raw 的 sidecar 本来就要求与 RAW 同目录同名，分开放根本没意义。

| 类别 | 默认位置 | 例子 |
|---|---|---|
| **成品图** | 原图同目录 | `IMG_0862.DNG` → `IMG_0862_final.jpg` |
| **Camera Raw sidecar** | 原图同目录、同名（否则 Lightroom 不识别） | `IMG_0862.xmp` |
| 其余全部过程产物 | 系统临时目录 `pme_work_<stem>/` | 预览 / 网格 / 候选 / 解码稿 / 对比图 / `crop.json` |

脚本已按此实现（v1.4.1）：

- `finish` 默认输出 `src.parent/<stem>_final.<ext>`，不需要 `--out`；
- `grid` / `candidates` / `raw-preview` / `raw-export` / `develop` / `compare` /
  `crop-plan` 默认写 `work_dir_for(src)`，即 `$TMPDIR/pme_work_<stem>/`；
- `xmp-crop` 是**唯一例外**：默认写 `src.parent/<stem>.xmp`，因为
  sidecar 只有和 RAW 同目录同名才可能被 Adobe 系识别；
- `work_dir_for()` 会剥掉 `_final/_edited/_grid/_preview/_full` 后缀，
  保证同一张图的所有步骤共用一个工作目录，清理时一次删干净。

**反例**：早期把 `IMG_0896` 的成品写进 `IMG_0896_out/IMG_0896_final_16x9.jpg`，
结果同一张照片在库里分成两处。该目录已清空移除，成片改回与
`IMG_0896.DNG` **同级同目录**的 `IMG_0896_final_16x9.jpg`。
**以后不要再建输出子目录。**

## 何时用 / 何时不用

| 用 | 不用 |
|---|---|
| 已有本地照片，要重新构图、裁剪、出片 | 从文字生成新图 → 用 ImageGen |
| 要按用途裁成特定比例 | 抠图 / 去水印 / 人像美化 → 用 `buddy-image-processing` |
| 要「摄影眼」诊断：边界该落在哪 | 改人脸、换天空、加元素等生成式编辑 |
| 想让 DNG / RAW 无损保留、只记裁剪参数 | **调色** → 本 skill 已移除，用 darktable / Lightroom |
| 批量套同一套裁剪参数 | RAW 的完整 16-bit 显影 → 用 darktable / RawTherapee |

## 大文件与 RAW：不要「上传」，给本地路径

聊天附件的大小限制只作用于上传通道；Agent 可直接读本机磁盘。
让用户给**绝对路径**即可，60MB 的 DNG、几百 MB 的 TIFF 都不受影响。

RAW 不是「大号 JPG」，而是传感器数据，Pillow 读不了。macOS 自带 `sips`
（ImageIO）可只读解码 CR2/CR3/NEF/ARW/RAF/RW2/DNG/ORF/PEF 等，无需安装。

> **平台边界（v1.6.2）**：`sips` 是 macOS 独有的。**非 RAW 格式
> （JPEG / PNG / TIFF / HEIC）在任何系统上都走 Pillow**，Windows / Linux 全功能可用。
> **只有 RAW 需要 `sips`**，在 Windows 上会给出明确报错而不是崩溃；
> 绕过办法：先用相机或转换器导出一张 JPEG/TIFF，再对那张图跑本 skill。
> 排查这类问题时可用 `PME_NO_SIPS=1` 强制走 Pillow 分支，在 Mac 上复现
> Windows 行为。

```bash
P=~/.workbuddy/skills/photo-master-eye/scripts/photo_lab.py
S="/绝对路径/DSC1234.DNG"

python3 "$P" info        "$S"                       # 尺寸 / 格式（RAW 走 sips）
python3 "$P" raw-preview "$S" --max 2000 --out-dir "<工作目录>"   # 轻量预览
python3 "$P" grid        "<preview.jpg>" -o "<工作目录>/grid.jpg"
python3 "$P" candidates  "<preview.jpg>" --ratios "4:5,1:1,16:9" --out-dir "<工作目录>/c"
```

预览只用于**判断**，绝不当成图。`sips` 兼容受 Apple 相机列表限制，
解不出时脚本会提示降级（相机内导出 JPG / darktable-cli / rawpy）。

### EXIF 方向：必须先统一坐标口径（v1.2.1）

竖拍 RAW 常见 `Orientation=6`。`sips` 报的 `pixelWidth/Height` 是**传感器存储尺寸**
（如 4032×3024），但观看时是**转正后的尺寸**（3024×4032）。`sips` 会把方向标签
复制进导出的 JPG 却**不旋转像素**，而 Pillow 读取时会旋转 —— 于是预览元数据、
网格、候选、裁剪参数各自用不同的宽高与轴向，裁剪框会落到错的位置。

v1.2.1 已修复：

- `raw-preview` / `raw-export` 导出后立即把方向**烤进像素并删掉标签**（`normalize_orientation`）。
- `info` / `crop-plan` / `xmp-crop` 一律以 **DISPLAY 方向**为坐标基准，
  同时输出 `orientation`、`raw_stored_size`、`coordinate_system`。
- `_preview.json` 的 `raw_size` 也是 DISPLAY 尺寸；`scale_x/scale_y` 因此可用。

```text
检查：python3 "$P" info "$S"    # 看 orientation / width_height_basis
```

> **未验证**：Adobe Camera Raw 对「带 90° 方向 RAW」的 `crs:CropTop/Left` 如何映射，
> 本机无 Lightroom / ACR，脚本会在 `xmp-crop` 输出里给出专项警告。
> 遇到这类 RAW，务必让用户在自己的软件里核对一次；不认就用 `crop.json` 手动裁。
> `raw-export --keep-orientation` 可跳过规范化（高级用法，坐标退回存储轴向）。

---

## 性能：瓶颈与快路径（v1.4.0）

实测对象 `IMG_0862.DNG`（6656×4992、43.6MB、Apple M5、Pillow 13）：

| 环节 | 优化前 | 优化后 | 做法 |
|---|---|---|---|
| `info` 探测 RAW | 1.14s | **0.09s** | 先读 TIFF 头，失败才回退 sips |
| `crop-plan` | 1.04s | **0.09s** | 同上 |
| 预览 + 网格 | 2.07s（两次调用） | **1.21s** | `raw-preview --grid` 一次出两张 |
| 候选 4 个 | 0.16s / 5 个文件 | **0.11s / 1 个文件** | 缩略图在内存拼图，默认不落盘 |
| 出片 | 6.80s + 107MB 中间稿 | **1.30s + 5.7MB 临时** | `finish` 一条命令，临时目录 `finally` 删除 |
| 出片 + 旋转 | — | **1.46s**（+0.15s） | `--rotate` 在裁切后做一次双三次重采样，代价可忽略 |

**真正的瓶颈不是 Pillow 画图，是 RAW 解码次数。** 每多一次 `sips` 调用就多一次
全图解码，所以优化全部围绕「少解码、少编解码、少落盘」：

- `probe_raw()`：DNG 多为 TIFF 容器，Pillow 能只读头拿到尺寸与方向。
  **命中率因文件而异**：`IMG_0862.DNG` 命中（0.09s），
  `我与黄浦江.DNG` 抛 `UnidentifiedImageError` 自动回退 sips（0.54s）。
  输出里的 `probe` 字段会写明走了哪条路。
- `finish`：sips 解码一次到私有临时目录 → 可选裁切 → 直出成品 →
  `finally` 删临时目录。替代 `raw-export` + `develop` 两步，
  中间不再出现 100MB 级文件，失败也不会留垃圾。
- `candidates`：crop 后直接在内存生成缩略图拼 `contact_sheet.jpg`，
  默认不写单张；需要逐张看细节时才加 `--keep-crops`。
- 预览长边 **1600 就足够判断构图**；2000 只是更清楚，代价是后续每步多 56% 像素。

**哪些不值得优化**：`grid`（0.14s）、`compare`（0.31s）已在毫秒级；
再改只会增加代码复杂度。瓶颈永远在 RAW 解码和全尺寸编码。

### v1.6.0：真正的瓶颈是往返次数，不是脚本

2026-11-10 复盘一次真实的竖幅任务（`我与黄浦江.DNG`，单张），
从会话转录里量出来的账：

| 项 | 实测 |
|---|---|
| 工具调用总耗时 | **25.8s**（占墙钟 3.1%） |
| 整段会话墙钟 | 828s（13.8min） |
| 其余 96.9% | **模型思考 + 每次调用的 token 重读成本** |
| 其中手搓 Pillow/numpy 测量脚本 | **占全部工具调用的 47.5%** |
| `ViewImage` 调用 | 19 次（每次都是往返 + 永久 vision token） |

结论：**流程决定轮次，模型给每轮定价。单价改不了，乘数可以。**
所以 v1.6.0 不优化像素算法，只砍轮次：

- `survey`：一条命令出「结构线 + 内容框 + 可裁预算 + 方案表」，
  替代「看缩略图 → 猜坐标 → 写脚本量 → 再看」这一整圈（实测 0.7s）。
- `level`：Hough 竖边投票量滚转，替代手写追踪脚本（实测 0.7s）。
- `panels`：把 N 次「帮我放大看看这里」合成一张带坐标刻度的图。
- `candidates --crops-src --annotate`：候选框直接吃源坐标、回带
  `finish --crop` 字符串，省掉 `map-crop` 这一步；标注拼图让一次看图
  就能同时判断 3–6 个框的位置、比例和坐标。
- 所有内建命令**直接读 RAW**（内部 probe + 解码 + 缓存），
  不再需要先导预览；解码结果缓存在 `$TMPDIR/pme_work_<stem>/_analysis_<N>.jpg`，
  多个命令共享同一次解码，返回值里的 `cache_reused` 标明是否复用。

**预期**：轮次 48 → 10–14，墙钟 13.8min → 3.5–5min，约 3–4×。

---

## 无损与非破坏性（先讲清楚，别让用户误判）

**「无损」是三个不同命题，去掉调色并不会改善其中任何一个：**

| 层级 | 含义 | 现状 |
|---|---|---|
| 原文件无损 | 不对源字节写入 | **本来就已满足**：脚本从不写原图 |
| 像素无损压缩 | PNG / TIFF 不引入二次有损压缩 | 可选，但裁剪本身丢像素、不可逆 |
| 数据无损保留 | 保留 RAW 的线性高位深与宽容度 | 像素导出做不到；只能靠 XMP 非破坏性编辑 |

**DNG 真相**（Adobe 官方：`adobe.com/creativecloud/file-types/image/raw`、
`/dng-file`、`adobe.com/go/profile-sdk`）：DNG 是 TIFF 6.0 扩展的 RAW 归档容器，
存未显影传感器数据 + XMP 编辑元数据。Adobe 明确 RAW「not actually images
themselves」，编辑后须导出成另一种 raster 格式。Lightroom 里「修过的 DNG」=
**原 DNG 未改动 + 编辑参数叠加**，不是把修好像素写回去。

→ 所以「让成片依旧是 DNG 且无损」的正确做法，是
**原 DNG 不动 + 写 XMP sidecar**，不是导出一个 `.dng` 文件。

### 需要裁剪时的非破坏性交付：crop-plan + XMP

> v1.5.0：裁剪是默认动作，但**只有在用户要「非破坏性参数」或要留编辑记录时**
> 才走本节。默认交付是像素成片（`finish`），不写 XMP。

```bash
python3 "$P" crop-plan "$S" --ratio 4:5 --anchor center --out "<目录>/crop.json"
python3 "$P" xmp-crop "$S" --from-plan "<目录>/crop.json" --out "<DNG 同目录>/IMG_0896.xmp"
```

- `crop-plan` **不生成任何图片**，只写裁剪框（像素坐标 + 0–1 归一化坐标 + 比例）。
- `xmp-crop` 写标准 `crs:` Camera Raw 字段（`HasCrop` / `CropTop` /
  `CropLeft` / `CropBottom` / `CropRight` / `CropAngle` / `AlreadyApplied`），
  字段依据 Exiv2 Camera Raw Schema 与 ExifTool XMP Tags。
- **兼容性声明**：XMP sidecar 的具体表现**未在 Lightroom / ACR 实测**（本机未安装）。
  必须如实告知用户去自己的软件里验证；若软件不认，用 `crop.json` 里的
  像素坐标手动裁。darktable 使用自己的 history schema，不保证兼容。
- 需要实体成片时才 `raw-export` + `develop`，且**优先 `--out .png / .tiff`（无损压缩）**。

> 8-bit 提醒：整条链路经 `sips` 解码，输出上限就是 8-bit。
> PNG/TIFF 的「无损」指压缩无损，**不等于 16-bit**。要真 16-bit 宽容度，
> 先在 darktable / RawTherapee 显影，本 skill 只做后续边界决策。
>
> 体积代价：48.8MP 的 DNG 导出无损 PNG 实测约 **160MB**（同图 JPG 约 8.6MB）。
> 选 PNG 前要确认用户能接受，否则说明权衡后再定格式。

---

## 导出前确认（v1.3.0 起默认跳过，v1.5.0 进一步收紧）

**不再问三项，直接按默认出片**：比例锁死原图比例、格式 `.jpg` q92、
取景由我做（诊断与候选照给，但不等待拍板）。
只在两种情况下才问：用户当次说要换比例（问要哪个比例）、
或要非破坏性交付（问是否接受仅参数不写像素）。

历史三项留档备查（一般不再使用）：

1. **交付形态** —— 非破坏性 `crop.json` + `.xmp`（原 DNG 不动）/ 像素成片
2. **成片格式** —— 默认 `.jpg` q92；`.png` 无损但 48MP 约 160MB；`.tiff` 归档
3. **画幅比例** —— 默认**沿用原图比例**，只有换用途时才调整

> 注意「比例不变」不等于「不动刀」：比例内照样要重新取景，切冗余。
> 「选了不裁」的旧说法已废弃，见「默认决策」节。

> **v1.2.0 变更**：旧版第三问「需不需要调色」已随调色模块一并移除。
> 若用户主动要调色，直接说明本 skill 不做，并给出 darktable / Lightroom 的路径。

## 工作流（严格按顺序）

### 1. 看图 + Shore 五问（不可跳过，但只看一次）

RAW 先跑 `raw-preview --grid --max 1600`，一次拿到预览 + 网格图（省一次解码），
用 `ViewImage` 看完，再**按 `references/shore-first-principles.md` §3 依次回答五问**：

1. **视点**：相机在哪、视高、朝向？哪些关系完全由机位决定（后期改不了）？
2. **边界**：这张图是「框=终点」还是「框=起点」？四边排除了什么？
   边缘有无半截物体？边界本身是不是结构的一部分？
3. **平面关系**：前景 / 中景 / 背景在**二维平面**上的对位与间隔，哪些是承重骨架？
4. **注意力路径**：眼睛从哪进、经过哪几个点、能往深处走多远？
   有没有多个可读点？这个裁剪是延长还是截短了路径？
5. **透明性**：这个决策会不会显得「很用力」？有没有为干净而破坏语境？

答不出来就如实说不确定，请用户指出想看什么，**不要编一段大师点评**。
最后给一句结论：**裁掉什么、保留什么**（默认一定会切掉某处；
只有确实找不出冗余时才说「不裁」，并说明这是例外）。

### 2. 定用途与比例（v1.5.0：先锁原比例）

长期默认是**锁定原图比例**，不再等确认。原片 4:3 就出 4:3、3:2 就出 3:2。
只把「若真要适配某用途该用什么比例」作为信息给出，不作为提问。
仅当用户当次明确要换比例时，才按用途表执行。

| 用途 | 比例 |
|---|---|
| 小红书 / 竖版社媒 | 4:5、1:1 |
| 公众号头图 / 网页横幅 / PPT 满版 | 16:9 |
| 打印 / 原片保留 | 3:2、4:3 |
| 头像 | 1:1 |

### 3. 生成裁剪候选（关键步骤，v1.6.0 重写）

**顺序：`survey` 量 → 手写框 → `candidates --crops-src` 一次看全。**

```bash
# ① 量：结构线 + 内容框 + 可裁预算 + 方案表，一条命令，不写代码
python3 scripts/photo_lab.py survey "/path/IMG.DNG"

# ② 写：把选定的方案换成源像素框（survey 的 scenarios 里已经给好）
#    比例会被自动量化到精确值（3:4 → 4 和 3 的整数倍），不用自己凑数
python3 scripts/photo_lab.py candidates "/path/IMG.DNG" \
  --crops-src "0,176,1068,1424;0,448,1068,1424;67,300,1000,1332" --annotate

# ③ 看一眼 annotate 拼图：每格带 SRC 坐标、比例、以及「这个框在整幅里的位置」
```

`--crops-src` 的坐标是**源像素**，标签可省略（自动 A/B/C/D），每个候选都会
回带一行 `finish: --crop "x,y,w,h"`，**可以直接粘进第 4 步**——不需要 `map-crop`。
`--annotate` 让一格 440×380，一次看 3–6 个框仍然读得出坐标。

要单独放大看某个边界时用 `panels`，不要反复 `ViewImage` 同一张图：

```bash
# 五条自动带（TOP/BOTTOM/LEFT/RIGHT/MIDDLE）+ 源坐标刻度，合成一张
python3 scripts/photo_lab.py panels "/path/IMG.DNG"
# 指定要看的地方（源像素）
python3 scripts/photo_lab.py panels "/path/IMG.DNG" \
  --regions "EDGE:x0,y0,w,h;TOWER:x0,y0,w,h"
```

候选按 Shore 五问逐张评估，**挑 2–3 个**给用户，
每个都写清「为什么这样裁 + 牺牲了什么 + 对注意力路径的影响」。
切边落点由 `survey` 的 `structure_lines` 决定，**不要靠猜，也不要再手写
亮度剖面脚本**——那正是一次 survey 就能替代的东西。
（IMG_0862 的干净切口在 `top=300`：窗户上方的石头浮雕带；`top=250` 与
`top=330` 都会横切窗框。）

**三个已知陷阱**：

1. 脚本**不做主体识别**。几何锚点假设主体居中，偏一侧时会把人脸、塔尖切成一半。
2. **竖幅上水平锚点可能完全失效**：最大内接框在宽度受限时 `dx=0`，
   `center / third-left / third-right` 会算出同一个框。旧版静默输出 3 张相同图；
   v1.2.1 会在 `duplicate_crops` 里点名，并给该候选加 `anchor_effective: false`。
   解法：`--zoom 0.9` 缩小内接框腾出余量，或直接用 `--crops` 写框。
3. **收紧取景会切掉纵深出口**：主体占满画面后，唯一的街道/走廊纵深往往被挤成
   一条细缝，构图反而变平。宁可少切一点，也要让那个出口读得出来。

> 三分法 / 黄金分割只是描述工具，不是目标。对称与中心构图完全合法 ——
> 消失点居中时，对称本身就是注意力通道，别硬掰到三分线。
>
> **单边轻裁落地别用 `--ratio + --anchor center`**：center 会把余量上下均分，
> 与「只裁掉上方空天空」不是同一个框。用 `map-crop` 换算预览框，
> 再 `crop-plan --crop "x,y,w,h"` 精确指定。

### 4. 执行（v1.5.0：默认出片，且确实裁了）

**默认动作 = 出片**。比例锁定、原片不写，但取景必须真的变了。
一条命令出成品，成品自动落在原图旁边：

**出成品一律用 `finish`**，不要走 `raw-export` + `develop`
（那条路会写 100MB 级中间稿，实测 6.8s vs 1.3s）：

```bash
# 出成品：裁到算好的框（坐标是 DISPLAY 方向全尺寸像素），成品 → <stem>_final.jpg
python3 scripts/photo_lab.py finish "$S" --crop "1479,1109,5177,3883"

# 按锚点+比例+收紧出片（不想手算框时用）
python3 scripts/photo_lab.py finish "$S" --ratio 4:3 --anchor bottom --zoom 0.88

# 需要校正滚转时（正值=顺时针；先裁后旋，自动贴合最大内接矩形，比例不变）
python3 scripts/photo_lab.py finish "$S" --crop "..." --rotate -1.2

# 非破坏性参数（完全不写像素时才用）
python3 scripts/photo_lab.py crop-plan "$S" --ratio 16:9 --anchor center   # → $TMPDIR/pme_work_<stem>/
python3 scripts/photo_lab.py xmp-crop "$S" --from-plan "<crop.json>"      # → <RAW 同目录>/<stem>.xmp
```

`finish` 默认**只写一张成品图**，落在 `src.parent/<stem>_final.<ext>`，
不加 `--out` 也不用传路径、不建子目录；不加 `--sidecar` 就没有参数 json。
它内部用私有临时目录解码、`finally` 删除，失败也不留垃圾。
`develop` 的调色代码仅作 legacy 保留，**必须显式 `--grade` 才生效**，工作流不传它。

**`--rotate` 的执行顺序（v1.5.0 新增）**：`crop → rotate → 贴合内接矩形`。
先裁是为了让你写好的 `--crop` 坐标不用二次映射；旋转后自动取最大内接矩形，
把四角空白挖掉，**比例保持不变**。返回值里的 `rotation.kept_fraction`
说明旋转吃掉了多少像素（1.5° 约 0.966，2.5° 约 0.945），低于 0.9 要提醒用户。

### 5. 复核 + 交付（含清理）

**只做一次拼图复核**：用 `compare` 生成「原图 | 成品」并排图，`ViewImage` 看一次，
同时检查三件事——有没有真裁（不是逐像素一致）、切边是否落在结构线上、
暗部/亮部有没有压缩块。**不要**先看成品、再看原图、再看对比图：
同一张图看两遍等于白付一遍 vision token。

**必须做「和原图比」这一步**：如果两张几乎分不出来，
说明第 3 步的取景没做，回去重做（v1.5.0 的教训）。
非破坏性交付则核对 `crop.json` 数值与 XMP 内容。

然后按下一节**清掉全部过程产物**，交付目录只留最终交付物，且**与原图同目录**：
非破坏性 = `crop.json`（按需） + `<stem>.xmp`；
像素成片 = `<stem>_final.<ext>`（+ 显式 `--sidecar` 时的同名 `.json`）。

最后**一次性**把本次学到的东西回写到
`references/composition-rules.md`（实战对照表）与记忆文件，
不要在流程中途穿插写文件——那会把一次任务切碎成十几个往返。

## 命令速查

```bash
P=~/.workbuddy/skills/photo-master-eye/scripts/photo_lab.py
python3 "$P" info        "<图>"                     # RAW 走 TIFF 头快路径，失败回退 sips
python3 "$P" formats                                # 可用成片格式与位深
# ↓ 下列过程产物的 -o / --out-dir 都可省略，默认写 $TMPDIR/pme_work_<stem>/
python3 "$P" grid        "<图>"                     # 三分法+黄金分割+中心线
python3 "$P" survey      "<图>"                     # ★结构线+内容框+可裁预算+方案表
python3 "$P" level       "<图>"                     # ★竖边量滚转 → roll_deg/suggested_rotate
python3 "$P" panels      "<图>"                     # ★一张多面板图+源坐标刻度
python3 "$P" panels      "<图>" --regions "EDGE:0,0,600,900;TOWER:200,300,500,700"
python3 "$P" candidates "<图>" --ratios "4:3" --anchors "center,bottom" --zoom 0.88
python3 "$P" candidates "<图>" --crops-src "0,176,1068,1424;0,448,1068,1424" --annotate
python3 "$P" candidates "<图>" --ratios 4:3 --zoom 0.9 --rotate -1.2   # 带旋转的候选
python3 "$P" candidates "<图>" --crops "A:0,0,1500,2000" --keep-crops  # 需要逐张看才落盘
python3 "$P" crop-plan   "<图>" --ratio 4:5 --anchor center          # 默认写临时目录
python3 "$P" crop-plan   "<图>" --crop "0,252,3024,3780"             # 精确框
python3 "$P" xmp-crop    "<DNG>" --from-plan "<crop.json>"   # 默认写 <DNG 同目录>/<stem>.xmp
python3 "$P" map-crop    "<预览图>" --crop "x,y,w,h"  # 预览坐标→原图坐标
python3 "$P" raw-preview "<RAW>" --max 1600 --grid    # 预览+网格一次出，自动规范化方向
python3 "$P" finish      "<RAW>"                     # 原比例原样出片 → <stem>_final.jpg
python3 "$P" finish      "<RAW>" --crop "1479,1109,5177,3883"       # 精确框（最常用）
python3 "$P" finish      "<RAW>" --zoom 0.88 --anchor bottom        # 比例不变、收紧取景
python3 "$P" finish      "<RAW>" --crop "..." --rotate -1.2         # 先裁后旋，比例不变
python3 "$P" finish      "<RAW>" --format png --sidecar             # 需要无损/参数 json 时
python3 "$P" raw-export  "<RAW>" --format png        # 全尺寸解码稿，默认进临时目录
python3 "$P" raw-export  "<RAW>" --format png --keep-orientation   # 高级：跳过方向规范化
python3 "$P" develop     "<图>" --ratio 4:5 --anchor center         # legacy 路径
python3 "$P" compare    "<原图>" "<成图>"            # 对比图默认进临时目录
```

脚本全部本地运行，无网络请求、无 API key。`presets` 子命令为 legacy，不在工作流中。

## 产物清理（v1.4.0：默认自动执行，不询问）

**规则**：任务结束只保留**原图 + 最终成品**，其余全部自动清除。
不要问用户「要不要删」——这是既定政策。

过程产物包括：

```
*_preview.jpg / *_preview.json / *_grid.jpg
candidates/ 与 contact_sheet.jpg（内存模式同样要清）
*_full.*（全尺寸解码稿） / *_final.json（参数 sidecar） / compare.jpg / *_show.jpg
$TMPDIR/pme_work_<stem>/（本 skill 的过程产物工作目录）
$TMPDIR/pme_finish_*（finish 的私有解码临时目录，正常会自动删）
```

执行顺序（每批 ≤10 项，逐批核对）：

1. 列出清单与体积，说明可恢复方式。
2. 需要留痕的先 `cp` 到工作区 `.workbuddy/photo-backups/<任务名>_<日期>/`。
3. 用系统废纸篓 `/usr/bin/trash`，**不要用 `rm` / `rm -rf`**。
4. 核验三件事：原图哈希未变、照片目录里只剩**原图 + 成品（同目录、无子目录）**、
   `$TMPDIR/pme_work_*` 无残留。

**只清本次任务自己生成的文件。** 用户原有照片、其他任务成果、
无法确认归属的文件（例如来源不明的 `胶片/` 目录）一律不动，只在报告里说明。

- 用户明确说「留预览图 / 留对比图 / 留候选」时才保留对应文件。
- 全尺寸成品在对话预览里常打不开；需要展示时**临时**生成缩图，看完立刻清掉。

## 硬性约束（不可违反）

1. **永不覆盖原图**，尤其 DNG / RAW。输出到新文件；`--out` 等于输入直接报错。
2. **比例锁死、取景必须动、原片不动**（v1.5.0）：不换比例，但必须真的重新取景；
   **成品与原图逐像素一致 = 失败**。原 DNG / RAW 一个字节不写；
   只有真裁了才写 `crop.json` / XMP；过程产物只在 `$TMPDIR`。
3. **切边落在结构线上**：优先切重复且可推断的内容（一排窗、一段路面），
   绝不横切唯一元素（招牌、人物、转角弧线、塔尖）。宁可少切，也不切断。
4. **成品必须在原图同一目录**（v1.4.1）：`<原文件名>_final.<ext>`，不建
   `IMG_xxxx_out/`、`output/`、`export/` 之类子目录。唯一同目录例外是
   `xmp-crop` 产出的 `<stem>.xmp`，因为它本来就必须和 RAW 放一起。
5. **人物照片**：仅在用户确认有授权的前提下编辑；不得改变身份、年龄、体型。
6. **裁剪不可逆**：成图丢了原始像素，所以原图必须保留（本 skill 从不写原图）。
7. **不臆造内容**：不做扩图、换天、加元素；不做调色（模块已移除）。
8. **不要假装看懂了**：看不清就请用户指出想看什么，不编大师点评。
9. **交付目录只留最终交付物**：过程产物用系统废纸篓清理，不得 `rm`。
10. **XMP 兼容性不得夸大**：明确告知未在 Lightroom / ACR 实测，需用户自行验证。
11. **方向口径必须唯一**：RAW 有 90° EXIF 方向时，一切坐标以 DISPLAY 方向为准；
    预览必须已烤进方向且无残留标签。发现预览与网格宽高不一致，**先修再裁**，
    不要带着错位的坐标继续出候选或 XMP。
12. **旋转不冒充构图，也不靠眼睛估**：只用 `level` 的结论，且**先看
    `vertical_spread` 再看角度**——竖边必须横跨画面 ≥35% 且左右都有，
    单侧证据一律当 0°（单面立面分不清滚转与俯仰 keystone）。
    |角度| <0.3° 当 0°；建筑场景量竖边、自然场景量长直线，
    **近景横线（栏杆 / 路缘 / 江岸）是透视线，不能判滚转**。
    把旋转当风格化手段必须由用户明确要求，
    并说明会先被看到「照片歪了」。旋转不得改变比例——`rotate_fit()` 已按原图
    约分后的比例量化（8064×6048 → 4:3 → 输出必为 4 和 3 的整数倍），
    `kept_fraction` < 0.9 要提醒。
13. **长期默认规则不得自行改回**：不得再以「不裁」为理由交付零处理的图。
    诊断与候选照给，然后直接出片，不停下来等拍板。
14. **测量不手搓**（v1.6.0）：结构线、内容边界、可裁预算、滚转角一律用
    `survey` / `level`，**不要临时写 Pillow/numpy 脚本**。实测旧流程 47.5%
    的工具调用是这类一次性代码，是单项最大开销，也最容易出错（曾把内容顶
    判到 1182，真值 769）。
15. **一次任务只许看 N 张图，N = 拼图张数**：结构用一张 `panels`、
    候选用一张 `contact_sheet`、复核用一张 `compare`。
    **禁止对同一张图重复 ViewImage**，也禁止「先看成品再看原图」。
    每多看一次都是永久 vision token + 一个往返。
16. **规则回写集中到任务末尾**：`composition-rules.md`、记忆文件、
    版本号一律在收官阶段一次性写完，中途不穿插写文件。

## 能力边界

| 能稳定做好 | 做不了 / 不可靠 |
|---|---|
| 按 Shore 五问做构图诊断、给候选与取舍 | 「哪个人是主角」这类意图判断 |
| 比例内精确裁剪、收紧取景、网格辅助定位 | 情绪、氛围、「决定性瞬间」的语境判断 |
| 小角度旋转 + 自动贴合内接矩形（比例不变） | 透视/梯形矫正（keystone，广角仰拍变形靠旋转修不了） |
| 输出非破坏性 crop.json + 标准 XMP sidecar | 保证 Lightroom / darktable 一定认这份 XMP |
| PNG / TIFF 无损压缩导出（8-bit） | 16-bit 显影、RAW 宽容度保留 |
| 参数可复现、批量套用 | 局部蒙版、复杂选区、调色（已移除） |

**结论：这是「带 Shore 提问框架的取景决策工具」，不是自动出大师作品的机器。**
审美分歧无法消除；长期规则已经定好「比例锁死 + 取景必做 + 原片不动 +
成品放旁边」，所以逐次流程不再设等待环节，但诊断、候选与取舍理由仍完整给出。
