# astrbot_plugin_offline_dev

> 离线技能开发框架 —— 把"技能包"拖进数据目录就能移植，安全的异步执行，不阻塞主线程。

## 设计原则

1. **安全**：技能在 `asyncio.wait_for` 隔离运行，超时强制中断；异常全捕获，绝不向上冒泡。
2. **不干扰主线程**：`__init__` 不阻塞，初始化丢到后台 Task；执行受全局信号量限流。
3. **可行性**：技能 = 文件夹（`skill.yaml` + `handler.py`），拖进 `plugin_data/astrbot_plugin_offline_dev/skills/` 即可移植。

## 用法

### 互动模式

```text
/skill list                  # 列出已加载技能
/skill info <name>           # 查看技能详情
/skill run <name> [args...]  # 执行技能
/skill reload                # 重新扫描技能目录（管理员）
/skill path                  # 显示技能根目录绝对路径（管理员）
```

首次启动会自动把内置示例 `echo` / `wordcount` 复制到技能目录。

```text
/skill run echo hello world      → echo: hello world
/skill run wordcount hello world → 字符数: 11 / 单词数: 2 / 累计调用次数: 1
```

### Loop 自动循环模式（v0.2+）

把任意技能挂成定时任务，结果自动推送回挂载时的会话。

```text
/skill loop add <name> <间隔秒> [args...]            # 间隔触发（≥ loop_min_interval_seconds）
/skill loop addcron <name> "<cron>" [args...]        # cron 触发（5 字段：分 时 日 月 周）
/skill loop list                                     # 列出所有 loop
/skill loop info <id>                                # 查看详情
/skill loop pause <id>                               # 暂停（保留记录）
/skill loop resume <id>                              # 恢复
/skill loop tick <id>                                # 立即手动触发一次（管理员，调试用）
/skill loop adopt <id>                               # 领养一个待领养模板（管理员，在目标会话里执行）
/skill loop stop <id>                                # 停止并删除
/skill loop clear                                    # 清空所有 loop（管理员）
```

cron 例子：

```text
/skill loop addcron wordcount "0 9 * * *" hello       # 每天 9:00
/skill loop addcron echo "*/15 * * * *" tick           # 每 15 分钟
```

### Loop 模板（v0.4+）

技能可以在 `skill.yaml` 中预声明 `default_loops:`。插件加载时会自动注册为**待领养**状态（暂停、无 target_session），管理员在目标会话里执行 `/skill loop adopt <id>` 即可绑定并启动。

```yaml
# skill.yaml
name: heartbeat
entrypoint: handler:run
default_loops:
  - id: heartbeat                    # 最终 loop id = tpl_<skill>_heartbeat
    interval_seconds: 60
    args: ["ping"]
    description: 心跳监控
  - id: morning
    cron_expr: "0 9 * * *"
    args: ["status"]
```

模板规则：
- ID 是确定性的（`tpl_<skill_name>_<id_suffix>`），插件重启/`/skill reload` 不会重复注册
- 默认始终是 paused 状态，避免重启自动刷屏
- `/skill loop adopt <id>` 必须在你想接收输出的目标群/私聊里执行
- 普通 `/skill loop stop <id>` 仍可删除模板 loop（下次 reload 会再次自动注册）

例子：每 60 秒在当前群里跑一次 wordcount：

```text
/skill loop add wordcount 60 hello world
→ ✅ 已挂载 loop  id: a1b2c3d4 ...
```

**安全护栏**

- 间隔小于配置 `loop_min_interval_seconds`（默认 5s）会被拒绝
- 全局 loop 数受 `loop_max_count` 限制（默认 10）
- 每次迭代仍走 `SkillRunner`，继承超时/并发限制
- 连续失败 ≥ 3 次自动暂停（断路器，避免失控刷消息）
- 失败默认只记日志，开 `loop_send_failure_message` 才往会话推送
- `terminate()` 时所有 loop 任务统一取消
- 技能 manifest 可声明 `loopable: false` 拒绝被挂 loop
- 技能 manifest 可声明 `max_loop_instances: N` 限制单技能 loop 配额
- `loop_default_jitter_seconds` 给 `add` 自动加抖动，避免多个 loop 共振

**持久化与恢复**

loop 状态写在 `<data_dir>/loops.json`。重启时**默认不自动恢复**（避免突然刷屏），可通过配置 `loop_auto_resume_on_start` 开启自动恢复。手动恢复也可以：直接 `/skill loop resume <id>`。

## 技能包格式

```
<skills_root>/<skill_name>/
├── skill.yaml          # 必需：技能清单
└── handler.py          # 必需：入口模块（文件名由 entrypoint 决定）
```

### `skill.yaml` 字段

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `name` | ✅ | — | 唯一标识，仅允许 `[A-Za-z0-9_-]`，最长 64 字 |
| `entrypoint` | ✅ | — | 形如 `handler:run`，即"模块名:函数名" |
| `command` | ❌ | 同 `name` | 触发指令，会作为 `/skill run <command>` 中的 `<command>` |
| `display_name` | ❌ | `""` | 展示名 |
| `version` | ❌ | `0.0.0` | 版本号 |
| `author` | ❌ | `""` | 作者 |
| `description` | ❌ | `""` | 一句话描述 |
| `timeout_seconds` | ❌ | 配置中 `default_timeout_seconds` | 单次执行超时（正整数） |
| `permission` | ❌ | `user` | `user` 或 `admin` |
| `loopable` | ❌ | `true` | 是否允许此技能被挂 loop |
| `max_loop_instances` | ❌ | `0` | 此技能最多并存的 loop 数（0=不限） |

### `handler.py` 契约

```python
async def run(ctx) -> str:
    """ctx 是 SkillContext，框架注入。"""
    return f"echo: {ctx.joined_args()}"
```

`run` 也可以是同步函数，框架会自动用 `asyncio.to_thread` 包装。

`SkillContext` 提供的字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `event` | `AstrMessageEvent \| None` | 互动模式下为事件对象；**loop 模式下为 None** |
| `astrbot_context` | `Context` | AstrBot 全局上下文（高级用法） |
| `data_dir` | `Path` | 本技能专属的可写持久化目录 |
| `config` | `dict` | `skill.yaml` 的原始字典（含自定义字段） |
| `logger` | `logging.Logger` | 已带前缀的子 logger |
| `args` | `tuple[str, ...]` | 触发指令时附带的参数列表 |
| `target_session` | `str` | 推荐用作输出路由的 session（互动/loop 都有值） |
| `is_loop` | `bool` | 是否处于 loop 模式 |
| `loop_id` | `str` | loop 模式下的 loop id |

辅助方法：`ctx.get_arg(i, default="")`、`ctx.joined_args(sep=" ")`。

> 想兼容 loop 模式的技能：访问 `ctx.event` 前判 `None`；输出路由用 `ctx.target_session` 而不是 `ctx.event.unified_msg_origin`。

## 配置（WebUI）

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `skills_dir_name` | `skills` | 技能根目录名（位于本插件 plugin_data 下） |
| `default_timeout_seconds` | 30 | 技能默认执行超时 |
| `max_concurrent_skills` | 4 | 全局并发上限 |
| `auto_install_examples` | true | 首次启动安装示例技能 |
| `skill_blacklist` | `[]` | 黑名单（按 `manifest.name` 匹配） |
| `enable_auto_reload_command` | true | 启用 `/skill reload` 指令 |
| `loop_min_interval_seconds` | 5 | loop 最小允许间隔 |
| `loop_max_count` | 10 | 全局 loop 数量上限 |
| `loop_auto_resume_on_start` | false | 启动时自动恢复持久化的 loop |
| `loop_send_failure_message` | false | loop 失败时是否往会话发错误提示 |
| `enable_loop_command` | true | 启用 `/skill loop` 指令组 |
| `loop_default_jitter_seconds` | 0 | `loop add` 默认叠加的 ±抖动秒数（必须 < interval） |
| `enable_llm_tools` | false | 把 `run_offline_skill` / `schedule_offline_skill_loop` 暴露为 LLM 工具 |
| `enable_web_ui` | true | 启用 `/api/plug/offline_dev/ui` 仪表盘 |

## 安全模型说明

- 每个技能模块以 `astrbot_plugin_offline_dev._skills.<name>.<module>` 完整名注册到 `sys.modules`，重新加载时会清理同前缀旧模块。
- `entrypoint` 模块路径会被解析为绝对路径并校验位于技能目录内，防止 `../` 越界。
- 技能执行通过 `asyncio.wait_for` 强制超时；同步 handler 走线程池，不会卡住主事件循环。
- 全局并发上限达到后，新请求会被立即拒绝并返回明确提示，而非排队堆积。
- 这不是沙箱：技能仍然以宿主进程权限运行，请只加载你信任的技能包。

## Web 仪表盘（v0.4+）

打开 `enable_web_ui`（默认即开），登录 AstrBot 仪表盘后访问：

```
/api/plug/offline_dev/ui
```

实时显示三个面板：

| 面板 | 内容 |
|------|------|
| 🔁 Loop 进程 | 所有 loop 的状态、调度规则、执行计数、失败计数、最近错误，带 tick/暂停/恢复/删除按钮 |
| 📦 已加载技能 | 技能列表 + 触发指令 + 描述 + 权限 + loopable / 配额 |
| 📜 执行事件 | 最近 200 条执行记录（手动/loop/tick），带耗时与输出预览 |

**实现细节**：

- 单页 HTML 内嵌于 `web_ui.py`，不依赖任何外部 JS/CSS
- 2 秒轮询 `/state` 与 `/events`（带 `since` 时间戳，只取增量）
- 鉴权直接复用 AstrBot 仪表盘 JWT，没有第二套登录
- 写操作（pause/resume/stop/tick）走 POST `/loop_action`，永远不让 GET 改状态
- 内存事件缓冲容量 200 条，FIFO 丢最旧；进程重启清空（不持久化）

**API 路由清单**（都挂在 `/api/plug/offline_dev/`）：

| 路径 | 方法 | 作用 |
|------|------|------|
| `/ui` | GET | 仪表盘 HTML |
| `/state` | GET | 技能 + loop 状态快照 |
| `/events?since=<ts>&limit=<n>` | GET | 执行事件增量 |
| `/loop_action` | POST | `{action:"pause/resume/stop/tick", loop_id:"..."}` |

## LLM 工具（可选，默认关闭）

打开 `enable_llm_tools` 后，AI 可以直接在对话里使用：

| 工具名 | 作用 |
|--------|------|
| `run_offline_skill(name, args)` | 执行一个本地技能 |
| `schedule_offline_skill_loop(name, interval_seconds, args)` | 把技能挂成定时任务 |

两个工具都共用 `/skill run` / `/skill loop add` 同一套校验：admin 权限 / `loopable` / `max_loop_instances` / 全局并发与超时。AI 拿不到任何"绕过"路径。

## 移植已有 Claude Code / Cursor / 其他 skills

最小适配只需：

1. 把代码放进一个文件夹。
2. 提供一个 `async def run(ctx) -> str` 入口（或同步函数也行）。
3. 写一份 `skill.yaml`。

把整个文件夹丢进 `plugin_data/astrbot_plugin_offline_dev/skills/`，`/skill reload` 即生效。
