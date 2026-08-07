// MathExplorer autorun extension — metronome + cheatsheet + /config + /mx + /autorun.
//
// The self-propelling chain is: worker delivery wakes the orchestrator, which
// revives/dispatches the next worker. This extension is only the BACKSTOP: when
// the chain is fully stopped (session idle, no running workers, autorun on),
// the 60s metronome nudges the orchestrator with CONTINUE_PROMPT. It never
// fires while anything is running, and data/STOP is the master switch.
//
// Quota gating is optional and fully configured by config.toml [quota]:
//   cmd       — executable / script / script-with-args / http(s) URL
//   used_re   — regex with ONE capture group for the used ratio (0..1)
//   threshold — skip nudging when used >= threshold (default 0.95)
// If cmd/used_re are missing, the command fails, or the regex does not match,
// the gate is OPEN (no gating) — quota querying is an optimization, never a dependency.

import { existsSync, readFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";

const STATE_TYPE = "mathx.autorun.state";
const TICK_MS = 60_000;
const CONTINUE_PROMPT =
  "检查 MathExplorer 循环：读 data/registry.json 与 results/ 下各 run.json；按项目根 AGENTS.md 的调度纪律推进（复活/分派/补货），全部产物落盘。";

const CHEATSHEET_LINES = [
  "MathExplorer: /mx 速查表 · /ask <命题> 猜想分诊 · /solve [id] 攻关 · /hunt [field] [quota] 补货 · /status 简报",
  "/stop 紧急制动 · /autorun on|off|status 无人值守 · data/seeds/ 丢.md录入 · data/STOP=总闸",
  "内置: /model 切模型 · /pause 暂停 · /collab 远程介入 · /reload-plugins 刷新命令 · Esc 中断当前轮",
];

// ---- minimal omp extension API types (local mirror of the documented surface) ----

interface ExecResult {
  ok: boolean;
  text: string;
}

interface UiContext {
  notify(message: string, level: "info" | "warning" | "error"): void;
  setWidget(lines: string[], placement: "aboveEditor" | "belowEditor"): void;
  input(prompt: string, defaultValue?: string): Promise<string | undefined>;
  confirm(message: string): Promise<boolean>;
}

interface SessionEntry {
  type?: string;
  customType?: string;
  data?: unknown;
}

interface ExtensionContext {
  cwd: string;
  hasUI: boolean;
  ui: UiContext;
  isIdle(): boolean;
  getAsyncJobSnapshot(): unknown;
  setInterval(fn: () => void, ms: number): unknown;
  sessionManager: { getBranch(): SessionEntry[] };
}

interface ExtensionApi {
  on(event: "session_start", handler: (event: unknown, ctx: ExtensionContext) => void | Promise<void>): void;
  registerCommand(
    name: string,
    def: { description: string; handler: (args: string, ctx: ExtensionContext) => void | Promise<void> },
  ): void;
  appendEntry(customType: string, data: unknown): void;
  sendUserMessage(content: string): void;
  exec(cmd: string): Promise<unknown>;
}

interface QuotaConfig {
  cmd?: string;
  usedRe?: string;
  threshold?: number;
}

// ---- helpers ----

/** Build a RegExp from a pattern that may carry inline (?flags) — JS RegExp lacks inline-flag syntax. */
function buildRegex(src: string): RegExp | null {
  const inline = /^\(\?([dgimsuvy]*)\)/.exec(src);
  try {
    return inline ? new RegExp(src.slice(inline[0].length), inline[1]) : new RegExp(src);
  } catch {
    return null;
  }
}

/** Section-aware flat-key extraction of [quota] from config.toml (no full TOML parse needed). */
function readQuota(configText: string): QuotaConfig {
  const out: QuotaConfig = {};
  let inQuota = false;
  for (const line of configText.split(/\r?\n/)) {
    const t = line.trim();
    const sec = /^\[([^\]]+)\]$/.exec(t);
    if (sec) {
      inQuota = sec[1] === "quota";
      continue;
    }
    if (!inQuota || !t || t.startsWith("#")) continue;
    const kv = /^([A-Za-z_]+)\s*=\s*(.+)$/.exec(t);
    if (!kv) continue;
    const raw = kv[2].trim();
    const unquoted =
      raw.startsWith('"') && raw.endsWith('"') && raw.length >= 2
        ? raw.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, "\\")
        : raw;
    if (kv[1] === "cmd") out.cmd = unquoted;
    else if (kv[1] === "used_re") out.usedRe = unquoted;
    else if (kv[1] === "threshold") {
      const n = Number.parseFloat(raw);
      if (Number.isFinite(n)) out.threshold = n;
    }
  }
  return out;
}

/** Normalize whatever pi.exec returns into { ok, text }. */
function normalizeExec(r: unknown): ExecResult {
  if (typeof r === "string") return { ok: true, text: r };
  if (r && typeof r === "object") {
    const text =
      "stdout" in r && typeof r.stdout === "string"
        ? r.stdout
        : "output" in r && typeof r.output === "string"
          ? r.output
          : "text" in r && typeof r.text === "string"
            ? r.text
            : JSON.stringify(r);
    const code =
      "code" in r && typeof r.code === "number"
        ? r.code
        : "exitCode" in r && typeof r.exitCode === "number"
          ? r.exitCode
          : 0;
    return { ok: code === 0, text };
  }
  return { ok: false, text: String(r) };
}

function jobIsActive(j: unknown): boolean {
  if (!j || typeof j !== "object") return false;
  const s =
    "status" in j && typeof j.status === "string"
      ? j.status
      : "state" in j && typeof j.state === "string"
        ? j.state
        : "";
  return s === "running" || s === "pending";
}

/** Best-effort "is any async job running" check over the snapshot's unknown shape. */
function hasRunningJob(snapshot: unknown): boolean {
  if (!snapshot) return false;
  const jobs: unknown = Array.isArray(snapshot)
    ? snapshot
    : typeof snapshot === "object" && "jobs" in snapshot
      ? snapshot.jobs
      : typeof snapshot === "object" && "running" in snapshot
        ? snapshot.running
        : [];
  if (!Array.isArray(jobs)) return true; // unknown shape: assume busy, stay quiet
  return jobs.some(jobIsActive);
}

export default function mathxAutorun(pi: ExtensionApi) {
  let autorunOn = false;
  let quotaSkips = 0;

  async function quotaGateOpen(ctx: ExtensionContext): Promise<boolean> {
    let quota: QuotaConfig;
    try {
      quota = readQuota(readFileSync(join(ctx.cwd, "config.toml"), "utf-8"));
    } catch {
      return true; // unreadable config: never gate
    }
    if (!quota.cmd || !quota.usedRe) return true; // not configured: never gate
    let text: string;
    try {
      if (/^https?:\/\//.test(quota.cmd)) {
        const resp = await fetch(quota.cmd, { signal: AbortSignal.timeout(10_000) });
        text = await resp.text();
      } else {
        const r = normalizeExec(await pi.exec(quota.cmd));
        if (!r.ok) return true; // command failed: never gate
        text = r.text;
      }
    } catch {
      return true; // exec/fetch failed: never gate
    }
    const re = buildRegex(quota.usedRe);
    if (!re) return true; // bad regex: never gate
    const m = re.exec(text);
    if (!m || m[1] === undefined) return true; // no capture: never gate
    const used = Number.parseFloat(m[1]);
    if (!Number.isFinite(used)) return true;
    const threshold = quota.threshold ?? 0.95;
    if (used >= threshold) {
      quotaSkips += 1;
      if (quotaSkips % 10 === 1) {
        try {
          ctx.ui.notify(
            `MathExplorer: 配额已用 ${(used * 100).toFixed(1)}% ≥ 阈值 ${(threshold * 100).toFixed(0)}%，暂停自动推进（第 ${quotaSkips} 次跳过）`,
            "warning",
          );
        } catch { /* UI optional */ }
      }
      return false; // gate CLOSED: skip this nudge
    }
    return true;
  }

  async function tick(ctx: ExtensionContext): Promise<void> {
    if (!autorunOn) return;
    try {
      if (!ctx.isIdle()) return;
    } catch {
      return;
    }
    try {
      if (hasRunningJob(ctx.getAsyncJobSnapshot())) return;
    } catch {
      /* unknown snapshot: fall through cautiously */
    }
    if (existsSync(join(ctx.cwd, "data", "STOP"))) return;
    if (!(await quotaGateOpen(ctx))) return;
    pi.sendUserMessage(CONTINUE_PROMPT);
  }

  pi.on("session_start", async (_e: unknown, ctx: ExtensionContext) => {
    // Replay persisted autorun state so a resumed session keeps its loop setting.
    try {
      for (const entry of ctx.sessionManager.getBranch()) {
        if (
          entry?.type === "custom" &&
          entry?.customType === STATE_TYPE &&
          entry.data &&
          typeof entry.data === "object" &&
          "on" in entry.data
        ) {
          autorunOn = Boolean(entry.data.on);
        }
      }
    } catch { /* state replay is best-effort */ }

    if (ctx.hasUI) {
      try {
        ctx.ui.setWidget(CHEATSHEET_LINES, "belowEditor");
      } catch {
        try {
          ctx.ui.notify(CHEATSHEET_LINES[0], "info");
        } catch { /* UI optional */ }
      }
    }

    ctx.setInterval(() => {
      tick(ctx).catch(() => { /* contained by ctx.setInterval already; belt and braces */ });
    }, TICK_MS);
  });

  pi.registerCommand("mx", {
    description: "MathExplorer 速查表（重新显示在编辑器下方）",
    handler: (_args: string, ctx: ExtensionContext) => {
      try {
        ctx.ui.setWidget(CHEATSHEET_LINES, "belowEditor");
      } catch { /* fall through to notify */ }
      ctx.ui.notify("速查表已显示在编辑器下方", "info");
    },
  });

  pi.registerCommand("autorun", {
    description: "MathExplorer 无人值守循环：/autorun on|off|status",
    handler: (args: string, ctx: ExtensionContext) => {
      const sub = (args || "").trim() || "status";
      if (sub === "on") {
        const stopPath = join(ctx.cwd, "data", "STOP");
        try {
          if (existsSync(stopPath)) unlinkSync(stopPath);
        } catch { /* best-effort */ }
        autorunOn = true;
        pi.appendEntry(STATE_TYPE, { on: true });
        ctx.ui.notify("MathExplorer autorun: ON（metronome 每 60s 兜底推进）", "info");
        try {
          if (ctx.isIdle()) pi.sendUserMessage(CONTINUE_PROMPT);
        } catch { /* busy is fine; the chain will move */ }
      } else if (sub === "off") {
        autorunOn = false;
        pi.appendEntry(STATE_TYPE, { on: false });
        ctx.ui.notify("MathExplorer autorun: OFF", "info");
      } else {
        ctx.ui.notify(`MathExplorer autorun: ${autorunOn ? "ON" : "OFF"}`, "info");
      }
    },
  });

  pi.registerCommand("config", {
    description: "config.toml 管理：/config show|setup|set <path> <value>|test",
    handler: async (args: string, ctx: ExtensionContext) => {
      const parts = (args || "").trim().split(/\s+/).filter(Boolean);
      const sub = parts[0] || "show";

      if (sub === "show") {
        const r = normalizeExec(await pi.exec("uv run python -m mathx.config show --masked"));
        ctx.ui.notify(r.text.trim() || "(empty config output)", "info");
        return;
      }

      if (sub === "set") {
        const path = parts[1];
        const value = parts.slice(2).join(" ");
        if (!path || !value) {
          ctx.ui.notify("用法: /config set <dot.path> <value>", "warning");
          return;
        }
        const r = normalizeExec(
          await pi.exec(`uv run python -m mathx.config set ${JSON.stringify(path)} ${JSON.stringify(value)}`),
        );
        const shown = path.endsWith(".keys") ? "<keys updated, masked>" : value;
        ctx.ui.notify(r.ok ? `已更新 ${path} = ${shown}` : `配置失败: ${r.text.trim()}`, r.ok ? "info" : "error");
        return;
      }

      if (sub === "test") {
        const r = normalizeExec(await pi.exec("uv run python -m mathx.fleet --smoke"));
        ctx.ui.notify(
          r.ok ? `fleet 冒烟通过: ${r.text.trim().slice(0, 200)}` : `fleet 冒烟失败: ${r.text.trim().slice(0, 300)}`,
          r.ok ? "info" : "error",
        );
        return;
      }

      if (sub === "setup") {
        if (!ctx.hasUI) {
          ctx.ui.notify(
            "无交互界面：请手工编辑仓库根 config.toml —— [providers.<name>] api + keys，[roles] default/prover/jury/judge = \"<name>/<model>\"，active_provider = \"<name>\"。",
            "warning",
          );
          return;
        }
        const provider = (await ctx.ui.input("Provider 名称", "stepfun")) || "stepfun";
        const api = (await ctx.ui.input("API 端点（OpenAI 兼容 base URL）", "https://api.stepfun.com/step_plan/v1")) || "";
        const model = (await ctx.ui.input("默认模型", "step-3.7-flash")) || "step-3.7-flash";
        const keys = (await ctx.ui.input("API keys（逗号分隔，可多个；本地无鉴权端点填 EMPTY）", "")) || "";
        if (!api || !keys) {
          ctx.ui.notify("setup 取消：api 与 keys 必填", "warning");
          return;
        }
        const ok = await ctx.ui.confirm(
          `写入 config.toml：provider=${provider}, model=${model}, keys ${keys.split(",").length} 个？`,
        );
        if (!ok) {
          ctx.ui.notify("setup 已取消", "info");
          return;
        }
        await pi.exec(`uv run python -m mathx.config set providers.${provider}.api ${JSON.stringify(api)}`);
        await pi.exec(`uv run python -m mathx.config set providers.${provider}.keys ${JSON.stringify(keys)}`);
        await pi.exec(`uv run python -m mathx.config set active_provider ${JSON.stringify(provider)}`);
        for (const role of ["default", "prover", "jury", "judge"]) {
          const check = normalizeExec(await pi.exec(`uv run python -m mathx.config get roles.${role}`));
          if (!check.ok) {
            await pi.exec(`uv run python -m mathx.config set roles.${role} ${JSON.stringify(`${provider}/${model}`)}`);
          }
        }
        ctx.ui.notify(`config.toml 已更新（provider=${provider}）。用 /config test 冒烟。`, "info");
        return;
      }

      ctx.ui.notify("用法: /config show|setup|set <path> <value>|test", "warning");
    },
  });
}
