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

这个脚本会安装 Python 依赖，检查并在 macOS/Homebrew 环境下自动安装缺失的 `uv`、Julia、Yosys，创建 `.claude/skills/iccad22/SKILL.md`，尝试从 Google Drive 下载 ICCAD22 测例，并生成 `.mcp.json`。如果下载失败，可以手动把测例放到 `examples/testcase/`；如果只做本库代码开发或运行现有单元测试，通常先执行 `uv sync --dev` 即可。

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

当前 `.claude/skills/iccad22/SKILL.md` 定义的是 agent 驱动的迭代式恢复流程。它本身不自动推出公式，而是指导 Claude 调用 GateSpy MCP 工具观察网表、提出 RTL 改写、再交给 CEC 验证。

流程分三阶段：

1. **加载与理解电路**：`read_file(path)` 解析 gate-level Verilog，建立 circuit IR，并保存当前源码。随后用 `get_node` 查看端口/内部信号，用 `find_cone` 追踪输出的 fan-in/fan-out 边界，用 `print_adder_stats` 和 `print_xor_stats` 定位加法树、CSA/CPA 结构和 XOR 链，再用 `simulate` 对少量输入模式做行为观察。

2. **提出并验证 word-level 改写**：Claude 根据结构线索和仿真样本猜测表达式，例如把 adder tree 改成 `+`、乘法结构改成 `*`、MUX 改成 `?:`。改写通过 `edit` 工具提交；后端会自动运行 CEC。若 CEC 失败，改写被拒绝并返回原因或反例；若成功，记录修改并报告 cost before/after 与 reduction rate。

3. **收敛与导出**：用 `show` 复查当前 RTL，用 `revert` 回退错误路径，用 `dump(output_path)` 写出最终 `*_recovered.v`。目标是在保持 CEC 等价的前提下，用更少 word-level operator 替代大量 primitive gates。

这个流程的优点是灵活，能结合 LLM 的归纳能力处理未知结构；缺点是候选表达式主要靠 Claude 从工具输出中人工推断。遇到 `test04` 这类大乘加结构时，GateSpy 能显示 adder/CSA 规模并提供仿真，但不会主动给出 `out3 = in1 * (in2 + in3 + in4) + in5` 这样的候选，所以容易在猜公式和 CEC 等待中消耗时间。

## LLM-Guided Hypothesis Workbench

GateSpy 当前新增了一套 LLM 可驱动的网表逆向实验台。工具负责结构分析、采样、拟合、验证、反例追踪和 cost 评估；LLM 负责提出假设、发明新模板、根据 CEX 追矛盾点并调整策略。

### 已实现 MCP 工具

- `infer_candidates(output="", methods=None, sample_num=256, detail=False)`：针对 PO word 生成初始候选，返回结构摘要、support words、样本拟合结果和 hypothesis id。当前内置线性与乘加模板，候选只作为 LLM 起点。
- `check_hypothesis(assignments, declarations="", sample_num=256, run_cec=True)`：接受 LLM 自己提出的 word-level hypothesis，例如 `{"out3": "in1 * (in2 + in3 + in4) + in5"}`。工具跑样本检查；若覆盖所有输出，还会生成临时 RTL、显式零扩展 word 操作数、计算 cost，并按需跑 CEC。
- `fit_hypothesis(output, template, unknowns, sample_num=256)`：支持 LLM 自定义模板并求小整数系数。`unknowns` 可以是列表，也可以用 dict 指定搜索范围。
- `trace_counterexample(hypothesis_id="", output="", bits=None, depth=3)`：回放最近失败 hypothesis 的 CEX 或样本 mismatch，返回 mismatch output、word-level 输入值、候选表达式中间项取值和相关 cone 摘要。

### 已实现模块

`src/circuit/infer/` 按职责拆分为 `words`、`sampling`、`hypothesis`、`fitting` 和 `trace`。`CircuitSession` 保存最近候选、样本 mismatch、CEX 和 CEC 结果，但 `edit` 仍是唯一修改当前源码的工具。

CEC 状态已分级：`proved` 表示严格证明等价，`counterexample` 表示有反例可追踪，`timeout_assumed` 只能作为候选证据，`error` 表示转换或验证失败。`edit` 已增加 `accept_timeout=False`，默认只接受 `proved`；LLM 若要接受 timeout 候选，必须显式设置。

### 计划工作流

LLM 先用 `infer_candidates` 获取结构引导的初始候选。若候选不合适，直接用 `check_hypothesis` 验证自创公式；若公式有参数，用 `fit_hypothesis` 拟合；若 CEC 返回反例，用 `trace_counterexample` 定位冲突位、相关 cone 和中间项，再调整模板。最终由 LLM 组合多 PO 表达式和公共子表达式，调用 `edit(rewrite=...)` 应用 RTL，再用 `dump` 导出。

### 依赖与安装

不做运行时懒加载。`pyproject.toml` 已加入 `numpy`、`sympy`、`pandas`、`pysr`，`install.sh` 会执行 `uv sync --dev`，检查 Julia/PySR、`yosys` 和 `yosys-abc`。在 macOS 且存在 Homebrew 时，脚本会自动安装缺失的 Julia/Yosys；无法自动安装时才明确报错并提示手动命令。不要新增批量脚本。

### 测试与后续增强

新增测试覆盖候选推断、正确/错误 hypothesis 的样本状态、自定义模板拟合，以及样本 mismatch 到 trace 报告的转换。当前已验证 test01 的 CEC 路径和 test04 的样本路径：`out1 = in1 + in2`、`out2 = in3 - out1`、`out3 = in1 * (in2 + in3 + in4) + in5`。后续增强重点是接入 PySR 真实 symbolic regression、更多控制逻辑模板、跨 PO 公共子表达式优化，以及把 CEX 增量加入拟合集合。
