"""Web UI / API for astrbot_plugin_offline_dev.

设计：
- 通过 AstrBot 的 register_web_api 注册一组路由，最终落到 /api/plug/offline_dev/*。
- 鉴权复用 AstrBot 仪表盘 JWT，无需自管登录。
- 单页 HTML 内嵌（无外部静态资源依赖），2s 轮询拿状态 → 实时显示自动化进程。
- 所有写操作（pause/resume/tick/delete/adopt）走 POST/DELETE，永远不让 GET 改状态。
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from quart import request
from astrbot.dashboard.server import Response

from .core import LoopSpec, RegisteredSkill, SkillRegistry

if TYPE_CHECKING:
    from astrbot.api.star import Context
    from .core import LoopScheduler

logger = logging.getLogger("astrbot_plugin_offline_dev.web_ui")

# 路由前缀；最终对外为 /api/plug/offline_dev/<...>
_ROUTE_PREFIX = "/offline_dev"

# 内存事件环形缓冲容量
EVENT_BUFFER_CAPACITY = 200


@dataclass(frozen=True)
class ExecutionEvent:
    """技能/loop 执行事件，记录到环形缓冲供 UI 实时展示。"""

    ts: float                # epoch 秒
    kind: str                # 'manual' | 'loop' | 'tick' | 'template'
    skill_name: str
    loop_id: str             # loop/tick 时给出，否则空串
    ok: bool
    duration_ms: int
    output_preview: str      # 前 120 字符
    error: str

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "kind": self.kind,
            "skill_name": self.skill_name,
            "loop_id": self.loop_id,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "output_preview": self.output_preview,
            "error": self.error,
        }


class EventBuffer:
    """轻量环形缓冲，FIFO 丢最旧。"""

    def __init__(self, capacity: int = EVENT_BUFFER_CAPACITY) -> None:
        self._buf: deque[ExecutionEvent] = deque(maxlen=capacity)

    def push(self, ev: ExecutionEvent) -> None:
        self._buf.append(ev)

    def snapshot(self, since_ts: float = 0.0, limit: int = 100) -> list[dict]:
        # 反向取最近的，再过滤 since_ts
        out: list[dict] = []
        for ev in reversed(self._buf):
            if ev.ts <= since_ts:
                break
            out.append(ev.to_dict())
            if len(out) >= limit:
                break
        return out


class OfflineDevWebUI:
    """注册 6 个路由，提供仪表盘 + JSON API。"""

    def __init__(
        self,
        *,
        astrbot_context: "Context",
        registry: SkillRegistry,
        scheduler: "LoopScheduler",
        events: EventBuffer,
        on_pause: Callable[[str], Awaitable[bool]],
        on_resume: Callable[[str], Awaitable[bool]],
        on_stop: Callable[[str], Awaitable[bool]],
        on_tick: Callable[[str], Awaitable[tuple[bool, str, str]]],
    ) -> None:
        self._ctx = astrbot_context
        self._registry = registry
        self._scheduler = scheduler
        self._events = events
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_stop = on_stop
        self._on_tick = on_tick

    def register(self) -> None:
        """把所有路由登记到 AstrBot Web 框架。"""
        routes = [
            (f"{_ROUTE_PREFIX}/ui", self.serve_ui, ["GET"], "Offline-Dev 仪表盘 HTML"),
            (f"{_ROUTE_PREFIX}/state", self.api_state, ["GET"], "技能 + loop 状态快照"),
            (f"{_ROUTE_PREFIX}/events", self.api_events, ["GET"], "执行事件流（轮询）"),
            (f"{_ROUTE_PREFIX}/loop_action", self.api_loop_action, ["POST"], "loop 控制：pause/resume/stop/tick"),
        ]
        for route, handler, methods, desc in routes:
            self._ctx.register_web_api(route, handler, methods, desc)
        logger.info(
            "[offline_dev] 已注册 %d 个 web 路由，UI 入口: /api/plug%s/ui",
            len(routes),
            _ROUTE_PREFIX,
        )

    # ───────────────────────────── 视图 ─────────────────────────────

    async def serve_ui(self) -> Any:
        """返回单页仪表盘 HTML。"""
        return _DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

    # ───────────────────────────── API ─────────────────────────────

    async def api_state(self) -> dict:
        skills = [
            {
                "name": s.manifest.name,
                "display_name": s.manifest.display_name,
                "version": s.manifest.version,
                "description": s.manifest.description,
                "trigger": s.manifest.trigger,
                "permission": s.manifest.permission,
                "loopable": s.manifest.loopable,
                "max_loop_instances": s.manifest.max_loop_instances,
            }
            for s in self._registry
        ]
        loops = [_serialize_loop(spec) for spec in self._scheduler.list_specs()]
        return Response().ok(
            data={
                "skills": skills,
                "loops": loops,
                "server_time": time.time(),
            }
        ).__dict__

    async def api_events(self) -> dict:
        try:
            since = float(request.args.get("since", "0"))
        except ValueError:
            since = 0.0
        try:
            limit = max(1, min(500, int(request.args.get("limit", "100"))))
        except ValueError:
            limit = 100
        events = self._events.snapshot(since_ts=since, limit=limit)
        return Response().ok(
            data={"events": events, "server_time": time.time()}
        ).__dict__

    async def api_loop_action(self) -> dict:
        try:
            payload = await request.get_json()
        except Exception:  # noqa: BLE001
            payload = None
        if not isinstance(payload, dict):
            return Response().error("非法 JSON 请求体").__dict__
        action = str(payload.get("action", "")).strip()
        loop_id = str(payload.get("loop_id", "")).strip()
        if not loop_id:
            return Response().error("缺少 loop_id").__dict__

        if action == "pause":
            ok = await self._on_pause(loop_id)
            return Response().ok(data={"ok": ok}).__dict__ if ok else Response().error("loop 不存在或未在运行").__dict__
        if action == "resume":
            ok = await self._on_resume(loop_id)
            return Response().ok(data={"ok": ok}).__dict__ if ok else Response().error("未找到 loop").__dict__
        if action == "stop":
            ok = await self._on_stop(loop_id)
            return Response().ok(data={"ok": ok}).__dict__ if ok else Response().error("未找到 loop").__dict__
        if action == "tick":
            ok, output, err = await self._on_tick(loop_id)
            if err == "not found":
                return Response().error("未找到 loop").__dict__
            return Response().ok(
                data={"ok": ok, "output": output, "error": err}
            ).__dict__
        return Response().error(f"未知 action: {action}").__dict__


# ──────────────────────────────────────────────────────────────────
# 序列化 helper
# ──────────────────────────────────────────────────────────────────


def _serialize_loop(s: LoopSpec) -> dict:
    return {
        "id": s.id,
        "skill_name": s.skill_name,
        "schedule": (
            f"cron {s.cron_expr}"
            if s.is_cron
            else f"every {s.interval_seconds}s"
            + (f" ±{s.jitter_seconds}s" if s.jitter_seconds else "")
        ),
        "interval_seconds": s.interval_seconds,
        "cron_expr": s.cron_expr,
        "jitter_seconds": s.jitter_seconds,
        "args": list(s.args),
        "target_session": s.target_session,
        "enabled": s.enabled,
        "iterations_done": s.iterations_done,
        "max_iterations": s.max_iterations,
        "failure_count": s.failure_count,
        "last_error": s.last_error,
        "last_run_at": s.last_run_at,
        "paused_reason": s.paused_reason,
        "is_template": s.id.startswith("tpl_"),
        "is_adopted": bool(s.target_session),
    }


# ──────────────────────────────────────────────────────────────────
# 内嵌仪表盘 HTML（单文件、无外链）
# ──────────────────────────────────────────────────────────────────


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Offline-Dev 仪表盘</title>
<style>
  :root {
    --bg: #0e1116;
    --panel: #161b22;
    --panel-2: #1f242c;
    --border: #2a313c;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --ok: #3fb950;
    --warn: #d29922;
    --err: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    font: 14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", "PingFang SC", sans-serif;
    background: var(--bg); color: var(--text);
  }
  h1 { margin: 0 0 4px; font-size: 22px; letter-spacing: .2px; }
  .sub { color: var(--muted); margin-bottom: 24px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 20px; }
  @media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; overflow: hidden;
  }
  .panel header {
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: baseline;
    background: var(--panel-2);
  }
  .panel header h2 { margin: 0; font-size: 14px; font-weight: 600; }
  .panel header .meta { color: var(--muted); font-size: 12px; }
  .panel .body { padding: 0; max-height: 540px; overflow: auto; }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    padding: 10px 14px; text-align: left;
    border-bottom: 1px solid var(--border);
    font-size: 13px; vertical-align: top;
  }
  th { color: var(--muted); font-weight: 500; background: var(--panel-2); position: sticky; top: 0; }
  tr:last-child td { border-bottom: none; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 500; }
  .pill.run { background: rgba(63,185,80,.15); color: var(--ok); }
  .pill.pause { background: rgba(210,153,34,.18); color: var(--warn); }
  .pill.tpl { background: rgba(88,166,255,.15); color: var(--accent); }
  .pill.fail { background: rgba(248,81,73,.18); color: var(--err); }
  .btn {
    background: transparent; color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer;
    margin-right: 4px;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn.danger:hover { border-color: var(--err); color: var(--err); }
  .mono { font-family: ui-monospace, "JetBrains Mono", monospace; font-size: 12px; color: var(--muted); }
  .empty { padding: 24px; text-align: center; color: var(--muted); }
  .err-row { color: var(--err); }
  .ok-row { color: var(--ok); }
  .topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .toggle { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; }
  .toggle input { accent-color: var(--accent); }
  .ts { color: var(--muted); font-size: 11px; font-family: ui-monospace, monospace; }
  .skill-tag { font-family: ui-monospace, monospace; font-size: 12px; color: var(--accent); }
</style>
</head>
<body>

<h1>Offline-Dev 仪表盘</h1>
<div class="sub">实时显示已加载技能与 loop 自动化进程。每 <span id="poll-interval">2</span>s 轮询一次。</div>

<div class="topbar">
  <div class="mono" id="server-status">连接中…</div>
  <label class="toggle">
    <input type="checkbox" id="auto-poll" checked>
    自动刷新
  </label>
</div>

<div class="grid">
  <div class="panel">
    <header>
      <h2>🔁 Loop 进程</h2>
      <span class="meta" id="loops-meta">—</span>
    </header>
    <div class="body" id="loops-body">
      <div class="empty">加载中…</div>
    </div>
  </div>

  <div class="panel">
    <header>
      <h2>📦 已加载技能</h2>
      <span class="meta" id="skills-meta">—</span>
    </header>
    <div class="body" id="skills-body">
      <div class="empty">加载中…</div>
    </div>
  </div>
</div>

<div class="panel" style="margin-top: 20px;">
  <header>
    <h2>📜 执行事件（最新在上）</h2>
    <span class="meta" id="events-meta">—</span>
  </header>
  <div class="body" id="events-body">
    <div class="empty">尚无事件</div>
  </div>
</div>

<script>
const API = "/api/plug/offline_dev";
const POLL_MS = 2000;

const $ = (sel) => document.querySelector(sel);
let lastEventTs = 0;
let allEvents = [];

async function jget(path) {
  const r = await fetch(API + path, { credentials: "include" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return await r.json();
}

async function jpost(path, body) {
  const r = await fetch(API + path, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await r.json();
}

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function renderLoops(loops) {
  $("#loops-meta").textContent = `${loops.length} 个`;
  if (!loops.length) {
    $("#loops-body").innerHTML = '<div class="empty">尚无 loop。在群里 /skill loop add &lt;name&gt; &lt;interval&gt; 创建。</div>';
    return;
  }
  const rows = loops.map(L => {
    let badge;
    if (L.is_template && !L.is_adopted) badge = '<span class="pill tpl">待领养</span>';
    else if (L.enabled) badge = '<span class="pill run">running</span>';
    else if (L.failure_count >= 3) badge = '<span class="pill fail">熔断</span>';
    else badge = '<span class="pill pause">paused</span>';

    const args = L.args.length ? L.args.join(" ") : "<span class='mono'>(无)</span>";
    const target = L.target_session || "<span class='mono'>(待绑定)</span>";
    const lastErr = L.last_error ? `<div class="err-row mono" style="margin-top:4px">err: ${escapeHtml(L.last_error)}</div>` : "";

    let actions = `
      <button class="btn" onclick="act('${L.id}','tick')">⚡tick</button>
      ${L.enabled
        ? `<button class="btn" onclick="act('${L.id}','pause')">⏸暂停</button>`
        : `<button class="btn" onclick="act('${L.id}','resume')">▶恢复</button>`}
      <button class="btn danger" onclick="confirmStop('${L.id}')">🗑删除</button>
    `;
    if (L.is_template && !L.is_adopted) {
      actions = '<span class="mono">需在群里 /skill loop adopt ' + L.id + '</span>';
    }

    return `
      <tr>
        <td>
          <div class="mono">${escapeHtml(L.id)}</div>
          <div style="margin-top:4px">${badge}</div>
        </td>
        <td>
          <div class="skill-tag">/${escapeHtml(L.skill_name)}</div>
          <div class="mono">${escapeHtml(L.schedule)}</div>
          <div class="mono">args: ${args}</div>
        </td>
        <td>
          <div class="mono">target: ${target}</div>
          <div class="mono">iter: ${L.iterations_done}${L.max_iterations ? "/" + L.max_iterations : ""}  fail: ${L.failure_count}</div>
          <div class="mono">last: ${fmtTs(L.last_run_at)}</div>
          ${lastErr}
        </td>
        <td>${actions}</td>
      </tr>`;
  }).join("");
  $("#loops-body").innerHTML = `
    <table>
      <thead><tr><th style="width:140px">ID</th><th>调度</th><th>状态</th><th style="width:240px">操作</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderSkills(skills) {
  $("#skills-meta").textContent = `${skills.length} 个`;
  if (!skills.length) {
    $("#skills-body").innerHTML = '<div class="empty">未加载任何技能</div>';
    return;
  }
  const rows = skills.map(s => `
    <tr>
      <td>
        <div class="skill-tag">/${escapeHtml(s.trigger)}</div>
        <div class="mono">${escapeHtml(s.name)} v${escapeHtml(s.version)}</div>
      </td>
      <td>${escapeHtml(s.display_name || s.name)}</td>
      <td class="mono">${escapeHtml(s.description || "(无描述)")}</td>
      <td class="mono">
        ${s.permission === "admin" ? "🔒 admin" : "user"}
        ${s.loopable ? "" : " · 🚫loop"}
        ${s.max_loop_instances ? ` · cap=${s.max_loop_instances}` : ""}
      </td>
    </tr>`).join("");
  $("#skills-body").innerHTML = `
    <table>
      <thead><tr><th style="width:200px">触发</th><th>展示名</th><th>描述</th><th style="width:140px">属性</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderEvents() {
  $("#events-meta").textContent = `${allEvents.length} 条`;
  if (!allEvents.length) {
    $("#events-body").innerHTML = '<div class="empty">尚无事件</div>';
    return;
  }
  const rows = allEvents.slice(0, 100).map(e => {
    const cls = e.ok ? "ok-row" : "err-row";
    const status = e.ok ? "✓" : "✗";
    return `
      <tr>
        <td class="ts">${fmtTs(e.ts)}</td>
        <td><span class="${cls}">${status}</span> <span class="mono">${escapeHtml(e.kind)}</span></td>
        <td><span class="skill-tag">/${escapeHtml(e.skill_name)}</span>${e.loop_id ? ' <span class="mono">[' + escapeHtml(e.loop_id) + ']</span>' : ""}</td>
        <td class="mono">${e.duration_ms}ms</td>
        <td class="mono">${escapeHtml(e.ok ? (e.output_preview || "(空)") : e.error)}</td>
      </tr>`;
  }).join("");
  $("#events-body").innerHTML = `
    <table>
      <thead><tr><th style="width:80px">时间</th><th style="width:90px">类型</th><th style="width:200px">技能/loop</th><th style="width:80px">耗时</th><th>结果/错误</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function refresh() {
  try {
    const sresp = await jget("/state");
    if (sresp.status === "ok") {
      renderLoops(sresp.data.loops || []);
      renderSkills(sresp.data.skills || []);
      $("#server-status").textContent = "已连接 · server_time=" + fmtTs(sresp.data.server_time);
      $("#server-status").style.color = "var(--ok)";
    } else {
      $("#server-status").textContent = "状态请求失败: " + (sresp.message || "");
      $("#server-status").style.color = "var(--err)";
    }

    const eresp = await jget(`/events?since=${lastEventTs}&limit=100`);
    if (eresp.status === "ok") {
      const fresh = eresp.data.events || [];
      if (fresh.length) {
        allEvents = fresh.concat(allEvents).slice(0, 200);
        lastEventTs = fresh[0].ts;
        renderEvents();
      } else if (!allEvents.length) {
        renderEvents();
      }
    }
  } catch (e) {
    $("#server-status").textContent = "连接失败: " + e.message;
    $("#server-status").style.color = "var(--err)";
  }
}

async function act(loopId, action) {
  const r = await jpost("/loop_action", { loop_id: loopId, action: action });
  if (r.status !== "ok") alert("操作失败: " + (r.message || ""));
  if (action === "tick" && r.status === "ok" && r.data && r.data.output) {
    alert("tick 输出: " + r.data.output);
  }
  refresh();
}

function confirmStop(loopId) {
  if (confirm("确定删除 loop " + loopId + "？")) act(loopId, "stop");
}

window.act = act;
window.confirmStop = confirmStop;

(async function init() {
  await refresh();
  setInterval(() => {
    if ($("#auto-poll").checked) refresh();
  }, POLL_MS);
})();
</script>
</body>
</html>
"""
