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

这个脚本会创建 `.claude/skills/iccad22/SKILL.md`，下载 ICCAD22 测例到 `examples/ICCAD22_Problem_A/`，并生成 `.mcp.json`。它需要网络访问 Google Drive；如果只做本库代码开发或运行现有单元测试，通常先执行 `uv sync --dev` 即可。

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

## 开发 Plan

暂空。
