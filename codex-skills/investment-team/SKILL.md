---
name: investment-team
description: "AI Berkshire skill: 投研团队：四角色并行分析框架. Source: skills/investment-team.md."
---

## Codex adapter note

This skill is generated from `skills/investment-team.md` so Claude Code and Codex users share one canonical workflow.

- Treat `$ARGUMENTS` as the user's request in the current Codex thread.
- When the source mentions Claude-only surfaces such as Task, Agent, WebSearch, Bash, Read, or Write, use the closest Codex capability available in this session: subagents when available, web search when needed, shell commands for local tools, and normal file edits for workspace files.
- Use shared project tools from `tools/` in this repository. Prefer running commands from the repository root with paths like `python3 tools/financial_rigor.py ...`; if the current thread starts outside the repo, locate the actual checkout path first instead of assuming a fixed home-directory path.
- Before starting research, run the `date` command to confirm today's date; treat it as the baseline for "latest" data and state the data cutoff date in the report header. Never assume the current date from training data.
- Preserve the research quality rules from `AGENTS.md`: cross-check financial data, use exact arithmetic tools for valuation/math, and clearly label uncertainty and source gaps.

# 投研团队：四角色并行分析框架

对 $ARGUMENTS 进行团队化投资研究分析。使用 Team 工具创建真正的多Agent并行研究团队。

## 执行流程

### 第一步：展示团队框架

向用户展示以下团队结构，与下面的「AI研究偏见评估」「WebSearch 预检」**合并成一次确认**后启动（用户已在命令里写明"直接跑 / 不用确认"时不再停下）：

| 角色 | 职责 | 分析框架 |
|------|------|----------|
| **team-lead**（你自己） | 统筹协调、汇总研判、输出最终报告 | 四大师综合框架 |
| **business-analyst** | 商业模式 & 护城河分析 | 段永平视角 |
| **financial-analyst** | 财务报表 & 估值分析 | 巴菲特视角 |
| **industry-researcher** | 行业格局 & 竞争态势 | 芒格视角 |
| **risk-assessor** | 风险评估 & 管理层研判 | 李录视角 |

### 第一步半：AI研究偏见评估

在创建团队前，先向用户展示该公司的"AI可研究性"评估：

**信息丰富度评级**（决定研究策略）：
| 等级 | 特征 | 研究策略调整 |
|------|------|------------|
| A级（信息充裕） | 上市多年、券商覆盖广 | 团队重点放在**反面检验**和**非共识视角**，避免输出与市场一致的"正确的废话" |
| B级（信息适中） | 上市不久、覆盖有限 | 每个Agent的推算数据必须标注置信度，team-lead汇总时标注"数据充分度" |
| C级（信息稀缺） | 冷门/新上市/新兴市场 | 团队转为"第一性原理模式"：不追求报告完整性，聚焦商业本质的几个核心问题 |

**关键提醒**：资料多≠确定性高，资料少≠确定性低。AI能输出的置信度 ≠ 投资的真实确定性。确定性来自商业模式本身，不来自资料数量。

将评级结果告知每个Agent，影响其研究方式。

### 第一步¾：WebSearch 权限预检（关键 · 避免 Agent 静默退化）

在创建团队、启动任何后台 Agent **之前**，必须先确认 WebSearch 权限已放行。

**为什么必须预检**：本 skill 用 `run_in_background: true` 启动 4 个后台子 Agent，而**后台 Agent 无法向用户弹出交互式权限确认**。若 `WebSearch` 未在 `.claude/settings.local.json` 的 `permissions.allow` 白名单中，子 Agent 的联网搜索会被**静默拦截**，导致其退化为仅凭训练知识（有知识截止日期）作答，却仍按框架输出一份"看起来完整、实则未联网"的伪研究——这是本 skill 最危险的失败模式（见 issue #58）。

**预检步骤**：
1. 用 Bash 检查白名单是否含 WebSearch：
   ```bash
   grep -l '"WebSearch"' .claude/settings.local.json ~/.claude/settings.local.json 2>/dev/null
   ```
2. 若两处都未命中（即未放行）→ **停下来，不要启动 Agent**，提示用户：
   > ⚠️ 检测到 WebSearch 未在权限白名单中。后台研究 Agent 无法联网，会退化成仅凭训练知识作答。请先在 `.claude/settings.local.json` 的 `permissions.allow` 加入 `"WebSearch"`（或运行 `/permissions` 勾选），再重跑本命令。
3. 命中 → 正常继续。

### 第一步⅞：共享数据底稿（关键 · 消灭"四份报告四个数"）

在启动四个视角 Agent **之前**，先产出一份所有人共用的事实表：`reports/{公司名}/00-数据底稿.md`。

**为什么必须先做底稿**：四个 Agent 各自联网搜同一批基础事实（最新季报数字、资本开支指引、股价股本、判决日期），必然搜到不同日期的文章，产出互相打架的数字——实测一次研究中同一个 Capex 指引出现过三个版本，其中一个不对应任何真实指引。基础事实只允许被查一次、被引四次。

**谁来做**：team-lead 亲自做，或派一个 `data-clerk` Agent **前置串行**完成（不要和四个视角 Agent 并行）。

**先查有没有现成的**——四个 Agent 并行跑，同一轮内彼此的取数结果互相帮不上忙（都在同一时间窗），缓存真正的价值在**跨轮复用**。所以第一件事是体检旧底稿：

```bash
# 美股（SEC XBRL）
python3 tools/usstock_data.py datasheet --check reports/{公司名}/00-数据底稿.json
# A股 / 台股：同一 schema，各自联网核对交易所是否已有更新报告期
python3 tools/ashare_data.py  datasheet --check reports/{公司名}/00-数据底稿.json
python3 tools/twstock_data.py datasheet --check reports/{公司名}/00-数据底稿.json
```

- ✅ 新鲜 → **直接沿用，不要重新取数**（实测第三轮完全复用第二轮底稿，省掉整个取数阶段）
- ❌ SEC 已有更新季度 → 重跑下面的命令刷新
- 无此文件 → 首次研究，直接生成

**产出两份底稿，一份给人读、一份给机器判过期**：

```bash
# 机器可读版：每个字段带 as_of（数据期末）与 fetched_at（实际取数时间）
# 美股：SEC XBRL，证据等级 A
python3 tools/usstock_data.py datasheet {TICKER} --cik {CIK} \
  --price {基准价} --as-of {基准日} --out reports/{公司名}/00-数据底稿.json
# A股：东方财富 F10（证据等级 B，年报/巨潮公告核对后方可升 A）
python3 tools/ashare_data.py datasheet {代码} --price {基准价} --as-of {基准日} --out reports/{公司名}/00-数据底稿.json
# 台股：FinMind（证据等级 B，按 skills/financial-data.md 台股章节与 Goodinfo 交叉验证）
python3 tools/twstock_data.py datasheet {代码} --price {基准价} --as-of {基准日} --out reports/{公司名}/00-数据底稿.json
# 港股：暂无取数工具，JSON 可省略；Markdown 底稿必须写全，股价/股本以港交所披露易 + 公司 IR 为准
```

`00-数据底稿.md`（人读）承载分部、指引、监管、治理等非 XBRL 内容；`00-数据底稿.json`（机器读）承载可自动判断时效的结构化财务序列。**基准价一旦写入 JSON 的 `basis.price`，即为全队基准价**。

**底稿必含内容**（每一项都带 `截至日期` 和 `来源`，指引类必须写明"来自哪次财报/电话会、发布日期"）：

| 区块 | 内容 | 一手来源 |
|---|---|---|
| 行情 | 股价（注明日期与股份类别）、总股本、市值验算（`financial_rigor.py verify-market-cap`） | 美股：`tools/usstock_data.py quote`；其余：交易所/公司IR + stockanalysis。**底稿定下的股价即全队基准价**，之后任何工具调用（`valuation`、`forward-range`、`three-scenario`）一律传 `--price {基准价}`，禁止再实时拉价——实测两个视角各自拉价会漂移 1% 并触发一致性冲突 |
| 最新季报 | 营收、经营利润、GAAP 净利润、**一次性/非经常项目及其金额**、OCF、Capex、FCF、各分部收入与经营利润 | 美股：`tools/usstock_data.py quarterly --json`（分部数据另看 8-K Exhibit 99.1）；港股/A股：交易所公告 / 巨潮 |
| 近 8 季 | 上述核心科目的季度序列 | 10-Q/10-K 或同等文件 |
| 指引 | 资本开支、收入/利润指引——**只保留最新一次**，旧指引用括号注明"原为…，YYYY-MM-DD 上调/下调" | 财报电话会、新闻稿 |
| 关键日期 | 诉讼判决、上诉排期、监管处罚、重大融资/回购/并购 | 法院文书、公司公告 |
| 口径警示 | 会计估计变更（折旧年限等）、汇率、股份类别、ADR 比率 | 10-K 附注 |

**若该公司已有上一轮最终报告，先把上轮 team-lead 复核记录转成本轮抽查清单**——复核者自己也会错（实测把正确的 $847.5 亿"订正"成 $800 亿，下一轮才翻出来），上轮每一处订正都要在本轮被**不同角色**独立核一次：

```bash
python3 tools/report_audit.py review-items --report reports/{公司名}/最终报告.md \
  --out reports/{公司名}/00-数据底稿.md      # 追加为底稿末尾的「上轮复核待查」表
```

四个 Agent 做底稿抽查时**优先从这张表里挑**（标"高"的是上轮没填依据或没填核验日期的项）。

**每条底稿条目必须带证据等级标记**：

| 标记 | 含义 | 谁能打 |
|---|---|---|
| ✅ | team-lead 或某个 Agent **直接读过一手原文**（SEC/交易所/法院/监管/公司IR），并写明文件名与日期 | 读过原文的人 |
| ⚠️ | 来自二级转述、上一轮研究、或子任务引述，**未见一手原文** | 默认状态 |

**⚠️ → ✅ 的升级规则**（不许口头升级）：必须在底稿该行补上①一手文件名或 URL、②核验日期、③核验人（team-lead 或哪个角色）。三样缺一，标记保持 ⚠️。降级同理：被证伪的条目改写为订正后的值并注明原值与证伪依据。

**四个视角 Agent 的引用纪律**（写进每个 Agent 的 prompt）：
1. 基础数据**只准引底稿**，不得自行联网重查；联网只用于各自领域的增量信息（行业份额、竞品动态、管理层言论、判决细节等）
2. 若联网发现与底稿冲突的数据，**在报告中标注冲突并上报 team-lead**，不得私自改数
3. 指引、预期类数字必须连同底稿中的日期与来源一起引用
4. **每个 Agent 必须抽查自己领域内的 3 条底稿条目**（优先挑 ⚠️ 的），去查一手原文，在报告固定小节里报「证实 / 证伪 / 核不到」——单一事实源的代价是错误会被四份报告同时继承，抽查是唯一的解药。实测底稿自身曾同时错 4 处（指引原值、融资用途、来源标注、月活口径），全部由 Agent 抽查翻出

### 第二步：创建团队

使用 TeamCreate 创建团队：
- team_name: `{公司名}-research`（英文小写，如 `meituan-research`）
- agent_type: `team-lead`

### 第三步：创建4个任务

使用 TaskCreate 创建以下4个任务（每个都要有 subject、description、activeForm）：

#### 任务1：商业模式分析
- subject: `分析{公司名}商业模式、护城河与用户价值`
- description 包含：
  1. 商业模式本质：核心生意定义、收入结构拆解
  2. 平台/产品飞轮效应如何运转
  3. 护城河分析：品牌/转换成本/网络效应/规模效应/技术壁垒，逐一验证
  4. 用户/客户价值：为各方创造了什么独特价值
  5. 业务矩阵与协同效应
  6. 段永平"好生意"标准评估：差异化、定价权、可持续竞争优势
  7. 要求搜索最新财报、行业报告等公开信息

#### 任务2：财务与估值分析
- subject: `分析{公司名}财务数据、盈利能力与估值`
- description 包含：
  1. 近3-5年营收、净利润、经营利润趋势
  2. 盈利能力指标：ROE、ROA、毛利率、经营利润率
  3. 现金流分析：经营性现金流、自由现金流、资本开支
  4. 资产负债表健康度：现金储备、负债率、流动性
  5. 估值分析：PE/PS/PB/EV等，与历史及同业对比
  6. 安全边际评估：内在价值 vs 当前股价
  7. **金融严谨性验证（必须使用Bash调用工具，禁止心算）**：
     - 市值验算：`python3 tools/financial_rigor.py verify-market-cap --price {价格} --shares {股本} --reported {报告市值} --currency {币种}`
     - 估值验算：`python3 tools/financial_rigor.py verify-valuation --price {价格} --eps {EPS} --bvps {每股净资产}`
     - 关键数据交叉验证：`python3 tools/financial_rigor.py cross-validate --field {字段} --values '{JSON}' --unit {单位}`
     - 三情景估值：`python3 tools/financial_rigor.py three-scenario --price {价格} --eps {EPS} --shares {股本亿} --growth {乐观} {中性} {悲观} --pe {乐观PE} {中性PE} {悲观PE}`
     - 将工具输出结果直接嵌入报告中作为验证记录

#### 任务3：行业与竞争分析
- subject: `分析{行业}行业格局与{公司名}竞争态势`
- description 包含：
  1. 行业规模与增长：市场规模、增速、渗透率
  2. 竞争格局：主要对手市场份额、竞争策略对比
  3. 核心竞争者威胁评估：逐个分析主要竞争对手
  4. 各细分赛道格局
  5. 行业趋势：技术变革、政策影响、新进入者
  6. 产业链分析：上中下游价值分配
  7. 要求搜索最新行业数据和竞争动态

#### 任务4：风险与管理层评估
- subject: `评估{公司名}投资风险与管理层质量`
- description 包含：
  1. 管理层评估：CEO能力圈、诚信度、战略眼光、资本配置能力、历史决策质量
  2. 监管风险：当前及潜在监管影响
  3. 竞争风险：各竞争对手威胁程度评估
  4. 业务风险：新业务亏损、扩张不确定性
  5. 宏观风险：经济周期、行业周期影响
  6. 治理结构：股权结构、关联交易、股东回报政策
  7. 长期确定性：10年后公司会怎样？什么可能颠覆其商业模式？
  8. 要求搜索最新监管动态、管理层言论等

### 第四步：启动4个并行Agent

使用 Task 工具同时启动4个Agent（**必须在同一条消息中并行调用**）：

每个Agent的配置：
- `subagent_type`: `general-purpose`
- `run_in_background`: `true`
- `team_name`: 对应团队名
- `name`: 对应角色名（business-analyst / financial-analyst / industry-researcher / risk-assessor）

每个Agent的prompt模板：

```
你是{公司名}投研团队中的"{角色中文名}"，负责从{大师名}投资视角分析{公司名}。

请完成任务 #{任务编号}：{任务subject}

具体要求：
{任务description的内容}

**研究方法**：
- **基础数据只准引 `reports/{公司名}/00-数据底稿.md`**（股价/股本/季报数字/指引/关键日期），不得自行联网重查；联网只用于你所负责领域的增量信息。发现与底稿冲突的数据，在报告中标注冲突并告知 team-lead，不得私改
- 调用任何取数/估值工具时**必须传 `--price {底稿基准价}`**（`usstock_data.py valuation`、`financial_rigor.py forward-range/three-scenario`），不许工具自动拉实时价
- **能用 WebFetch 就别用 WebSearch**：底稿与他人报告里已记了一手 URL 的，直接 fetch 那个 URL——WebFetch 按 URL 缓存 15 分钟且结果可复现，WebSearch 不缓存、排序还会漂。只有在"不知道该看哪份文件"时才用 WebSearch 找入口，找到后仍应 fetch 原文而不是引用搜索摘要
- 使用 WebSearch 搜索你领域内的最新公开信息（行业报告、竞品动态、管理层言论、判决文书）
- **叙述类断言的信源分级**（数字之外的事实同样要分级）：一手（SEC/公司IR/法院文书/监管公告）> 主流财经媒体（Bloomberg/Reuters/WSJ/FT/CNBC）> 行业研究机构（StatCounter/Synergy/eMarketer 等，注明口径）> 聚合博客与自媒体（**只能作线索，不得作为唯一证据**，引用时必须标注"单一低级别来源"）
- **优先调用项目自有工具，禁止心算**：`tools/financial_rigor.py`（市值/估值验算、交叉验证、三情景、`forward-range` 前瞻价值区间）、`tools/terminal_value.py`（终值与反向折现——财务 Agent 必须用它回答"当前股价隐含了多少年、多高的增长"）、`tools/usstock_data.py`（美股 SEC 一手取数）、`tools/twstock_data.py`（台股取数）
- **财务数据必须来自两个独立来源**，按 `skills/financial-data.md` 规范执行（美股：macrotrends+stockanalysis；港股：aastocks+macrotrends；A股：东方财富+巨潮资讯；台股：FinMind `tools/twstock_data.py`+Goodinfo），两源误差>1%须标记
- 确保数据准确，关键数据标注来源
- 分析要深入，不流于表面
- **联网失败禁止伪装**：若 WebSearch 被拦截/不可用，禁止用训练知识冒充联网结果。必须在报告顶部醒目标注「⚠️ 本报告未能联网，基于训练知识（截止日期 X），置信度降级」，并如实告知 team-lead，由其决定是否中止研究
- **核实子任务上限 2 个**：确需派子 Agent 去核一手文件时，每个子任务只问一个问题、只核一份文件，最多派 2 个；超出的核实需求写进报告「数据缺口」小节并告知 team-lead，由其统一派发。实测单个 Agent 自派 4 个子任务耗掉 51 万 token，占全队六成

**必做的三张表（缺一份报告即打回）**：

1. **底稿抽查表**——抽查你领域内 3 条底稿条目（优先 ⚠️），查一手原文：

   | 底稿条目 | 原值 | 一手来源（文件名/URL） | 核验日期 | 结论 |
   |---|---|---|---|---|
   | | | | | 证实 / 证伪（新值：…） / 核不到 |

   「一手来源」必须具体到**文件名/URL + 附注号或页码**（如 `10-K FY2026 Note 1，sec.gov/…/msft-20260630.htm`）；"在 10-Q 里见过""公司披露"不算来源。`evidence` 工具会把来源不具体或缺核验日期的"证实"降为"证实(凭据不全)"，不得升 ✅——上一轮两个 Agent 把"见过"写成"一手证实"，这是新出现的失败模式

2. **双算复核表**——标题必须含"双算"二字，供 `report_audit.py crosscheck` 机器比对：

   | 指标 | 本报告值 | 算式 / 工具 |
   |---|---|---|

   **指标名必须用字典里的基名**（`python3 tools/report_audit.py crosscheck --list-metrics` 打印全表），口径写在紧随其后的括号里，如 `净现金(狭义)`、`核心EPS(全额剔除)`、`市值(稀释股本)`。净现金 / 市值 / 核心EPS / 核心PE / 隐含增速是口径敏感指标，**值不同又没带括号口径的会被判"口径未标"**。字典外的指标可以写，但只作信息展示，不参与打回判定——各角色随手起名（"核心TTM EPS" / "TTM 核心 EPS"）是双算永远配不上对的首要原因。

   各角色的必算项（财务 Agent 算全部，其余角色按分工复算，**同一指标至少两个角色各算一次**）：
   | 角色 | 必算指标 |
   |---|---|
   | financial-analyst | 核心EPS、核心PE、市值、FCF利润率、隐含增速（反向折现）、稀释率（若有融资） |
   | risk-assessor | 稀释率、净现金、回购均价 |
   | business-analyst | 分部利润率、TAC率（或对应的单位经济指标） |
   | industry-researcher | 份额变化、增速（自算一遍，不直接抄机构口径） |

   **所有派生指标必须用 `tools/` 下的工具算并留算式，禁止心算**——实测曾出现稀释率漏除存托股 1/20、错 20 倍仍通过全部检查的事故，只有双算能拦住。

3. **关键事实清单**——本报告主张的 3–5 条关键事实，每条一行，带证据等级（一手/主流财经/行业机构/聚合）。team-lead 横向比对这四张小表即可发现叙述层矛盾，不必通读全文。

**输出要求**：
- 将完整报告写入 `reports/{公司名}/0{N}-{维度}-{大师}视角.md`（UTF-8，首行 `# 标题`），文件名见 CLAUDE.md
- 报告要详尽，使用Markdown表格呈现关键数据
- 每个分析维度要有明确结论和评分；**评分只用整数星（★1-5），禁止 3.5/5 这类半星**
- **货币单位统一用"亿"**（亿美元/亿港元/亿人民币），不要在同一份报告里混用 B/M 与亿；引用 $XXB 数据时换算成亿并保留原值
- 报告末尾要有该维度的总体结论

**完成后**：
1. 使用 TaskUpdate 将任务 #{任务编号} 标记为 completed
2. 通过 SendMessage 向 team-lead 发送 **5 条以内的核心要点摘要**（type: "message", recipient: "team-lead"）——完整报告已在文件里，不要在消息里重复整篇
```

### 第五步：接收报告并跟踪进度

- 向用户实时展示进度表（哪些Agent已完成、哪些仍在研究中）
- 每收到一份报告，更新进度并展示该报告的核心要点（3-5条）
- 等待全部4份报告到齐

### 第五步半：交叉质询（四份落盘后、关团队前）

四个 Agent 并行时互不可见，"相互印证和挑战"此前只发生在 team-lead 脑子里。这一步用最小成本把挑战做实：

1. team-lead 把四份报告的「关键事实清单」（各 3–5 条）合并成一张表，连同一句话问题——"你不同意哪一条、依据是什么"——用 SendMessage 发给四个 Agent（仍在团队内，**不要先 shutdown**）
2. 每个 Agent **只回答异议**：不同意的条目编号 + 一句依据（一手来源）+ 若涉及数字给算式；没有异议就回"无异议"。**限 300 字以内，禁止重述自己的报告，禁止为此派子任务**
3. team-lead 把四份异议汇成 `reports/{公司名}/00-交叉质询.md`（表：条目 | 提出方 | 异议方 | 依据 | 裁决），裁决连同依据写进最终报告第 9 节复核记录

成本约为一个核实子任务的十分之一，产出正是 team-lead 要找的叙述层矛盾（"03 号称未见公开证据、01 号引了官方博客"这类，机器检查完全看不见）。

### 第六步：关闭团队成员

全部报告收到后，向4个Agent发送 shutdown_request（使用 SendMessage，type: "shutdown_request"）。

### 第六步半：机器复核（汇总前必跑）

```bash
# 四份底稿之间的关键数据是否打架（指引 / 股价 / 股本 / 市值 / 净利润 / 一次性项目 …）
python3 tools/report_audit.py consistency --dir reports/{公司名}

# 关键派生指标是否被两个角色各算一次（consistency 查不出"自己算错"，这条才行）
python3 tools/report_audit.py crosscheck --dir reports/{公司名} \
  --require '核心EPS,核心PE,市值,FCF利润率,隐含增速'

# 证据台账：把四份报告的「底稿抽查表」汇总成可审计的账
python3 tools/report_audit.py evidence --dir reports/{公司名} \
  --company {公司名} --out reports/{公司名}/00-证据台账.md

# 格式与纪律 lint：半星、主观表述、无日期指引、单位错位、派生指标无算式
python3 tools/report_audit.py lint reports/{公司名}/0*.md
```

**证据台账的三条处理规则**：
- **证伪项**：立刻回写底稿（订正值 + 原值 + 证伪依据），并检查其他三份报告是否引用了旧值——不回写，下一轮会继续错
- **证实项**：同时有**具体**一手来源（文件名/URL + 附注）与核验日期的，底稿标记升 ✅；工具判为"证实(凭据不全)"的保持 ⚠️
- **核不到项**：保持 ⚠️，全文不得当作确定事实引用，并写进最终报告的「数据缺口」小节


四条命令任一报错，先查清楚谁错了再汇总——**尤其 crosscheck 报不一致时，不要取平均、不要选看起来顺眼的那个，要回到算式看谁漏了因子**。crosscheck 报「口径未标」不是算错，是让该角色补括号口径后重跑；报「口径不同」时在最终报告并列两种口径并注明。字典内指标"仅单算"的要补算，字典外的不用管。

另外人工做一件机器做不了的事：结合第五步半的交叉质询结果，把四份报告的「关键事实清单」并排比一遍，找**叙述层矛盾**（数字一致但说法打架）。实测曾出现一份报告称某合作有一手证据、另一份称"未见公开证据"，`consistency` 完全看不见。

### 第七步：汇总最终报告

综合4份分析报告，输出以下结构的最终报告：

---

#### 1. 一句话结论
> 用一段话（50-100字）概括是否值得投资及核心逻辑

#### 2. 四维评分总表
| 维度 | 框架 | 评分(1-5星) | 核心判断 |
|------|------|------------|----------|

综合评分：X / 5

> **聚合规则**：四维算术均值取整数星。均值恰为 .5 时，若"财务估值"或"风险管理层"任一维度 ≤ ★3，则**向下取整**（估值与确定性是买入决策的约束项，不能被业务质量平均掉）；否则向上取整。
> **封顶规则**：财务估值或风险管理层任一维度 ≤ ★2 时，综合评分不得高于该维度 +1。实测 4/3/4/2 均值 3.25 取 ★3，"长期确定性 2 星"被业务质量平均掉了——约束项在边界之外同样不能被平均。在报告中写明取整与封顶依据。

#### 3. 核心数据速览
关键财务和经营指标表格（近2年对比）

#### 4. 各维度分析摘要
每个维度摘取3-5条最重要的发现

#### 5. 投资论点（Bull vs Bear）
- 🟢 看多逻辑（5-7条）
- 🔴 看空逻辑（5-7条）

#### 6. 巴菲特买入前Checklist
| # | 检查项 | 通过? | 说明 |
10个核心检查项，逐一评估

#### 7. 最终投资建议
- 定性判断表（生意质量/管理层/估值/时机）
- 分层操作建议表（激进型/稳健型/保守型 → 建议+价格区间）
- 关键催化剂（加仓信号/减仓信号各3-5条）

#### 7½. 前瞻价值区间与更新记录

用 `python3 tools/financial_rigor.py forward-range --price {现价} --eps FY{N}:{EPS} FY{N+1}:{EPS} FY{N+2}:{EPS} --pe {低} {高} --pe-mid {中位} --eps-basis "{口径}" --as-of {基准日}` 生成：

| 财年 | 预期 EPS | EPS 口径与来源（含分析师数、是否单源） | 价值区间 | 中位价 | 中位 vs 现价 | 现价隐含 PE |
|---|---|---|---|---|---|---|

三个财年各有角色，表中要写明：**今年 = 校验**（现价是否被今年盈利支撑）、**明年 = 交易中**（市场按 NTM 盈利定价，下半年起主要看明年）、**后年 = 预计**（明年市场会滚动到它）。另给一行 NTM 混合 EPS（按剩余月份加权今年与明年）与现价隐含 NTM PE，这是"市场现在到底在按几倍交易"的唯一正确口径。

硬规则：
- **EPS 必须是剔除一次性损益的口径**。卖方一致预期若包含投资浮盈/减值/罚款，先拆出来再乘 PE，并在表中写明拆法
- **PE 区间必须写依据**（自身 5/10 年均值与分位、同业），不得拍脑袋；默认不含历史极端高点
- 至少给 FY+1 与 FY+2；FY+2 若只有单源要标注
- 解释"现价落在哪一年的区间的什么位置"——这是把估值结论翻译成可执行价位的一步
- 不含技术面（筹码/均线）——不在框架内且不可核验
- 末尾附「更新记录」表，每次财报或一致预期变化后**追加一行、不覆盖**，让读者看到预期如何漂移

#### 8. 总结段落
100-200字的最终总结

#### 9. team-lead 复核记录（必附）

| # | 事项 | 处理 | 依据（一手来源/链接） | 核验日期 |
|---|---|---|---|---|

**复核者自己也会错，而且没有下游能拦住**——实测曾发生 team-lead 把一份**正确**的融资金额"订正"成错的，直到下一轮才被翻出来。因此每一处订正都必须填「依据」与「核验日期」两列；**填不出依据的，就不要改**，改为在报告中并列两种口径并标注待核。本表的每一项自动进入下一轮研究的底稿抽查清单。

---

### 第八步：保存报告

按 CLAUDE.md 的目录规范写入 `reports/{公司名}/`：

```
reports/{公司名}/
├── README.md                         — 研究框架概览 + 核心结论 + 四维评分表 + 关键检验点
├── 00-数据底稿.md                     — 第一步⅞ 产出（人读：分部/指引/监管/治理）
├── 00-数据底稿.json                   — 第一步⅞ 产出（机器读：带 as_of/fetched_at，跨轮复用靠它判过期）
├── 00-证据台账.md                     — 第六步半产出（底稿抽查汇总，⚠️→✅ 的凭据）
├── 00-交叉质询.md                     — 第五步半产出（四方对彼此关键事实清单的异议与裁决）
├── 01-商业模式分析-段永平视角.md
├── 02-财务估值分析-巴菲特视角.md
├── 03-行业竞争分析-芒格视角.md
├── 04-风险管理层评估-李录视角.md
└── 最终报告.md                       — 第七步产出
```

若目录内已有同一公司的旧报告，在 README 的「历史报告」区列出，并注明其基准日与已过期的口径（不要回改旧报告正文）。

### 第八步半：生成投资论文，交接给 /thesis-tracker（必做）

**为什么必须做**：事前审计只能保证"当时可得信息下没算错"，**投研流程唯一真正的质量检验是事后回看**。报告里写的检验点若只躺在 Markdown 里，下季财报出来时没有任何东西会提醒你回看——三轮研究里这一步一直缺失。

把最终报告的结论落成 `reports/{公司名}/{公司名}-thesis.md`，格式对齐 `skills/thesis-tracker.md` 模式A，使下次 `/thesis-tracker {公司名}` 可直接进入模式B逐条检查：

```markdown
# {公司名} 投资论文

> 建立日期：YYYY-MM-DD ｜ 来源：/investment-team {基准日} 研究 ｜ 基准价：$X ｜ 综合评分：★N
> 状态：**已买入**（价格/仓位）｜ **观察中**（未买入，触发买入的条件见估值锚点）

## 核心论文（200字以内）
（若已买入，用 thesis-tracker A1 的五句式；若未买入，写"什么条件成立才买"的五句式）

## 核心假设清单
| # | 核心假设 | 验证方式 | 验证频率 | 当前状态 |
（3–7 条，从最终报告各维度的关键判断与前瞻区间的 EPS 假设转化而来）

## 红线清单
| # | 红线条件 | 严重程度 | 触发后动作 |
（直接搬最终报告的「减仓信号」，补上严重程度与动作）

## 估值锚点
| 指标 | 当前 | 乐观目标 | 中性目标 | 悲观情景 |
（搬前瞻价值区间表与三情景；未买入时额外写明"启动买入"的价位）

## 检验点日历
| 日期 / 触发事件 | 要看什么 | 对应假设# |
（搬最终报告的关键催化剂与已知日程：财报日、判决排期、整改期限、转股日…）

## 追踪记录
（初始为空，由 /thesis-tracker 模式B 追加）
```

**结论是"不买入"时同样要建**——记成观察论文。不建的话，三个月后没人记得当初的买点是多少、凭什么。

**检验点日历必须有具体日期**（财报日写"约 YYYY-MM-DD"，判决/整改期限写截止日，"持续观察"类另起一行），因为提醒靠机器读日期：

```bash
python3 tools/thesis_calendar.py                 # 45 天内到期 + 逾期未检 + 从未追踪的论文
python3 tools/thesis_calendar.py --company {公司名}
```

把第一条挂到每周定时任务（cron 或 Claude 定时任务），退出码非 0 即有逾期检验点，对应公司跑 `/thesis-tracker {公司名}`。实测 6 份论文里 3 份从未追踪、4 份没有带日期的检验点——没有这一步，第八步半写的东西不会被任何人回看。

### 第九步：数据抽检（准出流程）

```bash
# Step 1 — 提取抽检清单（15%随机抽样）
python3 tools/report_audit.py extract \
  --report <报告文件路径>

# Step 2 — 对清单每项从可靠信源取数（参见 skills/financial-data.md）

# Step 3 — 输出准出/打回判决
python3 tools/report_audit.py verdict \
  --results '<填好的JSON>' \
  --report <报告文件名>
```

**抽检两条纪律**（三轮实测暴露的问题）：
1. **分层抽样（工具已内置）**：`extract` 默认在随机样本外强制补足 2 个派生指标点 + 2 条带一手引用的点，并跳过复核记录 / 成本对照 / 更新记录等流程元数据表（此前抽到过"壁钟 7.4 分钟"）；清单的「层」列标明每条属于哪一层，`--no-stratify` 可关闭。纯随机抽到的多是营收、净利这类最不容易错的数字。
2. **不要自己核自己**：写报告的人填 `fetched_value` 等于自我背书。抽检取数交给一个独立 Agent 或换一个角色做。

**【准出】** 全部通过 → 报告可发布；**【打回】** 有不通过 → 修正后重审。

### 第十步：清理团队

使用 TeamDelete 清理团队资源。

## 重要注意事项

1. **4个Agent必须并行启动**——在同一条消息中调用4次Task工具
2. **Agent 把完整报告写进文件，只用 SendMessage 发摘要**——文件是交付物，消息是信号；四份底稿落盘后 team-lead 才能跑一致性检查
3. **数据准确性**——基础数据只查一次（第一步⅞ 底稿），四个 Agent 只引不查；各自领域的增量数据用 WebSearch 并交叉验证
4. **结论要明确**——不回避给出买入/观望/回避建议和具体价格区间
5. **所有分析必须有数据支撑**——附数据来源
6. **耐心等待**——4个Agent研究需要几分钟，实时向用户更新进度
7. **反偏见意识**——team-lead在汇总时必须评估：各Agent的分析是否受限于资料充裕度？是否与市场共识过度趋同？最终报告需包含"信息丰富度评级"和"AI研究局限性声明"
8. **信息稀缺时的诚实原则**——宁可在报告中留白标注"数据不足"，也不要用推测填满框架伪装确定性
9. **子任务有上限**——每个 Agent 最多派 2 个核实子任务；超出的写进数据缺口，由 team-lead 统一派发
10. **上一轮的错要被这一轮独立核**——做底稿先跑 `review-items` 把上轮复核记录转成抽查清单；team-lead 自己的订正不是终审
11. **交叉质询不可跳过**——四份落盘后先让四方互提异议再关团队；这是唯一能让"相互挑战"真实发生的步骤
