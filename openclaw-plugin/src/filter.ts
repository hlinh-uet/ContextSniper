import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import type { ResolvedConfig } from "./config.ts";
import { identityFields, postJson } from "./http.ts";
import { resolveInside } from "./paths.ts";

type Logger = { info?: (...args: unknown[]) => void; warn?: (...args: unknown[]) => void };

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function toolParams(event: any): Record<string, unknown> | null {
  return asRecord(event?.params) || asRecord(event?.arguments) || asRecord(event?.input);
}

function boolEnv(name: string, fallback = true): boolean {
  const raw = (process.env[name] || "").trim().toLowerCase();
  if (!raw) return fallback;
  return ["1", "true", "yes", "on"].includes(raw);
}

function commandFromParams(params: Record<string, unknown>): string {
  return String(params.command || params.cmd || params.input || "").trim();
}

function commandParamName(params: Record<string, unknown>): string {
  if (Object.prototype.hasOwnProperty.call(params, "command")) return "command";
  if (Object.prototype.hasOwnProperty.call(params, "cmd")) return "cmd";
  return "command";
}

function readPathFromParams(params: Record<string, unknown>): string {
  return String(params.path || params.file_path || params.filePath || "").trim();
}

function readPathParamName(params: Record<string, unknown>): string {
  if (Object.prototype.hasOwnProperty.call(params, "path")) return "path";
  if (Object.prototype.hasOwnProperty.call(params, "file_path")) return "file_path";
  if (Object.prototype.hasOwnProperty.call(params, "filePath")) return "filePath";
  return "path";
}

function bashFilterCandidate(command: string): string {
  const cmd = command.trim().replace(/\s+/g, " ");
  const lower = cmd.toLowerCase();
  if (!cmd) return "";
  if (/(^|[\s;&|()])(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+)?pytest\b/.test(lower)) return "test_runner";
  if (/(^|[\s;&|()])py\.test\b/.test(lower)) return "test_runner";
  if (lower.includes("tests/runtests.py") || /(^|[\s;&|()])tox\b/.test(lower)) return "test_runner";
  if (/(^|[\s;&|()])npm\s+(run\s+)?test\b/.test(lower)) return "test_runner";
  if (/(^|[\s;&|()])(?:python|python\d+(?:\.\d+)?)\s+(?:\.\/)?run_(smoke|filter_probe)\.py\b/.test(lower)) return "test_runner";
  if (/(^|[\s;&|()])git\s+diff\b/.test(lower)) return "diff";
  if (/(^|[\s;&|()])git\s+show\b/.test(lower)) return "diff_maybe";
  if (/(^|[\s;&|()])cat\s+\/tmp\/[^;&|]*\.(patch|diff)\b/.test(lower)) return "diff";
  if (/(^|[\s;&|()])(?:python|python\d+(?:\.\d+)?)\b/.test(lower)) return "python_maybe";
  return "";
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function pythonBin(): string {
  return process.env.PY_BIN || process.env.PYTHON || "python3";
}

async function filterRead(
  event: any,
  config: ResolvedConfig,
  ensureBackend: () => Promise<void>,
  logger: Logger,
): Promise<unknown> {
  if (!config.filterEnabled || !config.filterNativeRead || !boolEnv("CONTEXTSNIPER_FILTER_NATIVE_READ", true)) return undefined;
  const params = toolParams(event);
  if (!params) return undefined;
  if (params.offset !== undefined || params.limit !== undefined) return undefined;

  const requested = readPathFromParams(params);
  if (!requested) return undefined;

  let target: string;
  try {
    target = resolveInside(config.workspaceRoot, requested);
  } catch {
    return undefined;
  }

  try {
    await ensureBackend();
    const data = asRecord(await postJson(
      config,
      "/api/v1/filter_read",
      {
        ...identityFields(config.sessionId),
        accountId: config.accountId,
        userId: config.userId,
        agentId: config.agentId,
        file_path: target,
        workspaceRoot: config.workspaceRoot,
        tool_name: "read",
      },
      10000,
    ));
    if (!data?.ok || !data.filtered_file_path) {
      logger.info?.(`contextsniper: read filter skipped reason=${String(data?.reason || data?.error || "unknown")}`);
      return undefined;
    }

    logger.info?.(
      `contextsniper: read filtered detected=${String(data.detected || "")} chars=${String(data.original_chars || "")}->${String(data.filtered_chars || "")}`,
    );
    return {
      params: {
        ...params,
        [readPathParamName(params)]: String(data.filtered_file_path),
      },
    };
  } catch (error) {
    logger.warn?.(`contextsniper: read filter failed: ${String(error)}`);
    return undefined;
  }
}

async function filterExec(
  event: any,
  config: ResolvedConfig,
  ensureBackend: () => Promise<void>,
  logger: Logger,
): Promise<unknown> {
  if (!config.filterEnabled || !config.filterNativeExec || !boolEnv("CONTEXTSNIPER_FILTER_NATIVE_BASH", true)) return undefined;
  const params = toolParams(event);
  if (!params) return undefined;
  const command = commandFromParams(params);
  const category = bashFilterCandidate(command);
  if (!category) return undefined;

  try {
    await ensureBackend();
  } catch (error) {
    logger.warn?.(`contextsniper: exec filter backend unavailable: ${String(error)}`);
    return undefined;
  }

  const hookDir = path.join(config.runtimeDir, "openclaw-exec-filter-hook");
  await fs.mkdir(hookDir, { recursive: true });
  const runId = randomUUID().replace(/-/g, "");
  const payloadPath = path.join(hookDir, `${runId}.json`);
  const wrapperPath = path.join(hookDir, `${runId}.py`);
  const payload = {
    ...identityFields(config.sessionId),
    accountId: config.accountId,
    userId: config.userId,
    agentId: config.agentId,
    command,
    pre_category: category,
    workspaceRoot: config.workspaceRoot,
    cwd: String(event?.cwd || process.cwd()),
  };
  await fs.writeFile(payloadPath, JSON.stringify(payload), "utf8");
  await fs.writeFile(
    wrapperPath,
    `#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request


def post_json(path: str, body: dict, timeout: float = 30.0) -> dict:
    url = (os.environ.get("CONTEXTSNIPER_URL") or ${JSON.stringify(config.contextsniperUrl)}).rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def main() -> int:
    payload = json.loads(open(sys.argv[1], "r", encoding="utf-8").read())
    proc = subprocess.run(
        payload.get("command") or "",
        shell=True,
        executable="/bin/bash",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    body = dict(payload)
    body["output"] = proc.stdout or ""
    body["exit_code"] = proc.returncode
    try:
        result = post_json("/api/v1/filter_bash", body, timeout=float(os.environ.get("CONTEXTSNIPER_FILTER_BASH_TIMEOUT", "30")))
    except Exception:
        sys.stdout.write(proc.stdout or "")
        return proc.returncode
    if result.get("ok") and result.get("filtered_output"):
        sys.stdout.write(str(result["filtered_output"]))
    else:
        sys.stdout.write(proc.stdout or "")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
`,
    "utf8",
  );
  await fs.chmod(wrapperPath, 0o755);

  const rewritten = `${shellQuote(pythonBin())} ${shellQuote(wrapperPath)} ${shellQuote(payloadPath)}`;
  logger.info?.(`contextsniper: wrapped exec category=${category} command=${command.slice(0, 120)}`);
  return {
    params: {
      ...params,
      [commandParamName(params)]: rewritten,
    },
  };
}

export function registerFilterHooks(
  api: any,
  config: ResolvedConfig,
  ensureBackend: () => Promise<void>,
  logger: Logger,
): void {
  if (typeof api.on !== "function") return;

  api.on(
    "before_tool_call",
    async (event: any) => {
      if (event?.toolName === "read") return filterRead(event, config, ensureBackend, logger);
      if (event?.toolName === "exec") return filterExec(event, config, ensureBackend, logger);
      return undefined;
    },
    { priority: 100, timeoutMs: 15000 },
  );
}
