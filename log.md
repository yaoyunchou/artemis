# 修改日志

## 2026-10-08

- **增强 MCP 运行日志**（用户要求：不改变逻辑，尽可能详细地输出运行日志）。
  只加字段、只加记录；**不动任何分支、不动任何返回值、不动学习器**。改 `artemis/mcp/hands_server.py`
  和 `artemis/mcp/hands_trace.py` 两个文件。
  - **`source` 字段**：每次 `click` / `long_press` / `focus_and_clear_text` 标 `tree`（按元素编号）
    或 `coord`（按归一化坐标）；`tree` 时附 `index` / `bounds` / `element_class`。
    这就直接回答了「这一步靠元素树还是靠截图坐标」——以前只能靠 `text` 是否为空猜，还猜不准。
  - **`pkg` 前台包名**：`look` 时用 `dumpsys` 读一次并缓存到 `runtime.last_package`，
    之后每条动作记录自动带上（零额外开销）。**跨 App 跳转 = 相邻两条 `pkg` 不同**。
    带 3 秒超时、异常全吞、失败沿用旧值；环境变量 `ARTEMIS_HANDS_LOG_PKG=0` 可整体关掉。
  - **失败也记**：以前 `if result.ok:` 才写日志，失败什么都不留。现在失败落
    `op: "<工具名>_failed"` + `reason` + `code`，**走 `append` 而非 `note`**（不进当前行事件流，
    不会被 `compress_steps` 误学成一步可重放脚本）。
  - **补全「哑工具」**：`long_press` / `input_text` / `manage_app` / `open_link` /
    `erase_one_char` / `focus_and_clear_text` / `wait_for_text` / `click_sequence` / `use_device`
    之前**一条日志都不写**，现在全记。
  - **`look` 加字段**：`elements` / `width` / `height` / `hierarchy_ok` / `settle_ms` / `pkg`；
    **失败的 look 也记**（`op: "look_failed"`）——以前读不到树时日志是空的，正是最难排查的时候。
  - **`replay_row` 自己的记录**（最大盲区）：新增 `op: "replay_start"`（步骤数 + 各类型计数 +
    `script_fails`）、`op: "replay_step"`（第几步 / 期望什么字 / 是否 optional）、
    `op: "replay_end"`（`ok` / 第几步失败 / `reason` / `script_fails` / `cleared`）。
    **从此重放次数与成功率可直接从 jsonl 统计**（以前只能靠手写日志的 `how` 反推）。
  - 重放产生的 `click`/`swipe`/`wait`/`press_key` 带上 `replay: <行名>` + `step: N`，与 AI 操作区分。
  - `begin_row` 带 `pkg`；`end_row` 带 `button` / `elements` / `pkg`，学成功时还带写入的 `steps`。
    `HandsTrace.begin/finish/abandon` 加可选 `extra` 参数（默认 `None`，向后兼容）。
  - **安全性依据**：`compress_steps` 只读 `op=="look"` 的 `texts` 与 `op=="click"` 的 `text`；
    `settle` / `_open_rows_from_log` 只读 `begin_row` / `end_row`。新 op 名全部落在这之外，被自然忽略。
    `tests/unit/mcp` 242 passed / 1 skipped（唯一失败是 Windows 专用用例，在 macOS 上改动前就失败）。
  - ⚠️ **要重载 MCP 才生效**（长驻进程不会自己带上新字段）。

- **补全「日志→脚本」闭环所需的 A 类字段**（依据 `闲鱼币任务_日志补全要求与闭环设计.md` §4 A 清单；
  用户要求：**只加字段、只加记录、只加带默认值的可选参数，绝不改任何判定/返回值/学习器**）。
  改 `artemis/mcp/hands_trace.py`、`artemis/mcp/hands_server.py`、
  `artemis/mcp/hands_session.py`、`cli_study/.../catalog_service.py`，新增
  `.codebuddy/skills/xianyu-coin-tasks/scripts/note_run.py`。
  - **A1 `intent`**：`click` / `long_press` / `swipe` 各加可选参数 `intent: str = ""`；
    成功记录里 `intent` 非空才写该键（空则不写，旧日志形态不变）。把像素动作升级成语义动作，
    这是能不能从日志里挖出稳定脚本的分水岭。
  - **A2 真 `run_id` + `run_seq`**：`run_id` 原本在 `HandsTrace.__init__` 按进程生成一次，
    长驻进程跨天恒定（实测 10-01~10-08 全是 `1d31f717`）。新增 `HandsTrace.begin_run(label)`：
    换新 `run_id` + `run_seq += 1` + 写一条 `run_start`；**不重置 open row**
    （`title`/`events`/`before` 不动，行中学不会被 run 边界打乱）。新增 MCP 工具 `begin_run`
    在 `runtime.trace` 上操作，**不调 `bind`**（那会重置行状态）。`append` / `snapshot` 每条
    都带 `run_seq`。
  - **A3 坐标点击补上下文**：`_target_source` 的 `coord` 分支用 `session.elements` 找与目标
    归一化坐标最近的元素（欧氏距离 ≤ 常量 `NEAR_BOUNDS_RADIUS=80`），补
    `near_text` / `near_class` / `near_bounds` / `near_distance`。`source` 取值语义不变；
    `bounds` 缺失/格式异常安全跳过。
    ⚠️ **修正（QA 复验打回）**：元素 `bounds` 是**设备像素**（`utils/visualization.py:425`），
    而 tap 是**归一化 0-1000**；初版直接拿像素中心和归一化 tap 比距离 → 生产中几乎永不命中、
    A3 形同失效。现在 `_nearest_element` 先用新增的 `_to_normalized()`（复用
    `hands_session.py:92-93` 的 `round(v*1000/max(1,dim))` 写法）把 bounds 中心换算到 0-1000
    再比欧氏距离；`width`/`height` 缺失/为 0/非数值时安全返回 None（不命名也不崩）。
    `NEAR_BOUNDS_RADIUS` 仍是 80（它本来就是 0-1000 空间的量，换算只发生在元素一侧）。
  - **A4 行内/行外标记**：新增 `_in_row_field()`（`{"in_row": bool(runtime.trace.title)}`），
    `_trace_done` 写成功事件时统一补 `in_row`；**`_note_look`（成功 `look`）与 `look_failed`
    也一并补**（QA 复验追加）——`look` 是数量最大的一类 op（某天 805 条里 402 条），不覆盖它
    「多少动作落在行外」的指标就残缺。实测只有 39% 的动作能归到某一行，这个字段让该比例可精确统计。
  - **A6 回程记录**：`press_key` / `manage_app` / `open_link` 各加可选参数 `purpose: str = ""`；
    非空时写进自己的记录，`purpose == "return"` 且成功时**额外** `append` 一条
    `op: "return"`（`via` / `detail` / `pkg`），**不带 `title`**（避免被 `_open_rows_from_log` 误配对）。
  - **A8 `end_row` 记 done / reward**：`end_row(abandon=False, reward="")`；写入 `finish(extra=)`
    的详情加 `"done": judged["learned"]`（`judge_row` 返回的判定键）与 `reward`（非空才加）；
    `abandon=True` 分支也带 `"done": False`。
  - **结果流补 `detail`**：`CatalogService.note()` 新增参数 `detail: str = ""` 并写入 `record`；
    `how` / `result` / `archetype` 取值与归一逻辑**一律不动**（`analyze_archetypes.py` 与
    `cli_study/tests/test_xianyu.py` 依赖它们）。新增 `scripts/note_run.py` 给操作者一个合规写入入口
    （字段/顺序与 `note()` 一致，枚举只告警不阻断），避免继续手写 JSON 丢字段。
  - **安全性依据**：`compress_steps` 只读 `op=="look"` 的 `texts` 与 `op=="click"` 的 `text`
    （见 `artemis/mcp/row_steps.py:101` 与 `:119`）；`settle` / `_open_rows_from_log` 只读
    `begin_row` / `end_row`。新 op 名（`run_start` / `return`）与新键（`run_seq` / `intent` /
    `purpose` / `in_row` / `done` / `reward` / `near_*`）全部落在这些消费点之外。
    `tests/unit/mcp` 无新增失败/错误（基线 224 passed / 1 failed / 1 skipped / 18 errors 保持不变，
    另加新增用例）。
  - ⚠️ **同样要重载 MCP 才生效**。

## 2026-10-04

- 给代码对话单独加了 MCP `artemis-hands-code`。工具和 `artemis-hands` 相同，入口仍是 `mcp_server/hands_entry.py`，用 `ARTEMIS_HANDS_NAME` 把进程名分开。老的 `artemis`、`artemis-hands` 配置不动。Cursor 的 `~/.cursor/mcp.json` 已写入这一条，需要重载 MCP 后才会出现。

## 2026-09-30

- **分工定死**（用户明确要求）：**AI 负责检查 + 优化，不执行脚本**；跑任务由用户自己做。见 `.workbuddy-ai/memory/MEMORY.md` 和 `~/.workbuddy-ai/MEMORY.md`。
- **修掉「截图落在系统临时目录」**（`observation.py`）。查「图片到底存哪」时发现：
  - `traces/images/` **从 9/28 15:06 之后就没新增过** → hands 这条链路**从来没写进项目**。
  - 全部落在 `/var/folders/…/T/artemis/screenshots/`：**1949 张 / 500M**，命名 `screenshot_<epoch>.jpg`。
  - 根因：`observe()` 里 `ctx.data_engine` 为空（hands 链路不建 data_engine）→ 走 temp fallback。
    临时目录三个问题：**系统会清**、**不按内容去重**、**`mobile_inspect_trace` 会指向空**。
  - 改法：fallback 改成写 `get_images_dir()`（= `traces/images`，项目内，认 `ARTEMIS_TRACES_DIR`），
    文件名用 `sha256(bytes)`，跟 data_engine 同一套约定。删掉不再用的 `import time` / `get_temp_dir`。
  - ⚠️ **去重收益实测只有 1%**（抽样 300 张 → 297 种唯一内容）。屏幕一直在变（状态栏时钟、动画、滚动），
    所以**别指望 sha256 省磁盘**。这条修的是「落在项目里 + 不会被系统清 + 命名统一」。
    **真正的问题是没有任何保留策略** —— 500M 只是换个地方继续长。
- **截图改成只回路径，不回像素**（`hands_server.py`）。图片本来就落盘，但 `look` 以前会把 base64
  塞进返回值 → 进会话记录 → 之后每轮重发。
  - `_look_text()` 在元素列表后追加一行 `[screenshot] <绝对路径>`，agent 要看像素自己去开。
  - `_note_look(session, obs)` 签名加 `obs`，jsonl 的 `look` 事件带 `image` 字段（只记路径）。4 处调用点都改了。
  - 像素内联**默认关**，只有 `ARTEMIS_HANDS_LOOK_IMAGE=1`（或 true/yes/on）才开。默认下即使显式传
    `include_image=True` 也只回路径，并多回一条 `[screenshot not inlined] …` 说明，免得误判成看屏失败。
- 把 `hands_server.py:211` 和 `hands_session.py:117` 的 `look` 默认改成 `include_image=False`，
  跟 `action_server.py:98` 对齐。控件列表才是真正驱动点击的信号。
- ⚠️ 只改默认值**不够**：实测 agent 每次都显式传 `include_image: true`（`82a0f71b` 127 次 true = 127 张截图；
  `5fb784ed` 64 次）。模型是在照抄 schema 里的 `"default": true`。**软约束（写进技能）也无效** ——
  技能里本来就写着这条，照样 127 次。所以只有**服务端硬约束**靠得住。
- 纪律：长时间跑任务前先重启 WorkBuddy，别让会话跨天累积；一轮跑完就收尾，不要挂着过夜。

## 2026-09-29

- 主闲鱼「去逛一逛芭芭农场」停留三次仍是去完成，页面要下单，返回会掉进充值游戏。没有下单，清单标成跳过。
- 主闲鱼「去逛一逛淘金币」打开后只有打开淘宝和下载淘宝，没有点，清单标成跳过。
- 双开闲鱼昨晚跑完没有进 `logs/hands/JBCIUOGENV5DAYGU/20260928.jsonl`。按 `logs/JBCIUOGENV5DAYGU/20260928.md` 的双开段补了 37 条 backfill，带 `user` 999，没有编点击坐标。
- 闲鱼币技能改成跑的时候写日志：每一行回到弹层就写 md 和 jsonl，再做下一行。jsonl 第一下点击没有新行就停下来重载，不要整轮跑完再补。`backfill` 只表示当时没写。
- 闲鱼币技能的摇骰子补上棋盘步数：关掉任务弹层后，×N 要摇完，「N步」大于 0 也要点红色「前进获得闲鱼币」直到步数没有。右边「赚更多 / 领」只用来开弹层。昨天双开 129 步没点，是这条没写进去。

## 2026-09-28

- 双开闲鱼（`JBCIUOGENV5DAYGU` user 999，深圳读书匠）看15秒、看视频、观看视频都领完。闲鱼币 69759，棋盘 129 步。弹层滑到底没有领取奖励，关掉后没有骰子次数，没有点中间的前进。记录在 `logs/JBCIUOGENV5DAYGU/20260928.md`。
- 闲鱼币技能写明：`logs/hands/{设备序列号}/{日期}.jsonl` 由 hands 服务追加。进程还是改代码之前拉起的，看屏和点击都不会进这个文件，要先重载 MCP。
- 新手机 `3B15BA0009000000` 的任务弹层是「前进获得闲鱼币」。已领取闪购、蚂蚁庄园、芭芭农场、支付宝农场、搜一搜、闲鱼游戏15秒、淘宝签到和 1 小时步数奖励。头条未安装，没有点下载。记录在 `logs/3B15BA0009000000/20260928.md`。
- 运行日志按机器分开：`logs/hands/{设备序列号}/{日期}.jsonl` 和 `logs/{设备序列号}/{日期}.md`。每条 jsonl 带 `date` 和 `device`。今天这台是 `JBCIUOGENV5DAYGU`。
- 补上 `logs/hands/20260928.jsonl`。当天服务没落盘，按 `logs/20260928.md` 还原每一行的结果，没有编造点击坐标。
- `logs/20260928.md` 改成一行一条：行名、看屏还是跳过、回到列表后的按钮，并补上 12:40 翻完整张列表的收尾。
- 任务弹层里的「领取奖励」都要点，标记跳过的行也要点。跳过只表示不点「去完成」。买到或卖出一件宝贝的领取奖励已点掉。
- 闲鱼币技能补上骰子页规则：一看到骰子页先查 ×N。有次数就摇完再继续别的任务；角标已是「赚」就跳过。点在角标正下方，转盘只关不抽。
- 骰子点在 ×N 角标正下方，不再点屏幕正中。正中会打到棋盘奖品并弹出转盘。转盘只关，不点「开始抽奖」。次数从 ×40 点到角标变成「赚」。
- 闲鱼币仍走 `artemis-hands`。每次点击由服务记入 `logs/hands/当天.jsonl`。`begin_row` / `end_row` 在弹层确认做成后把步骤写入清单；已有步骤的行用一次 `replay_row` 跑完。`settle_run.py` 不过，这一轮不能停。改完需要重载 MCP。
- 闲鱼币任务继续跑：淘金币已领取，支付宝积分那一行从列表消失，骰子 ×40。百度、淘特是下载页，没有下载，仍是去完成。过程在 `logs/20260928.md`。
- 闲鱼币任务继续跑：美团刷视频和淘宝闪购果园都已领取，骰子 ×39。一淘、大众点评仍是去完成。可执行行还在，还没掷骰子。过程在 `logs/20260928.md`。
- 闲鱼币技能补上每轮跑完抽脚本：先把这一轮写入当天日志，再把能固定的路径写成 `claim`、`leave_and_return` 或 `browse_ad`。做不到的标跳过，对不上的仍走 AI。已跳过的行不因日志改回。`match_rows.py` 增加 `--set-script`。
- 闲鱼币页或任务弹层上只要还看得到「签到」按钮就点一次。变成「明日再来」、已勾选或按钮消失后不再点。

## 2026-09-27

- 新增项目技能 `xianyu-coin-tasks`。看到闲鱼币任务弹层时对照 `tasks/闲鱼币任务清单.json`：不能运行的行直接跳过，能固定点击的行走脚本，脚本同一行失败 3 次后改由 AI 接管并修脚本，确认脚本做不到就删掉脚本。
- 任务列表标题本身就是玩游戏（如「去赵云和阿斗玩1关」）直接跳过。视频任务点进去后如果是游戏，按页面提示进入，停留指定时间再回金币任务列表。
- 按 `xianyu-coin-tasks` 跑了一轮：蚂蚁庄园、芭芭农场各停留 15 秒后领取。玩游戏和「买到或卖出一件宝贝」跳过。记录在 `logs/20260928.md`。
- 闲鱼币任务按弹框循环做：没完成就继续，同一条失败 3 次才标不能执行，全部如此后再回骰子页把骰子点完。完整运行记录在 `logs/20260927.md`。
- 新增纯操作 MCP `artemis mcp --type hands`（安装名 `artemis-hands`）。外部智能体自己看截图和控件编号并决定每一步，服务只负责观察和点击，不调用内部模型。`--install` / `--generate-config` 会在原有 agent 服务旁边多写这一条，并装上一份短规则。
- 运行 `47cf8ff9` 的「看15秒视频领奖励」点了「去完成」后停在京东「点击浏览商品 15 秒拿奖励」页。这一页没有「广告」二字，也不是任务列表，按返回离开不了，后面的收录都进不了列表。现在先点倒计时右侧没文字的关闭，再回到任务列表。
- 当前屏幕不是任务列表时不再直接停下。先看页面上的任务名和「任务完成 / 跳过 / 关闭广告」，把这一页做完再回列表；页面认不出时交给模型继续，列表真正打开并且名单里没有剩余条目，才停止。
- 首页任务卡片的操作栏加上「收录」开关。打开后执行任务时，提示词会收进个人任务。
- 顶部导航在 New / Home、Workspace 后面增加「个人收录」。点进去可以改任务提示词、改每一条小任务，并查看每次运行的完整日志。
- 闲鱼币弹窗每一行的按钮就是「去完成」和「领取奖励」。提示词没另写按钮时，浏览、签到、跳过这类小任务也生成脚本。明确写了别的按钮，或写了「没有按钮」，仍按原文。
- 按 2026-09-26 的运行日志重写 `tasks/闲鱼币每日任务.md`：删掉列表里没有、翻找会空转的 6 条，卡住的行改成领取或立刻结束。
- 每做完一条先点掉当前屏幕上的「领取奖励」，避免完成行堆在列表顶部。小任务列表显示当天完成记录，并可以删除某一条。
- 领取奖励和掷骰子穿插进行：每做完一条先领奖励，再掷手头的骰子（最多 6 次），然后才做下一条。限时惊喜只关闭，不玩一局。
- 个人收录改为按屏幕上的任务列表执行：先领取，再掷看得见的骰子，再按行名对照收录。对上的用脚本做完并领取，没有的交给 AI。AI 失败累计 3 次，或日志确认不存在，就在提示词里标成直接跳过。成功且能固定的步骤沉淀成脚本。
- 收录脚本改读无障碍界面树。之前用 UIAutomator 看不到闲鱼网页任务列表，一点「再次执行」就退出，工作区也没有实时步骤。
- 收录脚本改读无障碍界面树。之前用 UIAutomator 看不到闲鱼网页任务列表，一点「再次执行」就退出，工作区也没有实时步骤。
- 脚本小任务超过 60 秒、模型小任务超过 120 秒就跳过，并记下停在哪一步。一级任务没有总超时。
- 「再次执行」进不了任务列表：首页橙色「领」没有文字，旧坐标点偏了，接着按返回离开闲鱼。现在按搜索按钮上方点入口，弹出「得骰子赚闲鱼币」就当作已经进列表。没进列表时进程以失败退出，不再报一条无法写入的 completed。
- 闲鱼币页上的「任务奖励」文字点了没反应。改为点骰子下面的「赚骰子」（上面的「领」），这一下能把任务列表拉出来。
- 整页网页容器 `WVUCWebView` 被当成一条任务，模型做完它就停了，16 条正事都没做、也没标跳过。容器名不再当任务。屏幕上暂时没有「去完成」时，先看收录里还有没有没完成、没标跳过的条目，再让模型决定继续还是停止。误加的第 17 条已从收录里删掉。
- 做完一条会关掉任务列表，回来时点「任务奖励」打不开，于是后面的收录都停了。现在从「赚骰子」回到列表。屏幕上先做已收录的行，陌生行排在后面。模型长日志里出现「不存在」不再把正事标成直接跳过。
- 运行 `20fcf30d` 停在「看15秒视频领奖励」打开的全屏广告页：关闭按钮是图片，界面树里只有「点击广告拿奖励」这类文字，脚本按了几次返回就放弃，后面每次进列表都失败，问了两次模型后停止。那一轮 AI 改脚本也因为模型响应超时没改成。App 脚本升到第 3 版：广告页先等倒计时，再按返回，返回不管用就点角上的关闭；任务行下面必须紧跟「+奖励」那一行，列表叠在棋盘上时，棋盘下的「向往的生活」「疯狂来解压」「36.7万+」等文字不再算成任务。视频任务超过 60 秒时，最后再看一眼有没有「领取奖励」。维护调用的超时从 180 秒放宽到 420 秒。广告页和真实任务列表这两张界面加进回放快照。
- 闲鱼专属的点击规则从核心代码搬到 `tasks/scripts/idlefish.py`，由 AI 按运行日志维护：怎么进列表、骰子在哪、哪些行不是任务、小游戏跳过、怎么回到列表。核心代码只剩界面循环、计时、日志和收录进化。每轮卡住或失败时，运行器把当时的界面存到 `traces/<会话>/dumps/`；跑完后 AI 读脚本、日志和这些界面，交回一份完整的新脚本。新脚本要先过几道检查：语法；导入只能用 time、re、typing 和脚本工具模块；函数不能少；在 `tasks/scripts/fixtures/idlefish/` 的标注界面上回放结果要对。通过就直接生效，每一版都存进数据库；新版本第一次跑就比上一版差，自动退回上一版。手动改脚本文件也会记成新版本。个人收录页新增「App 脚本」一块，可以看每一版的修改原因和差异，也能退回到某一版。
- 复查执行链路后再改几处：界面树里没有「扔骰子寻宝」文字，脚本以前一次都没掷成，现在按 ×N 角标定位骰子并掷到 0；任务列表盖着骰子时不再点骰子位置，免得点到列表里的行；「领」角标消失后仍能按位置点到「赚骰子」；在闲鱼币页不再点右上角，以前会点到「攻略」打开规则页；做完一条按返回回到闲鱼币页就停，改点赚骰子打开列表，不再一路退出闲鱼；要玩一局的小游戏行直接跳过；手机锁屏时直接停下并提示解锁，开跑时让手机插 USB 期间保持亮屏。
- 运行 `e1e19822` 自己停掉：棋盘下面的「闲鱼币兑曝光」被当成任务交给模型，模型按总说明去掷骰子，120 秒一到被掐断，接着误开签到规则页，问了两次「继续」后到达次数上限退出。商店入口不再当任务。骰子按按钮上的 ×N 由脚本掷完。规则页直接返回。

## 2026-09-27

- 闲鱼币完整运行记录写在 `logs/20260927.md`，完成状态写在 `tasks/20260927-status.json`。观看视频领取奖励已从 1/3 做到领取后从列表消失。国补标记为不能执行并跳过。去头条玩一玩已领取。去玩闲鱼游戏15s 三次都进升级页，标为不能执行。骰子从 ×16 掷到 0，转盘只关闭。
- 任务列表里能对上的固定步骤改走界面脚本：进列表、点「领取奖励」、浏览停留、看广告点跳过、返回，都不再调用模型。
- 模型只处理脚本没点成的那几行，而且提示改成这一行自己的短说明，不再把整份任务前言重复送进去。
- 脚本进不了任务奖励列表时，仍用原来的核对名单；核对之后再交回脚本。第二次还进不去，才把剩下的行交给模型。

## 2026-09-25

- 大模型只保留 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`。主模型 `ARTEMIS_MODEL`、备用模型 `ARTEMIS_FALLBACK_MODEL` 覆盖规划、执行、定位、步骤摘要和历史压缩。
- 闲鱼币任务说明补上可复用规律：已是领取就只点一次，滑动类只互动约 20 秒，必须玩一局的直接跳过，骰子只关不掷。
- 增加 `tasks/闲鱼币队列.json`，用一次 `/api/run` 的 `goals` 把清单拆成多场，由队列自动接龙，不用每场手动再输入。
- 启动页识别编号清单，拆成多条后写入现有任务队列，按钮变为「执行任务列表」，由队列逐条执行。
- 执行任务列表时先核对屏幕上的任务名，只把和用户清单重合的条目排进队列。屏幕上没有的行不再逐条去找。
- 工作区顶部增加「任务队列」入口和独立的「停止队列」按钮。它会停掉当前任务并清空后面排队的条目，不再自动开下一条。
- 去掉 `.env` 里用不到的 Gemini、Google、Anthropic、OpenRouter、xAI 配置，以及单独的 `ARTEMIS_STEP_SUMMARIZER_MODEL`。

## 2026-09-24

- 默认模型改为通过 DMXAPI 的 OpenAI 兼容接口调用 `gemini-3.8-flash`（`OPENAI_API_KEY` + `OPENAI_BASE_URL=https://www.dmxapi.cn/v1`）。
- `object_detector`、`hopper`、日志分析不再走 Google 官方客户端；Flash 步骤摘要和历史压缩在配置了兼容接口时也改走同一条通路。
- 补上 `socksio`，让模型请求能走本机 SOCKS 代理。任务 `62b9f4c1` 之前卡在缺这个包，安装后需要重新跑一次。
- 默认模型从 `gemini-3.8-flash` 换成 DMXAPI 上的 `qwen3.8-flash`，定位、摘要和历史压缩一并跟随。
- Flash 步骤摘要模型改为读取 `.env` 的 `ARTEMIS_STEP_SUMMARIZER_MODEL`，不再写死在代码或 `artemis.jsonc` 里。
- 根据会话 `d6cb83c1` 的实际路径，写好可直接粘贴的闲鱼币任务说明 `tasks/闲鱼币每日任务.md`。发布、闯关、重新打开闲鱼这些弯路写进了停机条件。
