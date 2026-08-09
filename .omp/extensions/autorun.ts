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

import { existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { spawn, spawnSync } from "node:child_process";

const STATE_TYPE = "mathx.autorun.state";
const CHAIN_STATE_TYPE = "mathx.chain.state";
const TICK_MS = 60_000;
const CONTINUE_PROMPT =
  "检查 MathExplorer 循环：读 data/registry.json 与 results/ 下各 run.json；按项目根 AGENTS.md 的调度纪律推进（复活/分派/补货），全部产物落盘。";

const CHEATSHEET_LINES = [
  "MathExplorer: /mx 速查表 · /ask <命题> 猜想分诊 · /solve <id前缀> 开始/恢复攻关 · /postpone <id> [原因] 暂停 · /current 活跃概况 · /hunt [field] [quota] 补货 · /brief 简报 · /show <id前缀> 看题 · /solution <id前缀> 看题解 · 菜单浏览过滤",
  "/stop 紧急制动 · /autorun on|off|status 无人值守 · /chain on|off 链式推进 · data/seeds/ 丢.md录入 · data/STOP=总闸",
  "内置: /model 切模型 · /collab 远程介入 · /reload-plugins 刷新命令 · Esc 中断当前轮",
];

// ---- minimal omp extension API types (local mirror of the documented surface) ----

interface ExecResult {
  ok: boolean;
  text: string;
}

interface UiContext {
  notify(message: string, level: "info" | "warning" | "error"): void;
  setWidget(
    key: string,
    lines: string[] | undefined,
    options?: { placement?: "aboveEditor" | "belowEditor" },
  ): void;
  input(prompt: string, defaultValue?: string): Promise<string | undefined>;
  confirm(message: string): Promise<boolean>;
  select(
    title: string,
    options: (string | { label: string; description?: string })[],
    dialogOptions?: { helpText?: string; initialIndex?: number; signal?: AbortSignal },
  ): Promise<string | undefined>;
  editor(title: string, prefill?: string): Promise<string | undefined>;
  custom?<T>(factory: (...runtimeArgs: unknown[]) => unknown, options?: { overlay?: boolean }): Promise<T>;
}

interface SessionEntry {
  type?: string;
  customType?: string;
  data?: unknown;
}

interface ModelLike {
  id: string;
  provider?: string;
  name?: string;
  reasoning?: boolean;
  contextWindow?: number;
}

interface ExtensionContext {
  cwd: string;
  hasUI: boolean;
  ui: UiContext;
  models?: {
    list(): ModelLike[];
    current(): ModelLike | undefined;
    resolve(spec: string): ModelLike | undefined;
    family(m: ModelLike): string | undefined;
  };
  isIdle(): boolean;
  getAsyncJobSnapshot(): unknown;
  setInterval(fn: () => void, ms: number): unknown;
  sessionManager: { getBranch(): SessionEntry[] };
}

interface ExtensionApi {
  on(event: "session_start" | "input" | "session_shutdown", handler: (event: unknown, ctx: ExtensionContext) => void | Promise<void>): void;
  registerCommand(
    name: string,
    def: {
      description: string;
      getArgumentCompletions?: (
        argumentPrefix: string,
      ) => { value: string; label: string; description?: string }[] | null;
      handler: (args: string, ctx: ExtensionContext) => void | Promise<void>;
    },
  ): void;
  appendEntry(customType: string, data: unknown): void;
  sendUserMessage(content: string): void;
  exec(command: string, args?: string[], options?: unknown): Promise<unknown>;
  registerProvider(name: string, config: Record<string, unknown>): void;
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
    const stdout =
      "stdout" in r && typeof r.stdout === "string"
        ? r.stdout
        : "output" in r && typeof r.output === "string"
          ? r.output
          : "text" in r && typeof r.text === "string"
            ? r.text
            : "";
    const stderr = "stderr" in r && typeof r.stderr === "string" ? r.stderr : "";
    const text = stdout + (stderr ? (stdout ? "\n[stderr] " : "") + stderr : "") || JSON.stringify(r);
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

// ---- first-time setup wizard (worker model roles) ----

const SETUP_ROLES: { role: string; label: string; hint: string }[] = [
  { role: "mathx_solver", label: "mathx_solver — 求解主攻", hint: "证明攻关 worker" },
  { role: "mathx_hunter", label: "mathx_hunter — 补货猎手", hint: "搜寻开放问题 worker" },
  { role: "referee_1", label: "referee_1 — 裁判 1", hint: "交叉验证，建议与其它裁判不同模型" },
  { role: "referee_2", label: "referee_2 — 裁判 2", hint: "交叉验证，建议与其它裁判不同模型" },
  { role: "referee_3", label: "referee_3 — 裁判 3", hint: "交叉验证，建议与其它裁判不同模型" },
];

const MODEL_ROLE_STORAGE = "modelRoleStorage: project";

/** Replace or append a modelRoles block in .omp/config.yml (line-based, machine-managed). */
function mergeModelRoles(text: string, roles: Record<string, string>): string {
  const block = Object.entries(roles)
    .map(([k, v]) => `  ${k}: "${v.replace(/"/g, '\\"')}"`)
    .join("\n");
  let out: string;
  if (text.trim() === "") {
    out = `modelRoles:\n${block}\n`;
  } else {
    const lines = text.split(/\r?\n/);
    const idx = lines.findIndex((l) => /^modelRoles:\s*$/.test(l));
    if (idx === -1) {
      out = text.replace(/\s+$/, "") + `\n\nmodelRoles:\n${block}\n`;
    } else {
      let end = idx + 1;
      while (end < lines.length && (lines[end].startsWith(" ") || lines[end].trim() === "")) end++;
      const head = lines.slice(0, idx).join("\n").replace(/\s+$/, "");
      const tail = lines.slice(end).join("\n").replace(/\s+$/, "");
      out = `${head ? head + "\n" : ""}modelRoles:\n${block}${tail ? `\n${tail}` : ""}\n`;
    }
  }
  return ensureModelRoleStorage(out);
}

/** Insert `modelRoleStorage: project` above the modelRoles block when no top-level
 *  modelRoleStorage key exists yet. Idempotent; a user-set value is preserved.
 *  Without it, /model writes role picks to the global config where the project's
 *  own roles shadow them — see omp://settings.md "Where writes go". */
function ensureModelRoleStorage(text: string): string {
  if (/^modelRoleStorage:\s*\S/m.test(text)) return text;
  const lines = text.split(/\r?\n/);
  const idx = lines.findIndex((l) => /^modelRoles:\s*$/.test(l));
  if (idx === -1) return text;
  lines.splice(idx, 0, MODEL_ROLE_STORAGE);
  return lines.join("\n");
}

function needsSetup(): boolean {
  // Setup is done iff the wizard's output exists: project .omp/config.yml with a
  // modelRoles block. File-based (no ctx.models dependency), gitignored so a
  // fresh clone always fires the wizard once.
  try {
    const text = readFileSync(join(process.cwd(), ".omp", "config.yml"), "utf-8");
    return !/^modelRoles:/m.test(text);
  } catch {
    return true; // missing file → not set up yet
  }
}

function modelSelector(m: ModelLike): string {
  return m.provider ? `${m.provider}/${m.id}` : m.id;
}

function modelDescription(m: ModelLike): string {
  const bits: string[] = [];
  if (m.name && m.name !== m.id) bits.push(m.name);
  if (m.reasoning) bits.push("推理");
  if (m.contextWindow) bits.push(`${Math.round(m.contextWindow / 1000)}k ctx`);
  return bits.join(" · ") || m.id;
}

/**
 * Interactive first-time setup: pick a model per worker role via the searchable
 * select dialog (same pattern as /show), then write .omp/config.yml modelRoles.
 * Referee suggestions prefer models from families not yet chosen (cross-model
 * verification). Headless/old builds fall back to a notify with instructions.
 */
async function runSetupWizard(ctx: ExtensionContext): Promise<void> {
  if (typeof ctx.ui.select !== "function" || typeof ctx.ui.input !== "function" || !ctx.models?.list) {
    ctx.ui.notify(
      "当前环境不支持交互向导（headless/旧版 omp）：请手工编辑 .omp/config.yml 的 modelRoles，把 5 个角色指向可用模型。",
      "warning",
    );
    return;
  }
  const models = ctx.models.list();
  if (models.length === 0) {
    ctx.ui.notify(
      "未检测到任何可用模型：当前机器还没有可用的 LLM provider。\n" +
      "mathx 网关由扩展在加载时自动注册（runtime provider），但需网关运行才能发现模型。请：\n" +
      "  1) 确认 config.toml 已配置 [providers] 的 keys（网关上游）；\n" +
      "  2) 重启会话后重新运行 /mxsetup（扩展注册 + 网关启动后即可发现）；或\n" +
      "  3) 编辑 ~/.omp/agent/models.yml 添加其它 provider，或用环境变量设置 API key。",
      "warning",
    );
    return;
  }
  const chosen: Record<string, string> = {};
  const chosenFamilies: string[] = [];
  for (const r of SETUP_ROLES) {
    const items: { label: string; description?: string }[] = [
      { label: "跟随 @default", description: "使用 default 角色的模型（推荐起步）" },
      { label: "✎ 自定义输入…", description: "手动输入 provider/model，如 medeli/deepseek-v4-flash" },
      ...models.map((m) => ({ label: modelSelector(m), description: modelDescription(m) })),
    ];
    // Referee: suggest a model whose family is not yet picked.
    let suggested = models[0];
    if (r.role.startsWith("referee_")) {
      const alt = models.find((m) => {
        const fam = ctx.models?.family?.(m);
        return fam ? !chosenFamilies.includes(fam) : false;
      });
      if (alt) suggested = alt;
    }
    const initialIndex = 2 + models.indexOf(suggested);
    const picked = await ctx.ui.select(
      `选择 ${r.label} 的模型（${r.hint}）`,
      items,
      {
        helpText: "↑↓ 导航 · 打字搜索(provider/模型) · Enter 选中 · Esc 跳过(用 @default)",
        initialIndex: initialIndex >= 2 ? initialIndex : 2,
      },
    );
    if (picked === undefined || picked === "跟随 @default") {
      chosen[r.role] = "@default";
      continue;
    }
    if (picked === "✎ 自定义输入…") {
      const spec = await ctx.ui.input(`输入 ${r.role} 的模型（provider/model）`, "");
      const s = (spec || "").trim();
      if (!s) {
        chosen[r.role] = "@default";
        continue;
      }
      if (!ctx.models?.resolve?.(s)) {
        ctx.ui.notify(`「${s}」无法解析，已回退 @default。可用模型可在 /model 中查看。`, "warning");
        chosen[r.role] = "@default";
        continue;
      }
      chosen[r.role] = s;
      continue;
    }
    chosen[r.role] = picked;
    const m = models.find((mm) => modelSelector(mm) === picked);
    const fam = m ? ctx.models?.family?.(m) : undefined;
    if (fam) chosenFamilies.push(fam);
  }

  const cfgPath = join(ctx.cwd, ".omp", "config.yml");
  let text = "";
  try {
    text = readFileSync(cfgPath, "utf-8");
  } catch { /* missing file: write fresh */ }
  writeFileSync(cfgPath, mergeModelRoles(text, chosen), "utf-8");
  ctx.ui.notify(
    "已写入 .omp/config.yml（modelRoles）：\n" +
      Object.entries(chosen).map(([k, v]) => `  ${k} = ${v}`).join("\n") +
      "\n重启会话后生效。",
    "info",
  );
  const usesGateway = Object.values(chosen).some((v) => v.startsWith("mathx/"));
  if (usesGateway && !existsSync(join(ctx.cwd, "config.toml"))) {
    ctx.ui.notify(
      "你选择了 mathx/* 模型（经本地网关 127.0.0.1:8399 转发），但仓库根缺少 config.toml：\n" +
      "请复制 config.example.toml 为 config.toml 并填入 [providers] 的 keys（或用 /config setup）。",
      "warning",
    );
  }
}

export default function mathxAutorun(pi: ExtensionApi) {
  let autorunOn = false;
  let quotaSkips = 0;
  let cheatsheetShown = false;

  // Preferred clipboard path: omp's native arboard binding (sync, Unicode-safe).
  // Unverified whether project extensions can resolve this package — the
  // PowerShell fallback in copyTextSync covers failure. Fire-and-forget pre-warm.
  let nativesCopy: ((text: string) => void) | null = null;
  import("@oh-my-pi/pi-natives/clipboard")
    .then((m) => { nativesCopy = (m as { copyToClipboard: (t: string) => void }).copyToClipboard; })
    .catch(() => { /* not resolvable from extensions → PowerShell fallback */ });

  // Register the mathx local gateway as a project-local omp provider at
  // runtime — no global ~/.omp/agent/models.yml pollution. The gateway strips
  // inbound auth (injects its own pool keys from config.toml), so a dummy
  // apiKey satisfies omp's runtime-registration requirement without effect.
  // Models are fetched synchronously from GET /v1/models at load time, with
  // per-model api derived from supported_endpoint_types (openai→completions,
  // anthropic→messages). If the gateway isn't running yet (cold first session),
  // no models are registered; the gateway is launched on session_start below
  // and models appear next session automatically.
  try {
    const probe = spawnSync("curl", ["-sf", "-m", "2", "http://127.0.0.1:8399/v1/models"], {
      encoding: "utf-8",
      timeout: 3000,
    });
    if (probe.status === 0 && probe.stdout) {
      const data = JSON.parse(probe.stdout) as { data?: { id: string; supported_endpoint_types?: string[] }[] };
      const models = (data.data || []).map((m) => {
        const types = m.supported_endpoint_types || [];
        const api = types.includes("anthropic") ? "anthropic-messages" : "openai-completions";
        return { id: m.id, name: `${m.id} (mathx gw)`, api };
      });
      if (models.length > 0) {
        pi.registerProvider("mathx", {
          baseUrl: "http://127.0.0.1:8399/v1",
          api: "openai-completions",
          apiKey: "mathx-gateway-no-key-needed",
          models,
        });
      }
    }
  } catch { /* gateway down or curl unavailable — mathx models won't appear this session */ }

  function showCheatsheet(ctx: ExtensionContext): void {
    try {
      ctx.ui.setWidget("mathx.cheatsheet", CHEATSHEET_LINES, { placement: "belowEditor" });
      cheatsheetShown = true;
    } catch {
      try {
        ctx.ui.notify(CHEATSHEET_LINES[0], "info");
      } catch { /* UI optional */ }
    }
  }

  function hideCheatsheet(ctx: ExtensionContext): void {
    if (!cheatsheetShown) return;
    cheatsheetShown = false;
    try {
      ctx.ui.setWidget("mathx.cheatsheet", undefined);
    } catch { /* UI optional */ }
  }

  /**
   * Run one shell command line through pi.exec. The current runtime spreads
   * `args` internally, so a bare single-string pi.exec(line) throws "Spread
   * syntax requires ...iterable" — always pass an explicit args array via a
   * platform shell. Prefer execMathx for mathx CLIs: no shell, no quoting bugs.
   */
  async function execLine(line: string): Promise<ExecResult> {
    const win = process.platform === "win32";
    return normalizeExec(await pi.exec(win ? "cmd" : "sh", [win ? "/c" : "-c", line]));
  }

  /** mathx CLI: direct argv execution (no shell, no quoting bugs). */
  async function execMathx(moduleArgs: string[]): Promise<ExecResult> {
    return normalizeExec(await pi.exec("uv", ["run", "python", "-m", ...moduleArgs]));
  }

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
        const r = await execLine(quota.cmd);
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
    if (!existsSync(join(ctx.cwd, "data", "CHAIN_ON"))) return; // chain gate: settle-and-report only
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
        // Sync the chain gate file with the last persisted chain decision.
        if (
          entry?.type === "custom" &&
          entry?.customType === CHAIN_STATE_TYPE &&
          entry.data &&
          typeof entry.data === "object" &&
          "on" in entry.data
        ) {
          const chainPath = join(ctx.cwd, "data", "CHAIN_ON");
          try {
            if (entry.data.on) {
              if (!existsSync(chainPath)) writeFileSync(chainPath, new Date().toISOString() + "\n", "utf-8");
            } else if (existsSync(chainPath)) {
              unlinkSync(chainPath);
            }
          } catch { /* chain sync best-effort */ }
        }
      }
    } catch { /* state replay is best-effort */ }

    if (ctx.hasUI) {
      showCheatsheet(ctx);
    }

    // The mathx LLM gateway (config.toml key pool, :8399) dies with the machine,
    // not with omp — relaunch it detached whenever a session starts and it's down.
    try {
      const gwUp = await fetch("http://127.0.0.1:8399/healthz", { signal: AbortSignal.timeout(1500) })
        .then((r) => r.ok)
        .catch(() => false);
      if (!gwUp) {
        // Fire-and-forget via a REAL detached process (Node spawn, not cmd
        // `start`): cmd `start` inherits the outer cmd's pipes and pi.exec
        // waits for EOF that never comes (30s handler timeout) — and its new
        // window's cwd is unreliable. detached+stdio-ignore gives a clean
        // background process with the project root as cwd.
        spawn("uv", ["run", "python", "-m", "mathx.gateway"], {
          cwd: ctx.cwd,
          detached: true,
          stdio: "ignore",
          windowsHide: true,
        }).unref();
        ctx.ui.notify("mathx gateway (:8399) 未运行，已在后台启动", "info");
      }
    } catch { /* gateway optional; worker spawns will fail visibly if it stays down */ }

    // First-time setup: if worker roles don't resolve yet (fresh clone), offer the wizard.
    try {
      if (ctx.hasUI && needsSetup()) {
        const go = await ctx.ui.confirm(
          "MathExplorer 首次使用：需要配置 5 个 worker 模型角色（solver/hunter/referee×3）。现在设置？" +
          "（稍后随时可用 /mxsetup 重新打开）",
        );
        if (go) {
          // 不 await：extension handler 有 30s 超时，交互向导（5 次 select）必然超时被杀。
          // fire-and-forget：dialog 由 UI 控制器驱动，handler 返回后仍可弹窗。
          void runSetupWizard(ctx).catch((e) => ctx.ui.notify(`设置向导出错：${e}`, "error"));
        }
      }
    } catch { /* setup wizard is best-effort */ }

    // Die-with-omp semantics: on session shutdown, kill the gateway we (or a
    // previous session) started — but only if it actually answers on :8399
    // (guards against stale pid files and PID recycling).
    pi.on("session_shutdown", async (_e: unknown, ctx: ExtensionContext) => {
      try {
        const pidText = readFileSync(join(ctx.cwd, "logs", "gateway.pid"), "utf-8").trim();
        const gwUp = await fetch("http://127.0.0.1:8399/healthz", { signal: AbortSignal.timeout(1200) })
          .then((r) => r.ok)
          .catch(() => false);
        if (gwUp && /^\d+$/.test(pidText)) {
          await execLine(`taskkill /PID ${pidText} /T /F`);
        }
        try {
          unlinkSync(join(ctx.cwd, "logs", "gateway.pid"));
        } catch { /* already gone */ }
      } catch { /* shutdown best-effort */ }
    });

    ctx.setInterval(() => {
      tick(ctx).catch(() => { /* contained by ctx.setInterval already; belt and braces */ });
    }, TICK_MS);
  });

  // The cheatsheet is a first-run hint: dismiss it as soon as the user sends
  // anything. /mx brings it back; the next input hides it again.
  pi.on("input", async (_e: unknown, ctx: ExtensionContext) => {
    hideCheatsheet(ctx);
  });

  pi.registerCommand("mx", {
    description: "MathExplorer 速查表（重新显示在编辑器下方）",
    handler: (_args: string, ctx: ExtensionContext) => {
      showCheatsheet(ctx);
      ctx.ui.notify("速查表已显示在编辑器下方（下一次输入后自动隐藏）", "info");
    },
  });

  // ---- /show: inspect recorded problems; interactive menu + direct id lookup ----
  const SHOW_STATUSES = ["queued", "tackling", "postponed", "solved", "unsolvable"];
  interface RegistryEntryLite {
    id: string;
    title: string;
    status: string;
    tractability: number;
    field: string;
    verdict?: string;
    reason?: string;
  }
  let showRegistryCache: { at: number; entries: RegistryEntryLite[]; fields: string[] } | null = null;

  function loadShowRegistry(): { entries: RegistryEntryLite[]; fields: string[] } {
    if (showRegistryCache && Date.now() - showRegistryCache.at < 5000) return showRegistryCache;
    let entries: RegistryEntryLite[] = [];
    let fields: string[] = [];
    try {
      const reg = JSON.parse(readFileSync(join(process.cwd(), "data", "registry.json"), "utf-8"));
      entries = (reg.problems ?? []).map((p: RegistryEntryLite) => ({
        id: p.id, title: p.title, status: p.status, tractability: p.tractability, field: p.field,
        verdict: p.verdict, reason: p.reason,
      }));
    } catch { /* registry unreadable: fields only */ }
    try {
      fields = readFileSync(join(process.cwd(), "data", "fields.txt"), "utf-8")
        .split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    } catch { /* fields.txt unreadable */ }
    showRegistryCache = { at: Date.now(), entries, fields };
    return showRegistryCache;
  }

  interface ShowOutput {
    ambiguous?: boolean;
    matches?: { id: string; title: string }[];
    count?: number;
    problems?: { entry: RegistryEntryLite; statement: string | null }[];
  }

  interface SolutionProblem {
    entry: RegistryEntryLite;
    verified_file: string;
    verified: string | null;
    draft_file: string;
    draft: string | null;
    verification_file: string;
    verification: {
      verdict?: string;
      verification_report?: {
        summary?: string;
        critical_errors?: { location: string; issue: string }[];
        gaps?: { location: string; issue: string }[];
      };
    } | null;
    run: { status?: string; iteration?: number; max_iterations?: number; phase?: string } | null;
  }
  interface SolutionOutput {
    ambiguous?: boolean;
    matches?: { id: string; title: string }[];
    count?: number;
    problems?: SolutionProblem[];
  }

  /** Legacy rendering: notify the first 5 matches (headless fallback & no-select guard). */
  async function renderShowResults(ctx: ExtensionContext, tokens: string[]): Promise<void> {
    const r = await execMathx(["mathx.harvest", "show", ...tokens]);
    if (!r.ok) {
      ctx.ui.notify(`show 失败: ${r.text.trim().slice(0, 300)}`, "error");
      return;
    }
    let out: ShowOutput;
    try {
      out = JSON.parse(r.text) as ShowOutput;
    } catch {
      ctx.ui.notify(r.text.trim().slice(0, 500) || "(empty show output)", "info");
      return;
    }
    if (out.ambiguous) {
      const lines = (out.matches ?? []).map((m) => `  ${m.id} — ${m.title}`);
      ctx.ui.notify(`前缀不唯一，匹配 ${lines.length} 个:\n${lines.join("\n")}`, "warning");
      return;
    }
    const problems = out.problems ?? [];
    if (!out.count || problems.length === 0) {
      ctx.ui.notify("没有匹配的问题", "warning");
      return;
    }
    const blocks = problems.slice(0, 5).map((p) => {
      const e = p.entry;
      const body = String(p.statement ?? "(no statement file)").replace(/^---[\s\S]*?---\s*/, "").trim();
      const solvedPtr = e.status === "solved" ? `\n\n（已解决 · 题解：/solution ${e.id}）` : "";
      return `## ${e.title}\n${e.id} · ${e.status} · tractability=${e.tractability}\n\n${body}${solvedPtr}`;
    });
    let text = blocks.join("\n\n----------\n\n");
    if (out.count > 5) text += `\n\n…共 ${out.count} 个匹配，仅显示前 5 个（请用更精确的 id/过滤条件）`;
    ctx.ui.notify(text, "info");
  }

  /** Verdict summary + full proof text for one resolved problem. */
  function renderSolutionText(p: SolutionProblem): string {
    const e = p.entry;
    const head = `${e.title}\n${e.id} · ${e.status} · tractability=${e.tractability}`;
    let verdictBlock: string;
    if (p.verification) {
      const rep = p.verification.verification_report ?? {};
      verdictBlock =
        `=== 裁决（${p.verification_file}）===\n` +
        `verdict: ${p.verification.verdict ?? "?"} · critical_errors=${(rep.critical_errors ?? []).length} · gaps=${(rep.gaps ?? []).length}\n\n` +
        `${rep.summary ?? "(no summary)"}`;
    } else if (e.verdict) {
      verdictBlock = `=== 裁决 ===\nverdict: ${e.verdict}（结论在 registry，未跑过三裁判验证）${e.reason ? `\n依据: ${e.reason}` : ""}`;
    } else {
      verdictBlock = "=== 裁决 ===\n尚无 verification.json（未跑过三裁判验证）";
    }
    let proofBlock: string;
    if (p.verified !== null) {
      proofBlock = `=== 证明（${p.verified_file}）===\n\n${p.verified}`;
    } else if (p.draft !== null) {
      proofBlock = `=== 草稿（${p.draft_file}，未通过验证）===\n\n${p.draft}`;
    } else {
      const runInfo = p.run
        ? `进度：iteration ${p.run.iteration ?? "?"}/${p.run.max_iterations ?? "?"} · phase=${p.run.phase ?? "?"} · ${p.run.status ?? "?"}`
        : "results/ 下无任何产物";
      proofBlock = `=== 题解 ===\n尚无 blueprint（${runInfo}）`;
    }
    return `${head}\n\n${verdictBlock}\n\n${proofBlock}`;
  }

  /** OSC 52 terminal clipboard: ESC ] 52 ; c ; <base64> BEL. The USER's terminal
   *  (iTerm2 / Windows Terminal / kitty / alacritty...) writes the payload to its
   *  LOCAL clipboard — the only mechanism that crosses an SSH session. */
  function writeOsc52(text: string): boolean {
    try {
      const b64 = Buffer.from(text, "utf-8").toString("base64");
      process.stdout.write(`\x1b]52;c;${b64}\x07`);
      return true;
    } catch {
      return false;
    }
  }

  /** macOS system clipboard via pbcopy (native UTF-8, stdin feed, no temp file). */
  function pbcopySync(text: string): boolean {
    try {
      const r = spawnSync("pbcopy", [], { input: text, encoding: "utf-8" });
      return r.status === 0;
    } catch {
      return false;
    }
  }

  /** Sync copy so one keypress completes and the footer updates on the same render. */
  function copyTextSync(text: string): boolean {
    const inSSH = !!(process.env.SSH_CONNECTION || process.env.SSH_TTY);
    if (inSSH && writeOsc52(text)) return true; // SSH: goes to the user's LOCAL clipboard
    if (nativesCopy) {
      try { nativesCopy(text); return true; } catch { /* fall through to shell */ }
    }
    if (process.platform === "darwin") {
      return pbcopySync(text); // local Mac session
    }
    if (process.platform === "win32") {
      // clip.exe mangles non-ASCII (codepage); PowerShell Set-Clipboard + UTF-8 temp file is safe.
      const tmp = join(process.env.TEMP || ".", `mathx-clip-${process.pid}-${Date.now()}.txt`);
      try {
        writeFileSync(tmp, text, "utf-8");
        const r = spawnSync(
          "powershell",
          ["-NoProfile", "-NonInteractive", "-Command",
           `Get-Content -Raw -Encoding UTF8 '${tmp.replace(/'/g, "''")}' | Set-Clipboard`],
          { stdio: "ignore", windowsHide: true },
        );
        return r.status === 0;
      } catch { return false; }
      finally { try { unlinkSync(tmp); } catch { /* best-effort */ } }
    }
    return false;
  }

  // East Asian Wide/Fullwidth ranges → display width 2; everything else 1.
  const WIDE_CHAR = /[ᄀ-ᅟ⺀-鿿ꥠ-꥿가-힣豈-﫿︰-﹏＀-｠￠-￦]/;

  /** Read-only scrollable viewer; c / Ctrl+O copies the full text, s swaps
   *  statement ⇄ solution when an alternate view exists, Esc/Enter/q closes. */
  class TextViewer {
    private logical: string[];
    private wrapped: string[] = [];
    private wrappedWidth = -1;
    private offset = 0;
    private copyNote = "";
    private showingB = false;
    private viewA: { title: string; text: string };
    private viewB: { title: string; text: string } | null;
    constructor(
      title: string,
      text: string,
      private done: (v: undefined) => void,
      alternate: { title: string; text: string } | null = null,
      private requestRender: (() => void) | null = null,
    ) {
      this.viewA = { title, text };
      this.viewB = alternate;
      this.logical = text.replace(/\t/g, "    ").split("\n");
    }
    private currentView(): { title: string; text: string } {
      return this.showingB && this.viewB ? this.viewB : this.viewA;
    }
    private swap(): void {
      if (!this.viewB) return;
      this.showingB = !this.showingB;
      this.logical = this.currentView().text.replace(/\t/g, "    ").split("\n");
      this.offset = 0;
      this.copyNote = "";
      this.wrappedWidth = -1;
      this.requestRender?.();
    }
    private wrap(width: number): string[] {
      if (this.wrappedWidth === width) return this.wrapped;
      const out: string[] = [];
      for (const line of this.logical) {
        if (line.length === 0) { out.push(""); continue; }
        let cur = "", curW = 0;
        for (const ch of line) {
          const w = WIDE_CHAR.test(ch) ? 2 : 1;
          if (curW + w > width) { out.push(cur); cur = ""; curW = 0; }
          cur += ch; curW += w;
        }
        out.push(cur);
      }
      this.wrapped = out;
      this.wrappedWidth = width;
      return out;
    }
    private pageSize(): number {
      return Math.max(5, (process.stdout.rows || 24) - 6);
    }
    handleInput(data: string): void {
      const page = this.pageSize();
      const maxOff = Math.max(0, this.wrapped.length - page);
      if (data === "\x1b" || data === "\r" || data === "q") { this.done(undefined); return; }
      if (data === "c" || data === "\x0f") {
        const text = this.logical.join("\n");
        this.copyNote = copyTextSync(text)
          ? `✓ 已复制 ${text.length} 字符 `
          : "✗ 复制失败 ";
        this.requestRender?.();
        return;
      }
      if (data === "s") { this.swap(); return; }
      if (data === "\x1b[A") this.offset = Math.max(0, this.offset - 1);
      else if (data === "\x1b[B") this.offset = Math.min(maxOff, this.offset + 1);
      else if (data === "\x1b[5~") this.offset = Math.max(0, this.offset - page);
      else if (data === "\x1b[6~") this.offset = Math.min(maxOff, this.offset + page);
      else if (data === "\x1b[H" || data === "\x1b[1~") this.offset = 0;
      else if (data === "\x1b[F" || data === "\x1b[4~") this.offset = maxOff;
      this.requestRender?.();
    }
    render(width: number): readonly string[] {
      const w = Math.max(20, width);
      const body = this.wrap(w);
      const page = this.pageSize();
      const maxOff = Math.max(0, body.length - page);
      this.offset = Math.min(this.offset, maxOff);
      const view = body.slice(this.offset, this.offset + page);
      while (view.length < page) view.push("");
      const rule = "─".repeat(w);
      const toggleHint = this.viewB ? "s 切换 · " : "";
      const foot =
        `${this.copyNote}c/Ctrl+O 复制全文 · ${toggleHint}↑↓ PgUp/PgDn 滚动 · Esc 关闭 · ` +
        `${this.offset + 1}-${Math.min(this.offset + page, body.length)}/${body.length}`;
      return [` ${this.currentView().title}`.slice(0, w), rule, ...view, rule, foot.slice(0, w)];
    }
  }

  /** Unified preview: custom viewer (copy chord + view swap) → editor dialog → truncated notify. */
  async function previewText(
    ctx: ExtensionContext,
    title: string,
    text: string,
    alternate: { title: string; text: string } | null = null,
  ): Promise<void> {
    if (typeof ctx.ui.custom === "function") {
      try {
        await ctx.ui.custom<undefined>((...runtimeArgs: unknown[]) => {
          const done = runtimeArgs[3];
          if (typeof done !== "function") throw new Error("custom UI done callback unavailable");
          const tui = runtimeArgs[0];
          const requestRender =
            tui && typeof tui === "object" && typeof (tui as { requestRender?: unknown }).requestRender === "function"
              ? () => (tui as { requestRender: () => void }).requestRender()
              : null;
          return new TextViewer(title, text, done as (v: undefined) => void, alternate, requestRender);
        });
        return;
      } catch { /* custom unsupported/failed → editor fallback */ }
    }
    if (typeof ctx.ui.editor === "function") {
      try { await ctx.ui.editor(title, text); return; } catch { /* notify fallback */ }
    }
    ctx.ui.notify(text.slice(0, 1500) + (text.length > 1500 ? "\n…(截断，无法打开预览窗口)" : ""), "info");
  }

  /** Fetch + render the statement for one id/prefix; null when unresolvable. */
  async function fetchStatement(
    ctx: ExtensionContext,
    tokens: string[],
  ): Promise<{ title: string; text: string; status: string } | null> {
    const r = await execMathx(["mathx.harvest", "show", ...tokens]);
    if (!r.ok) {
      ctx.ui.notify(`show 失败: ${r.text.trim().slice(0, 300)}`, "error");
      return null;
    }
    let out: ShowOutput;
    try {
      out = JSON.parse(r.text) as ShowOutput;
    } catch {
      return null;
    }
    const p = out.problems?.[0];
    if (!p) return null;
    const e = p.entry;
    const body = String(p.statement ?? "(no statement file)").replace(/^---[\s\S]*?---\s*/, "").trim();
    return {
      title: `${e.id}  [${e.status}|t${e.tractability}]`,
      text: `${e.title}\n${e.id} · ${e.status} · tractability=${e.tractability}\n\n${body}`,
      status: e.status,
    };
  }

  /** Fetch + render the solution for one id/prefix; null when unresolvable. */
  async function fetchSolution(
    ctx: ExtensionContext,
    tokens: string[],
  ): Promise<{ title: string; text: string } | null> {
    const r = await execMathx(["mathx.harvest", "solution", ...tokens]);
    if (!r.ok) {
      ctx.ui.notify(`solution 失败: ${r.text.trim().slice(0, 300)}`, "error");
      return null;
    }
    let out: SolutionOutput;
    try {
      out = JSON.parse(r.text) as SolutionOutput;
    } catch {
      ctx.ui.notify(r.text.trim().slice(0, 500) || "(empty solution output)", "info");
      return null;
    }
    if (out.ambiguous) {
      const lines = (out.matches ?? []).map((m) => `  ${m.id} — ${m.title}`);
      ctx.ui.notify(`前缀不唯一，匹配 ${lines.length} 个:\n${lines.join("\n")}`, "warning");
      return null;
    }
    const p = out.problems?.[0];
    if (!p) {
      ctx.ui.notify("没有匹配的问题", "warning");
      return null;
    }
    const v = p.verification?.verdict;
    return { title: `题解 ${p.entry.id}${v ? ` [${v}]` : ""}`, text: renderSolutionText(p) };
  }

  /** Fetch + display the solution for one id/prefix; viewer in UI, truncated notify headless. */
  async function previewSolution(ctx: ExtensionContext, tokens: string[]): Promise<void> {
    const sol = await fetchSolution(ctx, tokens);
    if (!sol) return;
    if (ctx.hasUI) {
      const st = await fetchStatement(ctx, tokens);
      await previewText(ctx, sol.title, sol.text, st ? { title: st.title, text: st.text } : null);
    } else {
      ctx.ui.notify(
        sol.text.slice(0, 1500) + (sol.text.length > 1500 ? "\n…(截断，请在 UI 中用 /solution 查看全文)" : ""),
        "info",
      );
    }
  }

  /**
   * Interactive browse menu: type-to-search across id/title/status/field, filter by
   * status/field inside the menu, preview full statements in a read-only editor.
   * Filter state lives only for this menu session; Esc discards it. Read-only:
   * never touches data/ files beyond the registry cache.
   */
  async function interactiveMenu(
    ctx: ExtensionContext,
    preset?: { status?: string; field?: string },
    view: "statement" | "solution" = "statement",
  ): Promise<void> {
    // Guard: older omp builds may lack select/editor — fall back to the legacy notify path.
    if (typeof ctx.ui.select !== "function" || typeof ctx.ui.editor !== "function") {
      await renderShowResults(ctx, []);
      return;
    }
    const STATUS_LABEL = "⚙ 按状态过滤…";
    const FIELD_LABEL = "⚙ 按领域过滤…";
    let statusF = preset?.status ?? "全部";
    let fieldF = preset?.field ?? "全部";
    for (;;) {
      const { entries, fields } = loadShowRegistry();
      const filtered = entries.filter(
        (e) => (statusF === "全部" || e.status === statusF) && (fieldF === "全部" || e.field === fieldF),
      );
      const title = `${view === "solution" ? "查看题解" : "查看问题"} · ${statusF} · ${fieldF} · ${filtered.length}/${entries.length} 题`;
      const items: { label: string; description?: string }[] = [
        { label: STATUS_LABEL, description: `当前：${statusF}` },
        { label: FIELD_LABEL, description: `当前：${fieldF}` },
        ...filtered.map((e) => ({
          label: e.id,
          description: `[${e.status}|t${e.tractability}] ${e.title} · ${e.field}`,
        })),
      ];
      const picked = await ctx.ui.select(title, items, {
        helpText: "↑↓ 导航 · 打字搜索(id/标题/状态/领域) · Enter 选中 · Esc 退出",
      });
      if (picked === undefined) return; // Esc → leave the menu entirely
      if (picked === STATUS_LABEL) {
        const s = await ctx.ui.select("按状态过滤", ["全部", ...SHOW_STATUSES]);
        if (s !== undefined) statusF = s;
        continue;
      }
      if (picked === FIELD_LABEL) {
        const f = await ctx.ui.select("按领域过滤", ["全部", ...fields]);
        if (f !== undefined) fieldF = f;
        continue;
      }
      // picked is a problem id → preview the full statement, then return to the menu.
      if (view === "solution") {
        await previewSolution(ctx, [picked]);
        continue;
      }
      const st = await fetchStatement(ctx, [picked]);
      if (!st) continue;
      let solAlt: { title: string; text: string } | null = null;
      if (st.status === "solved") {
        solAlt = await fetchSolution(ctx, [picked]);
      }
      await previewText(ctx, st.title, st.text, solAlt);
      // loop continues → menu reappears with filters intact
    }
  }

  function showCompletions(prefix: string) {
    const { entries, fields } = loadShowRegistry();
    const p = (prefix || "").toLowerCase();
    const items: { value: string; label: string; description?: string }[] = [];
    for (const f of fields) {
      if (f.toLowerCase().startsWith(p)) items.push({ value: `${f}/`, label: `${f}/`, description: "kind" });
    }
    for (const e of entries) {
      if (e.id.toLowerCase().includes(p) || e.title.toLowerCase().includes(p)) {
        items.push({ value: e.id, label: e.id, description: `[${e.status}|t${e.tractability}] ${e.title}` });
      }
    }
    return items.slice(0, 50);
  }

  pi.registerCommand("mxsetup", {
    description: "MathExplorer 首次配置：交互式选择 solver/hunter/referee 模型角色（写入 .omp/config.yml）",
    handler: async (_args: string, ctx: ExtensionContext) => {
      await runSetupWizard(ctx);
    },
  });

  pi.registerCommand("show", {
    description: "查看已记录问题：/show <id前缀> 直接看题；/show 交互菜单浏览过滤",
    getArgumentCompletions: showCompletions,
    handler: async (args: string, ctx: ExtensionContext) => {
      const tokens = (args || "").trim().split(/\s+/).filter(Boolean);

      // no arguments → interactive menu (headless: legacy notify path)
      if (tokens.length === 0) {
        if (!ctx.hasUI) {
          await renderShowResults(ctx, []);
          return;
        }
        await interactiveMenu(ctx);
        return;
      }

      // legacy --flags are no longer forwarded: point into the menu instead
      if (tokens.some((t) => t.startsWith("--"))) {
        if (ctx.hasUI) {
          ctx.ui.notify("过滤参数已移至交互菜单：直接 /show 后选择状态/领域过滤", "info");
          await interactiveMenu(ctx);
        } else {
          ctx.ui.notify("用法：/show <id前缀> 直接看题；/show 交互浏览（过滤在菜单中）", "warning");
        }
        return;
      }

      // single bare status/field word → menu with that filter preset
      if (tokens.length === 1) {
        const t = tokens[0];
        const { fields } = loadShowRegistry();
        if (SHOW_STATUSES.includes(t)) {
          if (ctx.hasUI) await interactiveMenu(ctx, { status: t });
          else ctx.ui.notify("过滤已移至 /show 交互菜单", "info");
          return;
        }
        if (fields.includes(t)) {
          if (ctx.hasUI) await interactiveMenu(ctx, { field: t });
          else ctx.ui.notify("过滤已移至 /show 交互菜单", "info");
          return;
        }
      }

      // everything else: id-prefix direct lookup, legacy rendering preserved
      await renderShowResults(ctx, tokens);
    },
  });

  pi.registerCommand("solution", {
    description: "查看题解：/solution <id前缀> 直接看（裁决摘要+证明全文）；/solution 交互菜单（默认只列 solved）",
    getArgumentCompletions: showCompletions,
    handler: async (args: string, ctx: ExtensionContext) => {
      const tokens = (args || "").trim().split(/\s+/).filter(Boolean);

      // no arguments → interactive menu (default solved filter; changeable in-menu);
      // headless → list solved ids
      if (tokens.length === 0) {
        if (!ctx.hasUI) {
          const { entries } = loadShowRegistry();
          const solved = entries.filter((e) => e.status === "solved");
          const lines = solved.slice(0, 10).map((e) => `  ${e.id} — ${e.title}`);
          ctx.ui.notify(
            solved.length
              ? `已解决 ${solved.length} 题（最多列 10 个）:\n${lines.join("\n")}\n用 /solution <id前缀> 查看题解`
              : "尚无 solved 问题",
            "info",
          );
          return;
        }
        await interactiveMenu(ctx, { status: "solved" }, "solution");
        return;
      }

      if (tokens.some((t) => t.startsWith("--"))) {
        if (ctx.hasUI) {
          ctx.ui.notify("过滤参数已移至交互菜单：直接 /solution 后选择状态/领域过滤", "info");
          await interactiveMenu(ctx, { status: "solved" }, "solution");
        } else {
          ctx.ui.notify("用法：/solution <id前缀> 直接看题解；/solution 交互浏览（过滤在菜单中）", "warning");
        }
        return;
      }

      if (tokens.length === 1) {
        const t = tokens[0];
        const { fields } = loadShowRegistry();
        if (SHOW_STATUSES.includes(t)) {
          if (ctx.hasUI) await interactiveMenu(ctx, { status: t }, "solution");
          else ctx.ui.notify("过滤已移至 /solution 交互菜单", "info");
          return;
        }
        if (fields.includes(t)) {
          if (ctx.hasUI) await interactiveMenu(ctx, { status: "solved", field: t }, "solution");
          else ctx.ui.notify("过滤已移至 /solution 交互菜单", "info");
          return;
        }
      }

      await previewSolution(ctx, tokens);
    },
  });

  /** Resolve a bare id/prefix/slug against the registry cache, mirroring
   *  harvest's _resolve_entries: exact id > unique prefix on the id or its slug part. */
  function resolveEntry(prefix: string): { entry: RegistryEntryLite } | { ambiguous: RegistryEntryLite[] } | null {
    const { entries } = loadShowRegistry();
    const exact = entries.filter((e) => e.id === prefix);
    if (exact.length === 1) return { entry: exact[0] };
    if (exact.length > 1) return { ambiguous: exact };
    const matches = entries.filter(
      (e) => e.id.startsWith(prefix) || e.id.slice(e.id.lastIndexOf("/") + 1).startsWith(prefix),
    );
    if (matches.length === 1) return { entry: matches[0] };
    if (matches.length > 1) return { ambiguous: matches };
    return null;
  }

  pi.registerCommand("solve", {
    description: "开始/恢复攻关：/solve <id前缀>（queued=首攻；postponed=恢复续攻；分派由主 agent 执行）",
    getArgumentCompletions: showCompletions,
    handler: (args: string, ctx: ExtensionContext) => {
      const tokens = (args || "").trim().split(/\s+/).filter(Boolean);
      if (tokens.length === 0) {
        const { entries } = loadShowRegistry();
        const queued = entries.filter((e) => e.status === "queued").slice(0, 5);
        ctx.ui.notify(
          queued.length
            ? `用法：/solve <id前缀>（Tab 补全选题）。队列中待解：\n${queued.map((e) => `  ${e.id} — ${e.title}`).join("\n")}`
            : "用法：/solve <id前缀>（Tab 补全选题）。队列为空。",
          "warning",
        );
        return;
      }
      const prefix = tokens[0];
      const res = resolveEntry(prefix);
      if (!res) { ctx.ui.notify(`没有匹配的问题：${prefix}`, "warning"); return; }
      if ("ambiguous" in res) {
        const lines = res.ambiguous.map((e) => `  ${e.id} — ${e.title}`);
        ctx.ui.notify(`前缀不唯一，匹配 ${res.ambiguous.length} 个:\n${lines.join("\n")}`, "warning");
        return;
      }
      const e = res.entry;
      if (e.status === "tackling") { ctx.ui.notify(`已在攻关中：${e.id}`, "info"); return; }
      if (e.status === "solved") { ctx.ui.notify(`已解决${e.verdict ? `（verdict=${e.verdict}）` : ""}，无需再解：${e.id}`, "info"); return; }
      if (e.status === "unsolvable") { ctx.ui.notify(`已判定不可解，无需再解：${e.id}`, "info"); return; }
      const resume = e.status === "postponed";
      try {
        pi.sendUserMessage(
          `用户通过 /solve 请求开始攻关：${e.id}（前缀=${prefix}，当前状态=${e.status}${resume ? "，恢复续攻" : ""}）。` +
          `按项目派发纪律执行：` +
          `1) 先 \`uv run python -m mathx.harvest set-status ${e.id} tackling\`；若报 "chain gate closed"（data/CHAIN_ON 不存在）且这是用户命令，用 --force 绕过。` +
          `2) 用 \`uv run python -m mathx.agents name solver ${e.id}\` 生成 name（含所用模型，如 solver-k3-...），用 task 工具后台派出 solver，调用必须含 "agent": "solver" 字段与模型化的 "name"：{"agent": "solver", "name": "<name>", "task": "problem_id = ${e.id}. agent_name = <name>, model = <model>. Read prompts/generation/AGENTS.md and follow it exactly (memory policy, iteration protocol, stdin=DEVNULL discipline, _tmp_*.json cleanup). Resume from results/${e.id}/run.json if it exists, else runstate init + memory init."}` +
          `3) 派出后立即读 history://<spawned-id> 头部验证：agent 类型为 solver、model 符合 mathx_solver 角色；不对就 cancel 重派。验证通过后 \`uv run python -m mathx.agents stamp ${e.id} <name> <model>\` 把身份写入 run.json 的 agent 字段（归档可查模型）。` +
          (resume ? `该题处于 postponed，solver 需读 results/${e.id}/run.json 从上次迭代续攻（revival 语义）。` : ""),
        );
      } catch { /* best-effort */ }
      ctx.ui.notify(`已请求调度${resume ? "（恢复）" : ""}：${e.id}`, "info");
    },
  });

  pi.registerCommand("postpone", {
    description: "手工暂停攻关：/postpone <id前缀> [原因]（状态 → postponed，不再自动调度；恢复用 /solve 或『继续 <id前缀>』）",
    getArgumentCompletions: showCompletions,
    handler: async (args: string, ctx: ExtensionContext) => {
      const tokens = (args || "").trim().split(/\s+/).filter(Boolean);
      if (tokens.length === 0) { ctx.ui.notify("用法：/postpone <id前缀> [原因]", "warning"); return; }
      const prefix = tokens[0];
      const reason = tokens.slice(1).join(" ") || undefined;
      const res = resolveEntry(prefix);
      if (!res) { ctx.ui.notify(`没有匹配的问题：${prefix}`, "warning"); return; }
      if ("ambiguous" in res) {
        const lines = res.ambiguous.map((e) => `  ${e.id} — ${e.title}`);
        ctx.ui.notify(`前缀不唯一，匹配 ${res.ambiguous.length} 个:\n${lines.join("\n")}`, "warning");
        return;
      }
      const e = res.entry;
      if (e.status === "postponed") { ctx.ui.notify(`已在暂停中：${e.id}`, "info"); return; }
      if (e.status === "solved" || e.status === "unsolvable") { ctx.ui.notify(`该题已${e.status}，无需暂停：${e.id}`, "info"); return; }
      const r = await execMathx(["mathx.harvest", "set-status", e.id, "postponed", ...(reason ? ["--reason", reason] : [])]);
      if (!r.ok) { ctx.ui.notify(`暂停失败: ${r.text.trim().slice(0, 300)}`, "error"); return; }
      ctx.ui.notify(`已暂停：${e.id}${reason ? `（${reason}）` : ""}。恢复：/solve ${e.id} 或说『继续 ${e.id}』`, "info");
    },
  });

  pi.registerCommand("current", {
    description: "当前活跃问题：tackling 进度 + postponed 列表 + 队列概况",
    handler: async (_args: string, ctx: ExtensionContext) => {
      const r = await execMathx(["mathx.harvest", "current"]);
      if (!r.ok) { ctx.ui.notify(`current 失败: ${r.text.trim().slice(0, 300)}`, "error"); return; }
      let out: {
        counts?: Record<string, number>;
        active?: {
          id: string; title: string; status: string; tractability?: number; attempts?: number;
          reason?: string; last_activity_utc?: string;
          run?: { status?: string; iteration?: number; max_iterations?: number; phase?: string } | null;
        }[];
      };
      try {
        out = JSON.parse(r.text);
      } catch {
        ctx.ui.notify(r.text.trim().slice(0, 500), "info");
        return;
      }
      const counts = out.counts ?? {};
      const active = out.active ?? [];
      const tackling = active.filter((a) => a.status === "tackling");
      const postponed = active.filter((a) => a.status === "postponed");
      const lines: string[] = [];
      if (tackling.length === 0) lines.push("当前无攻关中的问题（tackling）");
      for (const a of tackling) {
        const run = a.run ?? {};
        lines.push(`▶ ${a.id} — ${a.title}`);
        lines.push(`  attempts=${a.attempts ?? 0} · 最后活动 ${(a.last_activity_utc ?? "").slice(0, 19).replace("T", " ")}`);
        lines.push(`  进度: iteration ${run.iteration ?? "?"}/${run.max_iterations ?? "?"} · phase=${run.phase ?? "?"} · run=${run.status ?? "?"}${run.agent?.model ? ` · 模型=${run.agent.model}` : ""}`);
        lines.push("");
      }
      if (postponed.length > 0) {
        lines.push(`⏸ 暂停中 ${postponed.length} 题:`);
        for (const a of postponed.slice(0, 10)) {
          lines.push(`  ${a.id} — ${a.reason ? `原因: ${a.reason} · ` : ""}${(a.last_activity_utc ?? "").slice(0, 19).replace("T", " ")}`);
        }
        lines.push("");
      }
      lines.push(
        `队列: queued=${counts.queued ?? 0} · tackling=${counts.tackling ?? 0} · postponed=${counts.postponed ?? 0} · solved=${counts.solved ?? 0} · unsolvable=${counts.unsolvable ?? 0}`,
      );
      lines.push("恢复暂停的题：/solve <id前缀> 或说『继续 <id前缀>』");
      ctx.ui.notify(lines.join("\n"), "info");
    },
  });

  pi.registerCommand("chain", {
    description: "链式推进开关：/chain on|off|status（on = 结算后自动派下一题；off = 仅汇报）",
    handler: (args: string, ctx: ExtensionContext) => {
      const sub = (args || "").trim() || "status";
      const chainPath = join(ctx.cwd, "data", "CHAIN_ON");
      if (sub === "on") {
        try {
          writeFileSync(chainPath, new Date().toISOString() + "\n", "utf-8");
        } catch { /* best-effort */ }
        pi.appendEntry(CHAIN_STATE_TYPE, { on: true });
        ctx.ui.notify("链式推进: ON（结算后自动派下一题/复活/补货）", "info");
        try {
          if (ctx.isIdle()) pi.sendUserMessage(CONTINUE_PROMPT);
        } catch { /* busy is fine */ }
      } else if (sub === "off") {
        try {
          unlinkSync(chainPath);
        } catch { /* already gone */ }
        pi.appendEntry(CHAIN_STATE_TYPE, { on: false });
        ctx.ui.notify("链式推进: OFF（结算照做，停止一切推进，仅汇报）", "info");
      } else {
        ctx.ui.notify(`链式推进: ${existsSync(chainPath) ? "ON" : "OFF"}`, "info");
      }
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
        const r = await execMathx(["mathx.config", "show", "--masked"]);
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
        const r = await execMathx(["mathx.config", "set", path, value]);
        const shown = path.endsWith(".keys") ? "<keys updated, masked>" : value;
        ctx.ui.notify(r.ok ? `已更新 ${path} = ${shown}` : `配置失败: ${r.text.trim()}`, r.ok ? "info" : "error");
        return;
      }

      if (sub === "test") {
        const r = await execMathx(["mathx.fleet", "--smoke"]);
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
        await execMathx(["mathx.config", "set", `providers.${provider}.api`, api]);
        await execMathx(["mathx.config", "set", `providers.${provider}.keys`, keys]);
        await execMathx(["mathx.config", "set", "active_provider", provider]);
        for (const role of ["default", "prover", "jury", "judge"]) {
          const check = await execMathx(["mathx.config", "get", `roles.${role}`]);
          if (!check.ok) {
            await execMathx(["mathx.config", "set", `roles.${role}`, `${provider}/${model}`]);
          }
        }
        ctx.ui.notify(`config.toml 已更新（provider=${provider}）。用 /config test 冒烟。`, "info");
        return;
      }

      ctx.ui.notify("用法: /config show|setup|set <path> <value>|test", "warning");
    },
  });
}
