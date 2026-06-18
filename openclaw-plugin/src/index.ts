import { definePluginEntry } from "openclaw/plugin-sdk/core";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import type { ContextSniperPluginConfig } from "./config.ts";
import { resolveConfig } from "./config.ts";
import { findRepoRoot, currentPluginRoot } from "./paths.ts";
import { registerPolicyHook } from "./policy.ts";
import { ContextSniperService } from "./contextsniper-service.ts";
import { registerContextSniperTools } from "./tools.ts";
import { registerFilterHooks } from "./filter.ts";

function loadRepoEnvironment(repoRoot: string, logger: any): void {
  const rawToggle = String(process.env.CONTEXTSNIPER_OPENCLAW_LOAD_ENV || "1").trim().toLowerCase();
  if (["0", "false", "no", "off"].includes(rawToggle)) return;

  const setupEnv = path.join(repoRoot, "setup_env.sh");
  if (!fs.existsSync(setupEnv)) return;

  const script = [
    "set -a",
    `source ${JSON.stringify(setupEnv)} >/dev/null`,
    "env -0",
  ].join("; ");
  const result = spawnSync("bash", ["-lc", script], {
    cwd: repoRoot,
    encoding: "buffer",
    maxBuffer: 4 * 1024 * 1024,
  });
  if (result.status !== 0 || !result.stdout) {
    logger?.warn?.(`contextsniper: could not load setup_env.sh for OpenClaw plugin`);
    return;
  }

  for (const entry of result.stdout.toString("utf8").split("\0")) {
    if (!entry) continue;
    const index = entry.indexOf("=");
    if (index <= 0) continue;
    const key = entry.slice(0, index);
    if (["PWD", "OLDPWD", "SHLVL"].includes(key)) continue;
    const value = entry.slice(index + 1);
    process.env[key] = value;
  }
}

export default definePluginEntry({
  id: "contextsniper",
  name: "ContextSniper",
  description: "Code search and exact-replacement edits backed by ContextSniper.",
  register(api: any) {
    const pluginRoot = currentPluginRoot(import.meta.url);
    const pluginConfig = (api.pluginConfig ?? {}) as ContextSniperPluginConfig;
    const repoRoot = findRepoRoot(pluginRoot, pluginConfig.repoRoot);
    loadRepoEnvironment(repoRoot, api.logger ?? console);
    const config = resolveConfig(pluginConfig, pluginRoot, repoRoot);
    const service = new ContextSniperService(config, api.logger ?? console);

    if (config.autoStart) {
      void service.start().catch((error) => {
        api.logger?.warn?.(`contextsniper: backend auto-start failed: ${String(error)}`);
      });
    }

    registerContextSniperTools(api, config, async () => {
      if (config.autoStart) await service.start();
    });
    registerPolicyHook(api, config, async () => {
      if (config.autoStart) await service.start();
    });
    registerFilterHooks(api, config, async () => {
      if (config.autoStart) await service.start();
    }, api.logger ?? console);
    if (typeof api.on === "function") {
      if (config.readToolPolicy === "guard") {
        api.on("before_tool_call", (event: any) => {
          if (event?.toolName !== "read") return undefined;
          return {
            block: true,
            blockReason:
              "Native read is disabled by ContextSniper for this run. Use contextsniper_search_code results as code context; retry with a focused query when more exact text is needed.",
          };
        });
      }
      api.on("before_tool_call", (event: any) => {
        if (event?.toolName !== "edit") return undefined;
        return {
          block: true,
          blockReason:
            "Native edit is disabled by ContextSniper for this run. Use contextsniper_edit_file with old_string copied from contextsniper_search_code content_excerpt or a narrow native read result.",
        };
      });
    }

    api.logger?.info?.(
      `contextsniper: registered tools for workspace ${config.workspaceRoot} using ${config.contextsniperUrl}`,
    );
  },
});
