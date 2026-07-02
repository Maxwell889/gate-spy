# Repository Guidelines

## 这个库是干什么的

GateSpy 是一个面向门级网表分析的 Python 工具库和 MCP 服务。它的目标是让 LLM agent 能在低层 Verilog/AIGER 电路上做可解释的结构推理，而不是只把网表当成普通文本处理。

它主要针对的问题是：综合后的 gate-level netlist 往往丢失了原始 RTL 的字级语义，例如乘法器、加法树、XOR 链、局部 cone、输入输出 bus 边界等。GateSpy 通过解析电路图、追踪 fan-in/fan-out、仿真、结构识别和子图导出，帮助从低级门结构中恢复这些语义线索。

## 核心能力

- 读取 gate-level Verilog：支持 primitive gate 风格网表。
- 读取 AIGER：支持 `.aig` / `.aag`，示例文件见 `examples/Mul_INT16.aig`。
- 查询节点和信号：可查看 gate 类型、fan-in、fan-out、bus 汇总和通配搜索结果。
- cone 分析：从一组信号向前或向后追踪依赖边界。
- 电路仿真：支持随机输入、固定输入、watch 内部信号和可复现实验 seed。
- 子图提取：把指定输出的 backward cone 导出为 Verilog 或 AIG。
- 结构识别：当前包含 XOR 链和 half/full adder tree 统计。
- 等价检查脚本：`scripts/yosys_cec.py`、`scripts/abc_cec.py` 可辅助验证改写后的 Verilog。

## 怎么用

先安装依赖：

```bash
uv sync --dev
```

如果要配置 ICCAD22 Problem A 的完整实验环境，使用仓库自带脚本：

```bash
bash install.sh
```

这个脚本会安装 Python 依赖，检查并在 macOS/Homebrew 环境下自动安装缺失的 `uv`、Julia、Yosys，创建 `.claude/skills/iccad22/SKILL.md` 和 `.claude/skills/iccad22/references`，尝试从 Google Drive 下载 ICCAD22 测例，并生成 `.mcp.json`。如果下载失败，可以手动把测例放到 `examples/testcase/`；如果只做本库代码开发或运行现有单元测试，通常先执行 `uv sync --dev` 即可。

运行测试：

```bash
uv run pytest
```

启动 MCP 服务：

```bash
uv run python mcp_server.py
```

典型 MCP 调用流程是先加载电路，再逐步查询：

```text
read_file("examples/Mul_INT16.aig")
get_node("Out[0]")
find_cone(["Out[0]"], direction="backward", depth=3)
simulate(pattern_num=8, fixed_inputs={"IN1": 3, "IN2": 5})
print_adder_stats(detail=True)
extract_subgraph(outputs=["Out[0]"], out_path="subgraph.v")
```

如果需要把 Verilog 转成 AIGER：

```bash
scripts/v2aig.sh examples/Mul_F16.v /tmp/mul.aig
```

这些脚本依赖本机安装 `yosys`、`yosys-abc` 或 ABC。

## 项目结构

- `mcp_server.py`：FastMCP 入口，定义暴露给 agent 的工具。
- `src/session.py`：会话层，维护当前加载的 circuit，并封装 MCP 工具背后的核心操作。
- `src/circuit/`：电路数据结构、解析器、AIGER 支持、仿真、cone 分析和 Verilog 导出。
- `src/circuit/extract/`：结构识别逻辑，例如 XOR 和 adder。
- `scripts/`：Yosys/ABC 相关转换、综合和 CEC 脚本。
- `install.sh`：ICCAD22 Problem A 环境初始化脚本，会写入本地 MCP/skill 配置并下载测例。
- `examples/`：示例网表和 AIG 文件。
- `test/`：pytest 测试。

## 开发约定

使用 Python 3.11+。代码风格跟随现有文件：4 空格缩进，函数和变量用 `snake_case`，类用 `PascalCase`。新增面向 MCP 的输出应保持可读、稳定，并在错误信息里给出可恢复线索，例如可用输入 bus、支持的文件扩展名或未解析信号名。

涉及解析、仿真、cone 查询、子图提取或 MCP 输出格式的改动，都应补充或更新 `test/test_*.py`。随机仿真测试必须固定 `seed`。提交信息沿用当前历史中的短英文祈使句风格，例如 `add cec scripts`、`support primitive gate parser`。

## 当前 ICCAD22 Skill 的逆向流程

当前 `.claude/skills/iccad22/SKILL.md` 是指向 `skills/iccad22.md` 的 symlink，`.claude/skills/iccad22/references` 是指向 `skills/references` 的 symlink。更新 MCP 工具、逆向流程或 contest cost 策略时，必须同步维护 `skills/iccad22.md` 和 `skills/references/`，并确保 `install.sh` 会创建这两个链接，否则 Claude 打开仓库后可能只能看到主 skill，看不到 reference 文件。

该 Skill 定义的是 agent 驱动的迭代式恢复流程：GateSpy 负责结构观察、采样、拟合、验证、反例追踪和 cost 评估；LLM 负责提出假设、发明新模板、合并公共表达式，并在工具不够用时写受控的临时分析脚本。

`skills/iccad22.md` 按 Claude skill best practices 改成“短入口 + 直接引用文件”的结构。主文件必须有 YAML frontmatter（`name`、`description`），保持恢复流程、硬性规则和 checklist；长表格和细节放到 `skills/references/`，避免主 skill 过长导致模型抓不住重点。

当前必须同步维护四类内容：

1. **Core Invariants**：没有 Verilog 路径不开始；随机仿真不是证明；只有 CEC `proved` 是最终证明；算术/比较/移位/切片必须审计 width、truncation、constant sizing 和 signedness；共享 support 或结构的输出优先按 group 恢复；proved RTL 还要经过 cost audit 后才能 dump。
2. **Tool Semantics**：在 `skills/references/iccad22-tools.md` 明确每个 MCP 工具的用途、典型调用、返回结果和状态变更。`read_file` 加载设计；`run_method`、`infer_candidates`、`fit_basis`、`fit_hypothesis` 只产生候选；`check_hypothesis` 只验证临时 hypothesis；`edit` 是唯一修改当前 recovered source 的工具；`dump` 只写出已接受源码；`scripts/cost.py` 只算 contest cost，不证明等价。
3. **Evidence Ledger**：每个 output 或 output group 都要记录 bit range、support words、结构证据、疑似算子族、signedness、当前最佳假设、sample/CEC 结果、已知 counterexample、当前 cost、尝试过的更低 cost 改写和最终状态。失败 hypothesis 必须记录最小矛盾：mismatch bit、输入取值和疑似原因。
4. **Scoring Reference**：在 `skills/references/iccad22-scoring.md` 维护完整 contest operator/keyword cost；疑难恢复经验放在 `skills/references/iccad22-patterns.md`。

执行纪律：fresh repository session 默认先跑 `bash install.sh`，除非用户明确说已完成环境准备；用户没给 Verilog 路径时，只能在仓库内搜索唯一 `top_primitive.v`，多于一个或没有就让用户选择；`read_file` 后先记录 top module、port widths、output naming pattern、gate count 和输出是否 packed word，不能默认存在 `out1`；partial `check_hypothesis` 只能做样本调试，最终证明必须覆盖所有输出；final `edit` 优先完整 `rewrite`，`matches/replacements` 只用于唯一匹配的小清理；cost audit 是基于当前 proved/accepted RTL 的局部优化闭环，便宜候选失败就保留原 baseline，通过才更新 baseline；每个最终 output group 必须记录一次 cost-audit decision，即尝试过的低 cost 替代或无更便宜形式的理由。

当前流程分为：Repository Setup；Load/Port/Structure Inspection；Output Grouping and Evidence Ledger；Strategy Selection；Hypothesis Generation；Boundary/Width/Signedness Audit；Counterexample-Guided Refinement；Apply Edit and Prove；Cost Reduction Audit；Dump or Report Unsolved。

关键工具顺序是：先 `read_file`、`get_node`、`find_cone`、`print_adder_stats`、`print_xor_stats`、`simulate` 建立结构证据；再用 `propose_strategy` 排序 `template`、`polynomial`、`symbolic` 三条 lane，但把它当 hint 而不是裁决；候选生成用 `run_method`、`infer_candidates`、`fit_basis`、`fit_hypothesis`；完整公式用 `check_hypothesis`；失败后用 `explain_failure` 或 `trace_counterexample` 追 mismatch；最终 only after proof and cost audit 才用 `edit`、`show`、`dump`。

多输出模块不能只改一个输出，除非工具明确保留其余输出。共享中间 wire 的输出必须一起验证；单独正确的公式可能全局 cost 更差。LLM 可以写 `/tmp` 临时脚本分析原始 netlist、当前 recovered RTL、样本或 CEX trace，但脚本不能静默重建或修改被测设计，脚本结论也不能替代 CEC。

Skill 的 Scoring Reference 必须列出完整 contest operator/keyword cost。最终审计以 `scripts/cost.py` 为准，不以常规 RTL 可读性为准；允许使用 `**`、`%`、shift、reduction、compare、`case` 等所有 contest 允许且 CEC 可验证的写法。新增 skill 内容时优先放入主流程；只有工具表、评分表或可选疑难模式才放进 reference 文件。

LLM 可以在还原过程中写一次性脚本补足 MCP 工具短板，例如批量统计 support、挖掘重复表达式、枚举 basis、整理 `assignments_json` 或比较样本 trace。脚本应放在 `/tmp` 或明确的临时路径，固定 seed 和输入路径，输出保持简短，不直接覆盖源码或 recovered RTL；如果脚本具有复用价值，再讨论是否加入 `scripts/` 并补测试。脚本结论不能替代证明，最终仍必须通过 `check_hypothesis` 或 `edit` 的 CEC。

## LLM-Guided Hypothesis Workbench

GateSpy 当前新增了一套 LLM 可驱动的网表逆向实验台。工具负责结构分析、采样、拟合、验证、反例追踪和 cost 评估；LLM 负责提出假设、发明新模板、根据 CEX 追矛盾点并调整策略。

### 已实现 MCP 工具

- `propose_strategy(output="", detail=False, budget=None)`：对一个或所有 PO word 评估三条逆向通道。`template` 是现有结构/模板/拟合路径，`polynomial` 是有预算的 PO-to-PI 表达式展开，`symbolic` 是样本驱动的受限 symbolic regression。报告会给出 cone gate 数、support、重汇聚、polynomial 表达式长度估计和推荐调用。
- `run_method(output, method, sample_num=256, budget=None, detail=False)`：显式运行某条通道但不修改源码。`method="template"` 等价于按目标输出运行 `infer_candidates`；`method="polynomial"` 先检查预算再展开 bit-level 结构表达式并样本验证，估算超限时默认 `skipped_budget`，只有显式 `budget={"force": true}` 才强跑；`method="symbolic"` 不直接把问题交回给 LLM 猜，而是依次运行 affine prefit、由 support words 系统生成的 compact word sketches（shifted affine、product-add-shift、小 product-of-sums 等）、word-level grammar search，最后才进入 bit-select grammar。若这些 WolFEx-style 轻量搜索都失败且 support bits 太宽，会返回 `budget_exceeded` 而不是进入大枚举。
- `explain_failure(hypothesis_id="", output="", depth=3)`：包装最近失败 hypothesis 的状态、诊断建议和 `trace_counterexample` 输出，帮助 LLM 判断是补 basis、改 selector、处理位宽，还是切换方法。
- `infer_candidates(output="", methods=None, sample_num=256, detail=False)`：针对 PO word 生成初始候选，返回结构摘要、support words、样本拟合结果和 hypothesis id。当前内置模板包括线性加减、精确仿射大系数、常数/scale/shift、bit select/slice、稀疏双线性、多项式乘积和、乘加、比较器以及 MUX/条件表达式；候选只作为 LLM 起点。当 `output=""` 且输出数量较多时，工具会走 fast batch 模式，复用同一批样本并输出 `assignments_json`，可直接传给 `check_hypothesis` 做全输出验证。
- `check_hypothesis(assignments, declarations="", sample_num=256, run_cec=True)`：接受 LLM 自己提出的 word-level hypothesis，例如 `{"out3": "in1 * (in2 + in3 + in4) + in5"}`。工具跑样本检查；若覆盖所有输出，还会生成临时 RTL、显式零扩展 word 操作数，并按需跑 CEC。cost 按 compact RTL 计算；若 CEC 用的显式扩展 RTL cost 不同，会在 note 中单独提示。样本检查器支持 `$signed(...)`、`$unsigned(...)` 和简单 `wire signed [...] name = expr;` 本地声明，但复杂 Verilog signed propagation 仍要通过显式 width/signedness audit 和 CEC 验证。
- `check_hypothesis(..., share_common=True)`：默认启用保守公共子表达式共享。工具会从重复的加法前缀中生成 `gs_cse*` wires，例如共享 `x + y`、`x + y + k*z`、`... + t` 这类跨输出公共 base，优化后重新跑样本检查，再计算 cost/CEC。共享优化成功后使用 compact width 渲染，避免无谓 `{N'b0, x}` 增加 contest cost。
- `fit_hypothesis(output, template, unknowns, sample_num=256)`：支持 LLM 自定义模板并求整数系数。小规模未知量使用网格搜索；当未知量组合数爆炸时，若模板对未知量是线性的，会自动切换到大规模 affine solver，可处理几十个系数，例如 `c0*x0 + c1*x1 + ...`。solver 会先用低幅值样本子集提出候选，再用全样本按输出位宽验证，以处理加法树输出回绕。
- `fit_basis(output, basis, include_constant=True, coefficient_limit=4096, sample_num=256)`：开放式 basis 拟合接口。LLM 提供任意可求值 basis，例如 `["in1", "in2", "in1 * in2", "sel ? in5 : 0", "in8 << 3"]`；工具拟合 `const + Σ ci*basis_i`，并用样本严格验证。遇到新结构时优先扩展 basis，而不是修改内置模板；对大加法树可用分组 selector basis 避免人工枚举所有权重。
- `trace_counterexample(hypothesis_id="", output="", bits=None, depth=3)`：回放最近失败 hypothesis 的 CEX 或样本 mismatch，返回 mismatch output、word-level 输入值、候选表达式中间项取值和相关 cone 摘要。

### 已实现模块

`src/circuit/infer/` 按职责拆分为 `words`、`sampling`、`hypothesis`、`fitting`、`polynomial`、`symbolic`、`strategy` 和 `trace`。`CircuitSession` 保存最近候选、样本 mismatch、CEX 和 CEC 结果，但 `edit` 仍是唯一修改当前源码的工具。

CEC 状态已分级：`proved` 表示严格证明等价，`counterexample` 表示有反例可追踪，`timeout_assumed` 只能作为候选证据，`error` 表示转换或验证失败。`check_hypothesis` 和 `edit` 的底层 CEC 默认先跑 ABC AIG flow；若 ABC 报 counterexample，会自动用 Yosys `equiv` 交叉验证，避免 word-level RTL 在 AIG 转换或 PO 映射上产生假反例。`edit` 已增加 `accept_timeout=False`，默认只接受 `proved`；LLM 若要接受 timeout 候选，必须显式设置。

### 计划工作流

LLM 先用 `propose_strategy` 获取三通道建议，再用 `run_method` 选择性尝试 `template`、`polynomial` 或 `symbolic`。若候选不合适，先根据 cone、仿真和反例构造 basis，用 `fit_basis` 拟合开放式表达式；若已知具体模板再用 `fit_hypothesis` 求参数；若已有完整公式则用 `check_hypothesis` 验证并让 `share_common` 做公共子表达式共享。CEC 返回反例时，用 `explain_failure` 或 `trace_counterexample` 定位冲突位、相关 cone 和中间项，再调整 basis、切 cone 或切换方法。最终由 LLM 组合多 PO 表达式，完成三轮不可化简确认后，调用 `edit(rewrite=...)` 应用 RTL，再用 `dump` 导出。

### 依赖与安装

`pyproject.toml` 已加入 `numpy`、`sympy`、`pandas`、`pysr`，`install.sh` 会执行 `uv sync --dev`，检查 Julia/PySR、`yosys` 和 `yosys-abc`。在 macOS 且存在 Homebrew 时，脚本会自动安装缺失的 Julia/Yosys；无法自动安装时才明确报错并提示手动命令。不要新增批量脚本。

### 测试与后续增强

新增测试覆盖候选推断、正确/错误 hypothesis 的样本状态、自定义模板拟合、开放式 basis 拟合、样本 mismatch 到 trace 报告的转换、三通道 strategy、bounded polynomial rewrite、symbolic regression，以及双线性、乘积和、比较器、MUX、shift、bit select、大系数仿射分支等内置模板。当前已验证基础加法、乘加、多输出 MUX 阵列和共享加法前缀等路径；后续增强重点是把 CEX 增量加入 basis 拟合集合、跨 PO 公共子表达式优化，以及接入 PySR 真实 symbolic regression 作为更重的 basis 生成器。
