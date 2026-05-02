# UPK Reader & Matcher

UPK Reader & Matcher is a small reverse-engineering and verification toolkit
for reading a UPK-format flight recorder data file and comparing candidate raw
word locations against a supplied CSV sample table.

The project is designed for two related goals:

- read and inspect a `12minute.upk` file whose low-level data structure has
  already been identified;
- search for the likely UPK word or bit specified of each CSV parameter by
  comparing raw UPK samples with the reference CSV values.

The web matcher is also useful as a manual verification interface: it plots the
CSV sample series together with raw UPK candidate values so that correlations,
phase alignment, and discrete bit behavior can be visually inspected.

## Data Structure

The working UPK model uses `512 words/sec`.

Current confirmed structure:

- `12minute.upk` uses 16-bit little-endian containers holding 12-bit words.
- `0x0FFF` is fill/no-data.
- One recorded second/subframe has `512` word offsets, `0-511`.
- The file has `4176` 512-word seconds total.
- Non-fill data occupy seconds `0-734`, about `12.25` minutes.
- The validated timestamp-derived word map is generated with
  `round(frac * 512)`.

## Matching Method

The main difficulty is locating the concrete UPK word or bit position for each
parameter. This project uses search instead of a proprietary data-frame layout.

For continuous parameters:

- each candidate UPK word offset `Wn` is compared only against CSV rows whose
  timestamp fractional part maps to the same word position;
- both unsigned 12-bit and signed 12-bit interpretations are tested;
- a linear fit is computed between raw UPK values and CSV engineering values;
- candidates are ranked primarily by absolute Pearson correlation `abs(r)`;
- the frontend hides negative-`r` candidates by default, but they can be shown.

For discrete parameters:

- CSV labels are parsed from `%N(...)` discrete definitions and mapped to `0/1`;
- each candidate `word.bit` is tested in both normal and inverted form;
- candidates are scored by accuracy and, when both states appear in the sample
  window, balanced accuracy;
- constant-state samples are still shown, but marked as weaker evidence.

The web frontend uses a fixed alignment for this dataset:

- `shift = 43`
- `upk_second = floor(csv_time - 288200) - shift`
- scoring and plotting use UPK raw second `>= 600`

These fixed backend values are customized for decoding the publicly available
FDR information from a specified incident. They are not general defaults for every UPK/FDR
dataset.

## Repository Layout

Included project files:

- `parameters.csv`  
  Validated parameter list used to build the mapped parameter table.
- `analysis_tools/upk_reader.py`  
  Low-level UPK reader and helper utilities.
- `analysis_tools/build_validated_word_map.py`  
  Builds the timestamp-derived parameter-to-word map.
- `analysis_tools/build_core_49_params.py`  
  Builds the smaller key-parameter list used by default in the web UI.
- `analysis_tools/robust_candidate_search.py`  
  Batch candidate search tool for continuous and discrete parameters.
- `analysis_tools/explore_upk.py`  
  Exploratory UPK inspection utilities.
- `web_frontend/manual_match_server.py`  
  Local HTTP backend for the manual matcher.
- `web_frontend/manual_matcher.html`  
  Browser UI for manual candidate inspection.

Generated files:

- `generated/validated_param_word_map.csv`
- `generated/validated_param_word_map.md`
- `generated/core_49_params.csv`

These files are generated locally and are not committed.

## Data Files

This repository does not include the raw data files needed to run the matcher.
After cloning, obtain the required files elsewhere and place them in the
repository root:

- `12minute.upk`
- `ExactSample.csv`

`TableResolution.csv`, `report.pdf`, and translated recorder-report folders are
not required to run the web matcher.

## Setup

Use Python 3.10+.

Install dependencies:

```text
pip install numpy
```

Clone the repository and enter the project directory:

```text
git clone https://github.com/igttttma/UPK_reader-matcher
cd "UPK_reader&matcher"
```

Place the required data files in the project root:

```text
12minute.upk
ExactSample.csv
```

Then generate the derived files:

```text
python analysis_tools/build_validated_word_map.py
python analysis_tools/build_core_49_params.py
```

Start the local matcher server:

```text
python web_frontend/manual_match_server.py
```

Open a browser:

```text
http://127.0.0.1:8765/
```

## Web Matcher Behavior

- The dropdown initially contains the key parameters from
  `generated/core_49_params.csv`.
- Turning off `Key variables` expands the dropdown to all mapped parameters
  from `generated/validated_param_word_map.csv`.
- The language control switches between English and Chinese.
- Continuous candidates are displayed as raw UPK dots on the right axis against
  the fixed ExactSample line on the left axis.
- Discrete candidates are displayed as `word.bit` candidates with normal or
  inverted bit interpretation.
- Candidate scoring is phase-aware: candidate `Wn` is compared only with CSV
  rows whose timestamp fraction maps to `Wn`.

---

# UPK Reader & Matcher 中文说明

UPK Reader & Matcher 是一个用于读取 UPK 格式飞行记录数据、并与给定 CSV
样本表进行交叉比对的小型逆向与真实性验证工具。

本项目有两个相关目标：

- 读取并检查一个底层数据结构已经探明的 `12minute.upk` 文件；
- 通过搜索方法定位每个 CSV 参数最可能对应的 UPK word 或 bit 位置。

Web 匹配器也可以作为手动验证界面使用：它会把 CSV 样本序列和 UPK 原始候选值
画在同一张图上，方便检查相关性、采样相位对齐和离散 bit 状态。

## 数据结构

当前采用的 UPK 模型是 `512 words/sec`。

当前确认的结构：

- `12minute.upk` 使用 16-bit little-endian 容器保存 12-bit word。
- `0x0FFF` 是填充/无数据标记。
- 每个记录秒/子帧包含 `512` 个 word offset，即 `0-511`。
- 文件总共有 `4176` 个 512-word 秒。
- 非填充数据位于秒 `0-734`，约 `12.25` 分钟。
- validated timestamp-derived word map 使用 `round(frac * 512)` 生成。

## 匹配方法

主要难点在于定位每个参数在 UPK 中的具体 word 或 bit 位置。本项目不依赖专有
DFL，而是采用搜索和比对的方法。

对于连续型参数：

- 每个候选 UPK word offset `Wn` 只与 timestamp fraction 映射到同一个 word
  位置的 CSV 行比较；
- 同时测试 unsigned 12-bit 和 signed 12-bit 两种解释；
- 对 UPK raw 值和 CSV 工程值做线性拟合；
- 候选主要按 Pearson 相关系数的绝对值 `abs(r)` 排序；
- 前端默认隐藏负 `r` 候选，也可以手动显示。

对于离散型参数：

- 从 `%N(...)` 离散定义中解析 CSV 标签，并映射为 `0/1`；
- 对每个候选 `word.bit` 同时测试正向和反向解释；
- 使用 accuracy 评分；当样本窗口中 0/1 两种状态都出现时，优先使用
  balanced accuracy；
- 只有单一状态的样本也会显示，但会标记为较弱证据。

Web 前端后端对这份数据使用固定对齐：

- `shift = 43`
- `upk_second = floor(csv_time - 288200) - shift`
- 评分和绘图使用 UPK raw second `>= 600`

这些固定后端参数是为了某次事件公开 FDR 信息解码而定制的，不是所有
UPK/FDR 数据集的通用默认值。

## 仓库结构

项目包含：

- `parameters.csv`  
  validated 参数列表，用于生成参数映射表。
- `analysis_tools/upk_reader.py`  
  底层 UPK reader 和辅助工具。
- `analysis_tools/build_validated_word_map.py`  
  生成由时间戳推导出的参数到 word 的映射表。
- `analysis_tools/build_core_49_params.py`  
  生成 Web UI 默认使用的关键参数列表。
- `analysis_tools/robust_candidate_search.py`  
  连续型和离散型参数的批量候选搜索工具。
- `analysis_tools/explore_upk.py`  
  UPK 探索和检查工具。
- `web_frontend/manual_match_server.py`  
  手动匹配器的本地 HTTP 后端。
- `web_frontend/manual_matcher.html`  
  浏览器里的手动候选检查界面。

生成文件：

- `generated/validated_param_word_map.csv`
- `generated/validated_param_word_map.md`
- `generated/core_49_params.csv`

这些文件需要在本地生成，不提交到仓库。

## 数据文件

本仓库不包含运行匹配器所需的原始数据文件。clone 后，请在别处获得以下文件，
并放到仓库根目录：

- `12minute.upk`
- `ExactSample.csv`

`TableResolution.csv`、`report.pdf` 和翻译版记录器报告目录不是运行 Web
匹配器所必需的。

## 部署步骤

使用 Python 3.10+。

安装依赖：

```text
pip install numpy
```

clone 仓库并进入项目目录：

```text
git clone https://github.com/igttttma/UPK_reader-matcher
cd "UPK_reader&matcher"
```

把所需数据文件放到项目根目录：

```text
12minute.upk
ExactSample.csv
```

然后生成派生文件：

```text
python analysis_tools/build_validated_word_map.py
python analysis_tools/build_core_49_params.py
```

启动本地匹配器服务：

```text
python web_frontend/manual_match_server.py
```

最后打开浏览器：

```text
http://127.0.0.1:8765/
```

## Web 匹配器行为

- 参数下拉框默认来自 `generated/core_49_params.csv` 中的关键参数。
- 关闭 `Key variables / 筛选关键变量` 后，下拉框会扩展为
  `generated/validated_param_word_map.csv` 中全部 mapped 参数。
- 语言控件可以在英文和中文之间切换。
- 连续型候选以 UPK raw dots 显示在右轴，ExactSample 固定显示在左轴。
- 离散型候选显示为 `word.bit`，并区分正向或反向 bit 解释。
- 候选评分按采样相位对齐：候选 `Wn` 只与 timestamp fraction 映射到
  `Wn` 的 CSV 行比较。
