import os from "node:os";
import fs from "node:fs";
import path from "node:path";

export interface ContextSniperPluginConfig {
  repoRoot?: string;
  workspaceRoot?: string;
  contextsniperUrl?: string;
  runtimeDir?: string;
  autoStart?: boolean;
  autoStop?: boolean;
  startWaitSeconds?: number;
  injectCodePolicy?: boolean;
  readToolPolicy?: string;
  filterEnabled?: boolean;
  filterNativeRead?: boolean;
  filterNativeExec?: boolean;
  searchLimit?: number;
  accountId?: string;
  userId?: string;
  agentId?: string;
  sessionId?: string;
}

export interface ResolvedConfig {
  pluginRoot: string;
  repoRoot: string;
  workspaceRoot: string;
  contextsniperUrl: string;
  runtimeDir: string;
  autoStart: boolean;
  autoStop: boolean;
  startWaitSeconds: number;
  injectCodePolicy: boolean;
  readToolPolicy: string;
  filterEnabled: boolean;
  filterNativeRead: boolean;
  filterNativeExec: boolean;
  searchLimit: number;
  accountId: string;
  userId: string;
  agentId: string;
  sessionId?: string;
}

function expandHome(value: string): string {
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.join(os.homedir(), value.slice(2));
  return value;
}

export function resolvePathValue(value: string, base = process.cwd()): string {
  const expanded = expandHome(value);
  return path.resolve(base, expanded);
}

function asBoolean(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    return ["1", "true", "yes", "on"].includes(value.toLowerCase());
  }
  return fallback;
}

function truthyEnv(name: string): boolean {
  const raw = (process.env[name] || "").trim().toLowerCase();
  return ["1", "true", "yes", "on"].includes(raw);
}

function launchCwd(): string | undefined {
  if (truthyEnv("CONTEXTSNIPER_OPENCLAW_IGNORE_LAUNCH_CWD")) return undefined;
  const raw = process.env.CONTEXTSNIPER_OPENCLAW_LAUNCH_CWD || process.env.PWD;
  if (!raw) return undefined;
  const candidate = resolvePathValue(raw);
  try {
    if (fs.statSync(candidate).isDirectory()) return candidate;
  } catch {
    // Ignore stale inherited PWD values and fall back to OpenClaw's cwd.
  }
  return undefined;
}

function asNumber(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, Math.trunc(parsed)));
}

function asReadToolPolicy(value: unknown): string {
  const raw = typeof value === "string" ? value.toLowerCase().trim() : "";
  if (["off", "none", "disabled", "disable"].includes(raw)) return "off";
  if (["guard", "strict", "block"].includes(raw)) return "guard";
  if (["advisory", "describe", "description", "warn"].includes(raw)) return "advisory";
  return "advisory";
}

export function resolveWorkspaceRoot(config: ContextSniperPluginConfig): string {
  const respectConfig = truthyEnv("CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG");
  const respectEnvPaths = truthyEnv("CONTEXTSNIPER_OPENCLAW_RESPECT_ENV_PATHS") || respectConfig;
  const raw =
    (respectEnvPaths ? process.env.CONTEXTSNIPER_WORKSPACE_ROOT || process.env.OPENCLAW_WORKSPACE_ROOT : undefined) ||
    (respectConfig && truthyEnv("CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG_WORKSPACE") ? config.workspaceRoot : undefined) ||
    launchCwd() ||
    process.cwd();
  return resolvePathValue(raw);
}

export function resolveConfig(config: ContextSniperPluginConfig, pluginRoot: string, repoRoot: string): ResolvedConfig {
  const respectConfig = truthyEnv("CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG");
  const respectEnvPaths = truthyEnv("CONTEXTSNIPER_OPENCLAW_RESPECT_ENV_PATHS") || respectConfig;
  const effectiveConfig = respectConfig ? config : {};
  const contextsniperUrl = (effectiveConfig.contextsniperUrl || process.env.CONTEXTSNIPER_URL || "http://127.0.0.1:8090").replace(/\/+$/, "");
  const runtimeDir = resolvePathValue(
    effectiveConfig.runtimeDir ||
      (respectEnvPaths ? process.env.CONTEXTSNIPER_RUNTIME_DIR : undefined) ||
      path.join(os.homedir(), ".cache", "contextsniper-openclaw-plugin"),
  );

  return {
    pluginRoot,
    repoRoot,
    workspaceRoot: resolveWorkspaceRoot(config),
    contextsniperUrl,
    runtimeDir,
    autoStart: asBoolean(effectiveConfig.autoStart ?? process.env.CONTEXTSNIPER_OPENCLAW_AUTO_START ?? process.env.CONTEXTSNIPER_PLUGIN_AUTO_START, true),
    autoStop: asBoolean(effectiveConfig.autoStop ?? process.env.CONTEXTSNIPER_OPENCLAW_AUTO_STOP ?? process.env.CONTEXTSNIPER_PLUGIN_AUTO_STOP, true),
    startWaitSeconds: asNumber(effectiveConfig.startWaitSeconds ?? process.env.CONTEXTSNIPER_PLUGIN_START_WAIT, 45, 1, 180),
    injectCodePolicy: asBoolean(effectiveConfig.injectCodePolicy, true),
    readToolPolicy: asReadToolPolicy(effectiveConfig.readToolPolicy ?? process.env.CONTEXTSNIPER_OPENCLAW_READ_TOOL_POLICY),
    filterEnabled: asBoolean(effectiveConfig.filterEnabled ?? process.env.CONTEXTSNIPER_FILTER_ENABLED, true),
    filterNativeRead: asBoolean(effectiveConfig.filterNativeRead ?? process.env.CONTEXTSNIPER_FILTER_NATIVE_READ, true),
    filterNativeExec: asBoolean(effectiveConfig.filterNativeExec ?? process.env.CONTEXTSNIPER_FILTER_NATIVE_BASH, true),
    searchLimit: asNumber(effectiveConfig.searchLimit ?? process.env.CONTEXTSNIPER_SEARCH_LIMIT, 4, 1, 100),
    accountId: effectiveConfig.accountId || process.env.CONTEXTSNIPER_ACCOUNT_ID || "acct-demo",
    userId: effectiveConfig.userId || process.env.CONTEXTSNIPER_USER_ID || "u-openclaw",
    agentId: effectiveConfig.agentId || process.env.CONTEXTSNIPER_AGENT_ID || "openclaw",
    sessionId: effectiveConfig.sessionId || process.env.CONTEXTSNIPER_SESSION_ID,
  };
}
