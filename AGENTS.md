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
- 功能表达式发现：底层仍有 `infer_expression` 和内部候选生成能力，但 agent-facing RTL 恢复默认改为小工具驱动，不鼓励一次性自动搜索。
- 通用 RTL 恢复：`plan_recovery` / `recovery_tool_guide` / `probe_target` / `validate_expr` / `fit_template` / `infer_native_expr` / `check_polynomial_lowbits` / `split_control_cases` / `list_candidates` / `combine_case_expr` / `infer_pysr_expr(force=True)` / `assemble_rtl` / `verify_rtl_candidate` 组成 plan -> probe -> explicit validation or light attempt -> failure summary -> revised plan -> assembly/CEC 流程。
- Reasoning Graph + 本地经验 RAG：`read_file(record_ir=True)` 或 `start_recovery_run(...)` 会启动 graph-only 记录，`.gate_spy/runs/<run_id>/` 只保存 `graph.json`、`graph.jsonl`、`summary.json` 和可选 `final_candidate.v`；普通 tool log、完整 report text、text preview、raw events 不落盘。`record_recovery_graph_node` 记录 `problem/experiment/observation/relation/validation/experience`；`query_recovery_experience` 从 `data/recovery_kb/` 检索已 promotion 的解决路径经验，`promote_recovery_experience` 只把已验证 `relation -> validation` 链或显式负面问题写入知识库。
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

这个脚本会执行 `uv sync --dev` 安装/检查项目依赖（包括 PySR/SymPy）、创建 `.claude/skills/iccad22/SKILL.md`，并生成 `.mcp.json`。它不再下载 ICCAD22 测例；如果只做本库代码开发或运行现有单元测试，通常先执行 `uv sync --dev` 即可。

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

通用 RTL 恢复流程优先使用：

```text
read_file("examples/iccad22_test01.v")
plan_recovery(detail=True)
probe_target("out1", inputs=["in1", "in2", "in3"], detail=True)
validate_expr("out1", "in1 + in2 + in3", inputs=["in1", "in2", "in3"])
assemble_rtl(timeout_s=300)
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
- `src/circuit/symbolic/`：typed bit-vector expression AST、采样、候选生成、PySR 适配和验证。
- `src/circuit/recovery/`：通用 RTL 恢复流程，包括 word/support 分析、bounded polynomial rewriting、template fitting、control enumeration、candidate RTL 和报告。
- `data/recovery_kb/`：可提交的 curated recovery 经验库；普通运行产物不放这里，只有显式 promotion 才追加已验证关系经验或负面经验。
- `scripts/`：Yosys/ABC 相关转换、综合和 CEC 脚本。
- `install.sh`：GateSpy 环境初始化脚本，会执行 `uv sync --dev` 安装/检查依赖，并写入本地 MCP/skill 配置；当前不下载测例数据。
- `examples/`：示例网表和 AIG 文件。
- `test/`：pytest 测试。

## 当前状态

已实现 cost-aware Verilog expression discovery 和初版通用 RTL recovery：

- 底层仍保留 `src.circuit.symbolic.infer_expression(...)` 作为库函数和测试对象；MCP 层不再暴露 `infer_expression`，不能作为 agent-facing 恢复入口或复杂目标的逃生通道。
- GateSpy 自己维护 typed bit-vector expression layer，而不是直接信任 symbolic regression 输出。AST 支持 `{}`、`{{}}`、`[]`、`[:]`、`+ - * / % **`、比较/相等、shift、bitwise、reduction、logical 和 `?:`，每个节点都有 render、evaluate、width inference 和 Table-I cost。
- PySR 是候选生成后端之一，不是证明器。PySR 返回的公式必须先转成 GateSpy AST，再通过 circuit simulation 的 training/validation 样本；小 support 会穷举验证，大 support 会明确标注 sample-verified。
- 新增 `src/circuit/symbolic/` 子包：`bitvec.py`、`ast.py`、`resolver.py`、`sampler.py`、`search.py`、`pysr_backend.py`、`verify.py`、`report.py`、`engine.py`。
- 新增 `src/circuit/recovery/` 子包：`models.py`、`word.py`、`polynomial.py`、`fit.py`、`small_tools.py`、`pipeline.py`、`report.py`。它把 word/support 分析、小工具候选生成、candidate cache、candidate RTL 生成和验证状态统一成结构化报告。
- MCP 暴露的小工具包括 `plan_recovery(...)`、`recovery_tool_guide(...)`、`analyze_words(...)`、`probe_target(...)`、`validate_expr(...)`、`fit_template(...)`、`infer_native_expr(...)`、`check_polynomial_lowbits(...)`、`split_control_cases(...)`、`influence_profile(...)`、`list_candidates(...)`、`combine_case_expr(...)`、`infer_pysr_expr(...)`、`assemble_rtl(...)`、`verify_rtl_candidate(...)`。`plan_recovery` 是 agent-facing 第一入口，会按 output support/cone/role 分组，给出 per-cluster plan steps，并可通过 `feedback` 修订计划；每个 step 会显示 effort、search space、risk、prerequisites 和 why_cheapest。
- 新增 Recovery reasoning graph 层：`src/circuit/recovery/ir.py` 定义 `RecoveryRunIR`、`CircuitFingerprint`、`ReasoningGraphNode` 以及兼容旧导入的 `EvidenceItem`、`HypothesisIR`、`CandidateIR`、`CexIR`、`ToolCallIR`、`LearningEvent`。MCP 默认 `read_file(record_ir=True)` 会把 graph-only 运行产物写入 `.gate_spy/runs/<run_id>/`；库内 `CircuitSession.load(...)` 默认不写磁盘，测试/脚本可显式开启。
- 新增本地经验 RAG/知识库：`query_recovery_experience(...)` 使用确定性 token/fingerprint 检索，不依赖外部 embedding 或云向量库；`query_recovery_memory(...)` 保留为兼容外壳。RAG 命中只返回触发条件、建议小实验、关系归纳、验证来源和负面经验；检索结果不能直接进入 `C#`，必须再经 full-support `validate_expr` 或 CEC。
- `infer_expression`、`recover_expression`、`recover_rtl` 已从 MCP 暴露面移除。它们只可作为内部库函数、session guardrail 或开发 baseline 保留，不能出现在正常 agent 恢复轨迹里。
- 正常最终组装使用 `assemble_rtl(...)`，它只消费 session 中已验证通过的 accepted global candidates；缺候选时返回 missing output 清单，不自动补全。
- 小工具默认边界：`probe_target` 只做 support/cone/sample 画像；`validate_expr` 只验证显式公式并缓存 accepted candidate，不搜索，默认 `validate_scope="support"`，会在应用 `fixed_inputs={...}` 后采样目标 cone 中所有未固定 PI；显式 `validate_scope="expr_only"` 只用于切片诊断，结果标成 `slice-*`，不能缓存为 `C#`/`B#`，也不能被 `assemble_rtl` 消费；报告会列出 `sample_scope`、`support_inputs`、`expression_inputs`、`omitted_support`。显式 `inputs=[...]` 漏掉表达式里的 primary input 时会自动补齐，但未缓存的 output/internal 引用会被拒绝，避免把原电路输出当成输入形成假验证；branch-conditioned candidate 只作为 `B#` 证据，不进入 `assemble_rtl` 缓存，必须先通过 `combine_case_expr(...)` 组合成全局 `C#` 候选；`list_candidates(...)` 显示 `C#`/`B#`/`F#` 当前状态；`fit_template` 只做 bounded template fitting，当前覆盖 linear/product/comparator、signed comparator、signed affine-difference comparator，支持 `fixed_inputs` 做 branch-local full-support template validation；`infer_native_expr` 只做 GateSpy native TABLE-I AST，属于 medium-cost enumeration，宽 support 默认会返回 deferred；`check_polynomial_lowbits` 只做 bounded low-bit polynomial rewriting 并明确 skip reason；`split_control_cases` 只分析 scalar control/case，多 control 默认生成 balanced full-assignment profile 并报告低覆盖组合；`influence_profile` 在固定分支/基准输入下扰动 support bit/word，报告 signed/unsigned delta 和零影响输入，作为 CEX 后缩小边界的诊断工具，不生成公式；`infer_pysr_expr(force=False)` 只返回成本提示，必须 `force=True` 才运行 PySR。
- 已新增 `ToolProfile` / `tool_profiles.py`，统一记录 agent-facing 工具的 `effort`、`search_space`、`risk`、`requires_hypothesis`、`prerequisites`、`fallback_after` 和 guidance。默认方法顺序是 probe -> verify explicit hypothesis -> light templates/control -> medium bounded search -> heavy forced search；如果模型已经推导出公式，必须先用 `validate_expr`，不要再用 `infer_native_expr` 搜索一遍。
- 已直接改造现有 `skills/iccad22.md`，没有新建 skill。现在 skill 固定为小工具 plan-first 流程：`read_file -> plan_recovery -> probe_target/validate_expr/fit_template/infer_native_expr/check_polynomial_lowbits/split_control_cases/influence_profile -> feedback/revised plan -> assemble_rtl 或 verify_rtl_candidate -> edit/dump 可选`。
- `src/circuit/recovery/polynomial.py` 当前是 bounded v1：只对较小 support/cone 做低位多项式重写，超过 cap 会明确跳过，避免 SymPy 在大乘法器或复杂 cone 上展开失控。这类目标依赖 native/PySR/template/control 路径。
- `pyproject.toml` 已把 `pysr` 和 `sympy` 作为普通依赖；`install.sh` 会执行 `uv sync --dev` 并检查这些依赖可用。
- 已新增 `test/test_symbolic.py` 和 `test/test_recovery.py`，覆盖 Table-I operator cost/evaluator、AND、mux、concat/sign-extension parser、`examples/Mul_INT16.aig` 乘法表达式、`examples/iccad22_test01.v` 加法表达式、word/support 分析、plan/revision、tool profile、小工具拆分、显式公式验证、full-support vs expr-only slice 验证、`test29` 旧误判回归、`test13` signed comparator/branch affine comparator/CEC acceptance、`influence_profile`、candidate cache、`assemble_rtl` 原始 module port order、deprecated guardrail、control mux case split 和 AIG candidate RTL 输出。
- 已新增 `test/test_recovery_ir.py`，覆盖 graph-only 持久化、不生成 `events.jsonl`、节点裁剪、unverified relation 不会变成 positive experience、`slice-*` 不参与 promotion、experience promotion/query 返回 provenance 和 negative experience。

当前可用的模型推理思路：

1. 如果只是查结构、cone、节点关系，可以继续用 `get_node` / `find_cone` / `simulate` / `print_adder_stats` / `print_xor_stats`。
2. 如果目标是恢复 word-level RTL，先调用 `plan_recovery(detail=True)`，让模型产出 output cluster、support/cone、suspected role、工具选择和验收条件；不要直接从网表文本猜 RTL，也不要把 `recover_rtl` 当入口。
3. 对单个目标，先用 `probe_target(...)`，再按假设选择一个小工具。如果已经有公式假设，先用 `validate_expr(..., validate_scope="support")`；如果只是切片或调试假设，必须显式写 `validate_scope="expr_only"`，并把结果当作 hypothesis-only；如果是 control 分支假设，用 full-support `validate_expr(..., fixed_inputs={...})` 或 `fit_template(..., fixed_inputs={...})` 生成 branch-local `B#` 证据；否则再考虑 `fit_template(...)`、`infer_native_expr(...)`、`check_polynomial_lowbits(...)`、`split_control_cases(...)`。多个 control 必须按完整组合固定验证，不要把 `split_control_cases` 的边际 profile 当成分支真值表。如果分支都成立，用 `combine_case_expr(...)` 组合并全局验证。只有 narrowed target 上轻量/中等工具失败后，才考虑 `infer_pysr_expr(force=True, ...)`。不要调用 `infer_expression`。
4. 对 hard case，优先让 `read_file(record_ir=True)` 保留 reasoning graph；模型遇到失败必须记录 `problem` 并做小实验记录 `experiment/observation`，归纳后记录 `relation`，验证后才能 promotion 为 `experience`。`record_recovery_note(...)` 只是兼容小观察；优先使用 `record_recovery_graph_node(...)`。需要复用历史经验时用 `query_recovery_experience(...)`，把命中当作下一步实验或禁忌路径，不要把命中本身当作候选公式或验证结果。
5. 每次局部恢复前，模型应说明 mini-plan：target、hypothesis、tool、effort、why this is the cheapest sufficient tool、expected evidence、acceptance；每次失败后要总结 target、attempted method、observed failure/CEX、likely cause、revised next step。
6. 遇到 CEX、timeout、failed validation 或 unrecovered target 时，调用 `plan_recovery(feedback=...)` 或手动修订原计划，再继续局部分析。CEX 后默认提取 control assignment，运行 `influence_profile(...)` 或定向 `simulate(...)`，再做 branch-local full-support `validate_expr(...)`。失败后的默认动作是缩小问题，而不是加大搜索。
7. 对完整模块，只有局部候选稳定并进入 accepted candidate cache 后才用 `assemble_rtl(...)` 或 `verify_rtl_candidate(...)` 做最终组装/验证。`verify_rtl_candidate` 和 `edit` 不是公式搜索器；连续两个相近 candidate 出 CEX 后必须总结 CEX 模式并回到局部 probe/simulate/plan 修订。不要把旧 full-module recovery 当作 fallback；如果缺候选，回到 plan/局部分析。只有需要实际改写源码或比赛 cost 优化时，再进入 `edit` / `revert` / `dump` 流程。
8. 信任顺序：`cec-proved` 最强；`cec-timeout-assumed` 是当前项目约定下可接受但不是严格证明；`exhaustive-exact` 可当作当前 support 下的精确结果；full-support `sample-exact` / `sample-masked` 只是强证据；`slice-*` 只能用于提出假设，不能组合、组装或 promotion；RAG/experience hit 和未验证 graph observation 只能提出实验方向或负面禁忌；failed candidate 只能作为调试线索。
9. 模型使用表达式时要同时保留 cone/support 证据和表达式/验证证据。表达式说明 word-level 语义；cone、support、结构识别说明边界和电路区域。
10. 若 recovery 找不到候选，下一步应缩小目标范围（单 bit、part-select、局部 cone）、显式指定 inputs、运行 `find_cone(detail=True)` 或 `split_control_cases` 找更自然边界。不要直接从 `fit_template` 失败跳到全局 RTL 恢复。

典型 MCP 调用：

```text
read_file("examples/Mul_INT16.aig")
plan_recovery(detail=True)
probe_target("Out", inputs=["IN1", "IN2"], detail=True)
fit_template("Out", inputs=["IN1", "IN2"])
assemble_rtl()
```

## 开发约定

使用 Python 3.11+。代码风格跟随现有文件：4 空格缩进，函数和变量用 `snake_case`，类用 `PascalCase`。新增面向 MCP 的输出应保持可读、稳定，并在错误信息里给出可恢复线索，例如可用输入 bus、支持的文件扩展名或未解析信号名。

涉及解析、仿真、cone 查询、结构识别、symbolic/recovery pipeline 或 MCP 输出格式的改动，都应补充或更新 `test/test_*.py`。随机仿真测试必须固定 `seed`。长耗时或可能爆炸的搜索必须有显式 cap，并在报告中说明跳过原因。

## 开发 Plan

[x] 寻找合适的 Symbolic Regression 工具并使用或者实现：当前采用“内置 Table-I bit-vector AST + PySR 算术候选后端 + GateSpy 仿真验证”的方式提供底层候选能力；agent-facing 流程不再直接暴露 `infer_expression`。
[x] 引入 WolFEx 风格的通用 RTL recovery v1：先做 word/support/control 边界，再用多路候选生成和 bit-vector/CEC 验证输出 candidate RTL；原 `skills/iccad22.md` 已继续升级为 plan-first 流程。
[x] 将恢复流程升级为分析驱动、可迭代的 plan-first 机制：`plan_recovery` 生成/修订计划，最终阶段使用 `assemble_rtl` / `verify_rtl_candidate`，不再把旧 full-module recovery 作为 MCP 入口。
[x] 拆掉 agent-facing 的“一步到位”表达式恢复入口：`recover_expression` / `recover_rtl` 不再作为 MCP 工具暴露；新增小工具和 `assemble_rtl`，强制模型按 plan -> probe -> light attempt -> failure summary -> revised plan -> assembly 推进。
[x] 补齐显式公式验证和工具代价模型：新增 `validate_expr` 负责验证/缓存模型已推导出的表达式，新增 `ToolProfile` 和 `recovery_tool_guide`，并让 `plan_recovery` 输出 effort/search/risk，避免模型把 medium/heavy search 当默认下一步。
[x] 移除旧 `infer_expression` / `recover_expression` / `recover_rtl` 的 MCP 暴露面，并给 `validate_expr` 增加 `fixed_inputs` 分支验证：复杂 control 目标应走 `split_control_cases -> validate_expr(fixed_inputs=...) -> combine_case_expr`，而不是退回 broad symbolic search。
[x] 补齐候选复用与分支组合：`list_candidates` 展示 `C#`/`B#`/`F#`，`validate_expr` 可复用已接受目标名或 `C#` id，`combine_case_expr` 把分支证据组合成全局候选后再交给 `assemble_rtl`。
[x] 修复一次真实恢复过程暴露的问题：`validate_expr` 自动补齐表达式中遗漏的 primary input、拒绝未缓存 output/internal 变量；`split_control_cases` 增加多 control 组合 profile；skill 明确禁止把边际 profile、`verify_rtl_candidate` 或 `edit` 当作公式盲搜机制。
[x] 加强 MCP tool descriptions：在 `validate_expr`、`split_control_cases`、`verify_rtl_candidate`、`edit`、`assemble_rtl` 等工具 docstring 中直接写入输入边界、候选缓存、多 control 组合、CEX stop-rule 和非盲搜约束；这些描述会进入 MCP schema，优先影响模型选工具。
[x] 重建 hard-case recovery 的第一轮语义修复：`validate_expr` 默认改为 full-support validation，显式 `validate_scope="expr_only"` 的结果降级为 `slice-*` 且不进 candidate cache；`split_control_cases` 改成 balanced full-assignment coverage；新增 `influence_profile` 作为 CEX/branch 诊断工具；新增 `test29` 回归锁定旧的窄输入误判。
[x] 修复 `test13` 恢复链路：candidate RTL 组装保持原始 module port order 并在 CEC 前 guard 端口集合/顺序；expression parser 支持 `{}`/`{{}}` sign-extension；`fit_template` 增加 signed comparator 与 signed affine-difference comparator，并支持 `fixed_inputs` 分支模板验证；`scripts/recovery_eval.py` 在缺少候选文件时可用 bounded session flow 自动生成 test13 候选并通过 ABC CEC。
[x] 引入 graph-only Recovery IR + 本地经验 RAG：普通 tool log 不再落盘，`.gate_spy/runs/<run_id>/` 只保存 reasoning graph；用 `data/recovery_kb/` 保存显式 promotion 后的解决路径经验和负面经验；`query_recovery_experience` 只做确定性本地检索和实验建议，不返回可直接接受的候选公式，不改变验证信任链。
