import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ResolvedConfig } from "./config.ts";
import { identityFields, postJson, getJson } from "./http.ts";
import { resolveInside } from "./paths.ts";

const IDENT = "[A-Za-z_][A-Za-z0-9_]*";

function canonicalCodeQuery(query: string): string {
  const q = (query || "").trim();
  const patterns: Array<[RegExp, (symbol: string) => string]> = [
    [new RegExp(`^function\\s+(${IDENT})$`, "i"), (symbol) => `${symbol} function`],
    [new RegExp(`^def\\s+(${IDENT})$`, "i"), (symbol) => `${symbol} function`],
    [new RegExp(`^async\\s+function\\s+(${IDENT})$`, "i"), (symbol) => `${symbol} async function`],
    [new RegExp(`^async\\s+def\\s+(${IDENT})$`, "i"), (symbol) => `${symbol} async function`],
    [new RegExp(`^type\\s+(${IDENT})$`, "i"), (symbol) => `${symbol} type`],
  ];
  for (const [pattern, rewrite] of patterns) {
    const match = pattern.exec(q);
    if (match) return rewrite(match[1]);
  }
  return q;
}

function workspaceRoot(config: ResolvedConfig, requested?: string): string {
  const raw = (requested || "").trim();
  if (!raw || ["$WORK_DIR", "${WORK_DIR}", "$PWD", "${PWD}", "$CONTEXTSNIPER_WORKSPACE_ROOT", "${CONTEXTSNIPER_WORKSPACE_ROOT}"].includes(raw)) {
    return config.workspaceRoot;
  }
  return path.resolve(raw);
}

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function toolResult(value: unknown): unknown {
  const text = typeof value === "string" ? value : prettyJson(value);
  return {
    content: [{ type: "text", text }],
  };
}

const recentContextSniperHitFiles = new Set<string>();

function normalizeHitPath(root: string, item: Record<string, unknown>): string | null {
  const values = [item.uri, item.file_path, item.path, item.relative_path];
  for (const value of values) {
    const raw = String(value || "");
    if (!raw) continue;
    try {
      const candidate = raw.startsWith("file://") ? fileURLToPath(raw) : raw;
      return path.isAbsolute(candidate) ? resolveInside(root, candidate) : resolveInside(root, candidate);
    } catch {
      // Try the next path-like field.
    }
  }
  return null;
}

function rememberContextSniperHitFiles(root: string, value: unknown): void {
  const data = asRecord(value);
  const hits = Array.isArray(data?.hits) ? data.hits : [];
  for (const hit of hits) {
    const item = asRecord(hit);
    if (!item) continue;
    const fullPath = normalizeHitPath(root, item);
    if (fullPath) recentContextSniperHitFiles.add(fullPath);
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function slimHit(hit: unknown): Record<string, unknown> | null {
  const item = asRecord(hit);
  if (!item) return null;
  const uri = String(item.uri || item.file_path || item.path || "");
  const relativePath = String(item.relative_path || item.path || "");
  const content = String(item.content_excerpt || item.snippet || item.text || "");
  if (!content) return null;
  const slim: Record<string, unknown> = {
    content_excerpt: content,
  };
  if (uri) slim.uri = uri;
  if (relativePath) slim.path = relativePath;
  if (item.start_line !== undefined) slim.start_line = item.start_line;
  if (item.end_line !== undefined) slim.end_line = item.end_line;
  if (item.source !== undefined) slim.source = item.source;
  return slim;
}

function slimSearchResponse(response: unknown, localSnippets: unknown[] = [], limit?: number): unknown {
  if (searchDebugEnabled()) return response;

  const data = asRecord(response);
  if (!data) return response;
  if (data.ok === false || data.error) {
    return {
      ok: data.ok ?? false,
      error: data.error || "contextsniper_search_code failed",
    };
  }

  const maxHits = Math.max(1, Number(limit || hitsDefaultLimit()));
  const seen = new Set<string>();
  const rawHits = Array.isArray(data.hits) ? data.hits : [];
  const hits = [...rawHits, ...localSnippets]
    .map(slimHit)
    .filter((hit): hit is Record<string, unknown> => {
      if (!hit) return false;
      const key = String(hit.uri || hit.path || hit.content_excerpt || "");
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, maxHits);

  return {
    hit_count: hits.length,
    hits,
  };
}

function hitsDefaultLimit(): number {
  const parsed = Number(process.env.CONTEXTSNIPER_SEARCH_LIMIT || 5);
  return Number.isFinite(parsed) ? parsed : 5;
}

function searchDebugEnabled(): boolean {
  return ["1", "true", "yes", "on"].includes((process.env.CONTEXTSNIPER_SEARCH_DEBUG || "").toLowerCase()) ||
    ["1", "true", "yes", "on"].includes((process.env.CONTEXTSNIPER_SEARCH_INCLUDE_DEBUG || "").toLowerCase());
}

function formatSearchSnippets(result: unknown, query?: string): string {
  if (searchDebugEnabled()) return prettyJson(result);

  const data = asRecord(result);
  if (!data) return typeof result === "string" ? result : prettyJson(result);
  if (data.ok === false || data.error) return prettyJson(result);

  const lines: string[] = [];
  const searchQuery = String(data.query || query || "").trim();
  if (searchQuery) lines.push(`# search_code query: ${searchQuery}`);

  const hits = Array.isArray(data.hits) ? data.hits.filter((hit) => asRecord(hit)) : [];
  lines.push(`# hit_count: ${hits.length}`);

  hits.forEach((hit, index) => {
    const item = asRecord(hit);
    if (!item) return;
    const excerpt = String(item.content_excerpt || item.abstract || "").trim();
    if (!excerpt) return;
    lines.push("");
    lines.push(`# hit ${index + 1}`);
    lines.push(excerpt);
  });

  return `${lines.join("\n").trimEnd()}\n`;
}

function paramsFromArgs<T>(arg1: unknown, arg2: unknown): T {
  return ((arg2 ?? arg1 ?? {}) as T);
}

function splitCsv(value?: string): string[] {
  return (value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function globToRegex(pattern: string): RegExp {
  let out = "^";
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i];
    const next = pattern[i + 1];
    if (ch === "*" && next === "*") {
      out += ".*";
      i += 1;
    } else if (ch === "*") {
      out += "[^/]*";
    } else if ("\\^$+?.()|{}[]".includes(ch)) {
      out += `\\${ch}`;
    } else {
      out += ch;
    }
  }
  return new RegExp(`${out}$`);
}

async function walkFiles(root: string, maxFiles = 2000): Promise<string[]> {
  const files: string[] = [];
  const stack = [root];
  const skip = new Set([".git", ".hg", ".svn", "node_modules", ".tox", ".venv", "venv", "__pycache__"]);
  while (stack.length && files.length < maxFiles) {
    const dir = stack.pop()!;
    let entries;
    try {
      entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!skip.has(entry.name)) stack.push(full);
      } else if (entry.isFile()) {
        files.push(full);
        if (files.length >= maxFiles) break;
      }
    }
  }
  return files;
}

async function filesForGlobs(root: string, patterns: string[]): Promise<string[]> {
  if (!patterns.length) return [];
  const literalFiles: string[] = [];
  const globPatterns: string[] = [];
  for (const pattern of patterns) {
    if (!pattern.includes("*")) {
      const target = resolveInside(root, pattern);
      try {
        const stat = await fs.stat(target);
        if (stat.isFile()) literalFiles.push(target);
      } catch {
        // Fall through to normal glob matching for patterns that are not literal files.
        globPatterns.push(pattern);
      }
    } else {
      globPatterns.push(pattern);
    }
  }
  if (!globPatterns.length) return literalFiles;
  const regexes = globPatterns.map(globToRegex);
  const walked = await walkFiles(root);
  const matched = walked.filter((file) => {
    const relative = path.relative(root, file).split(path.sep).join("/");
    return regexes.some((regex) => regex.test(relative));
  });
  return Array.from(new Set([...literalFiles, ...matched]));
}

function termsForLocalSearch(query: string, grepTerms?: string): string[] {
  const explicit = splitCsv(grepTerms);
  if (explicit.length) return explicit;
  return Array.from(
    new Set(
      (query.match(/[A-Za-z_][A-Za-z0-9_]{2,}|0o[0-7]+|`[^`]+`/g) || [])
        .map((term) => term.replace(/^`|`$/g, ""))
        .filter((term) => term.length >= 3),
    ),
  ).slice(0, 8);
}

async function localSnippetFallback(root: string, query: string, globPatterns?: string, grepTerms?: string): Promise<unknown[]> {
  const patterns = splitCsv(globPatterns);
  const terms = termsForLocalSearch(query, grepTerms);
  if (!patterns.length && !terms.length) return [];

  const files = patterns.length ? await filesForGlobs(root, patterns) : await walkFiles(root, 3000);
  const lowerTerms = terms.map((term) => term.toLowerCase());
  const codeLike = new Set([
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".cs", ".swift", ".kt", ".scala", ".sh", ".txt", ".rst", ".md",
  ]);
  const rankedFiles = files
    .filter((file) => codeLike.has(path.extname(file).toLowerCase()))
    .map((file) => {
      const rel = path.relative(root, file).split(path.sep).join("/").toLowerCase();
      const score = lowerTerms.reduce((total, term) => total + (rel.includes(term) ? 1 : 0), 0);
      return { file, score };
    })
    .sort((a, b) => b.score - a.score);
  const candidates = rankedFiles.map((item) => item.file).slice(0, patterns.length ? 80 : 300);
  const hits: unknown[] = [];
  for (const file of candidates) {
    let stat;
    try {
      stat = await fs.stat(file);
    } catch {
      continue;
    }
    if (stat.size > 2_000_000) continue;
    let text;
    try {
      text = await fs.readFile(file, "utf8");
    } catch {
      continue;
    }
    const lines = text.split(/\r?\n/);
    let matchLine = 0;
    if (lowerTerms.length) {
      matchLine = lines.findIndex((line) => {
        const lower = line.toLowerCase();
        return lowerTerms.some((term) => lower.includes(term));
      });
      if (matchLine < 0) continue;
    }
    const start = Math.max(0, matchLine - 10);
    const end = Math.min(lines.length, matchLine + 35);
    hits.push({
      uri: file,
      relative_path: path.relative(root, file).split(path.sep).join("/"),
      start_line: start + 1,
      end_line: end,
      matched_terms: terms,
      content_excerpt: lines.slice(start, end).join("\n"),
      source: "openclaw-plugin-local-snippet-fallback",
    });
    if (hits.length >= 8) break;
  }
  return hits;
}

async function executeContextSniperReadTool(config: ResolvedConfig, params: { path?: string; offset?: number; limit?: number }): Promise<unknown> {
  const requested = String(params.path || "");
  if (!requested) {
    throw new Error("read requires path");
  }
  const target = resolveInside(config.workspaceRoot, requested);

  if (config.readToolPolicy === "guard" && recentContextSniperHitFiles.has(target)) {
    return toolResult({
      ok: false,
      blocked_by: "contextsniper-read-guard",
      path: target,
      reason:
        "contextsniper_search_code already returned this file with usable code snippets in this run. Use those content_excerpt lines as context and call contextsniper_edit_file directly. If the snippet is insufficient, run one more focused contextsniper_search_code query for the exact symbol/heading/nearby phrase before doing a narrow read.",
      next_step: "Use contextsniper_edit_file with old_string copied from contextsniper_search_code.content_excerpt, or retry contextsniper_search_code with a more focused query.",
    });
  }

  const text = await fs.readFile(target, "utf8");
  const lines = text.split(/\r?\n/);
  const start = Math.max(0, Number(params.offset || 1) - 1);
  const end = params.limit ? Math.min(lines.length, start + Math.max(1, Number(params.limit))) : lines.length;
  const selected = lines.slice(start, end).join("\n");
  return toolResult(selected);
}

export function registerContextSniperTools(api: any, config: ResolvedConfig, ensureBackend?: () => Promise<void>): void {
  const register = (tool: Record<string, unknown>, names: string[]) => {
    api.registerTool(tool, { names, name: names[0] });
  };

  register(
    {
      name: "contextsniper_health",
      label: "ContextSniper Health",
      description: "Check whether the local ContextSniper backend is healthy.",
      parameters: { type: "object", additionalProperties: false, properties: {} },
      async execute() {
        await ensureBackend?.();
        return toolResult(await getJson(config, "/api/v1/health", 5000));
      },
    },
    ["contextsniper_health"],
  );

  register(
    {
      name: "contextsniper_index_codebase",
      label: "ContextSniper Index Codebase",
      description:
        "Warm code indexing for a workspace. Normal contextsniper_search_code calls also index candidates automatically.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          path: { type: "string", description: "Workspace root. Leave empty for the configured workspace." },
          force: { type: "boolean", description: "Reserved re-index flag." },
        },
      },
      async execute(arg1: unknown, arg2: unknown) {
        await ensureBackend?.();
        const params = paramsFromArgs<{ path?: string; force?: boolean }>(arg1, arg2);
        return toolResult({
          ok: true,
          skipped: true,
          reason: "contextsniper_search_code performs candidate indexing automatically",
          workspace_root: workspaceRoot(config, params.path),
          force: Boolean(params.force),
          next_step: "Call contextsniper_search_code with a focused query.",
        });
      },
    },
    ["contextsniper_index_codebase"],
  );

  register(
    {
      name: "contextsniper_search_code",
      label: "ContextSniper Search Code",
      description:
        "Search repository source, tests, docs, and release notes with ContextSniper. content_excerpt values are exact editable file text, not summaries; copy them directly into edit replacement context when possible. Do not re-read files already returned with usable snippets; if search misses or lacks exact context after a focused retry, use a narrow read.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          query: { type: "string", description: "Natural language, symbol, or keyword query." },
          path: { type: "string", description: "Workspace root. Leave empty for the configured workspace." },
          limit: { type: "number", minimum: 1, maximum: 100, description: "Max hits." },
          glob_patterns: { type: "string", description: "Optional comma-separated glob patterns, e.g. src/**/*.py or docs/**/*.txt." },
          grep_terms: { type: "string", description: "Optional comma-separated literal terms to require in results." },
        },
        required: ["query"],
      },
      async execute(arg1: unknown, arg2: unknown) {
        await ensureBackend?.();
        const params = paramsFromArgs<{
        query: string;
        path?: string;
        limit?: number;
        glob_patterns?: string;
        grep_terms?: string;
        }>(arg1, arg2);
        const body = {
          ...identityFields(config.sessionId),
          accountId: config.accountId,
          userId: config.userId,
          agentId: config.agentId,
          workspaceRoot: workspaceRoot(config, params.path),
          query: canonicalCodeQuery(params.query),
          limit: params.limit ?? config.searchLimit,
          glob_patterns: params.glob_patterns || null,
          grep_terms: params.grep_terms || null,
          waitForFullWorkspaceIndex: false,
        };
        const localSnippets = await localSnippetFallback(
          body.workspaceRoot,
          body.query,
          params.glob_patterns,
          params.grep_terms,
        );
        let response: unknown;
        try {
          response = await postJson(config, "/api/v1/call/code_semantic_search", body, 60000);
        } catch (error) {
          throw error;
        }
        const slim = slimSearchResponse(response, localSnippets, body.limit);
        rememberContextSniperHitFiles(body.workspaceRoot, slim);
        return toolResult(formatSearchSnippets(slim, body.query));
      },
    },
    ["contextsniper_search_code"],
  );

  register(
    {
      name: "contextsniper_edit_file",
      label: "ContextSniper Edit File",
      description:
        "Edit a file through exact string replacement after contextsniper_search_code. old_string must match exact text in the target file, similar to OpenClaw edit oldText. Normally build old_string by copying exact lines from contextsniper_search_code content_excerpt/local_snippet_fallback and call this directly without re-reading the same file. If exact text is missing after a focused search retry, use a narrow read.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          file_path: { type: "string", description: "Absolute or workspace-relative file path." },
          old_string: { type: "string", description: "Exact text to replace." },
          new_string: { type: "string", description: "Replacement text." },
          workspace_root: { type: "string", description: "Workspace root. Leave empty for the configured workspace." },
          replace_all: { type: "boolean", description: "Replace every occurrence." },
        },
        required: ["file_path", "old_string", "new_string"],
      },
      async execute(arg1: unknown, arg2: unknown) {
        await ensureBackend?.();
        const params = paramsFromArgs<{
        file_path: string;
        old_string: string;
        new_string: string;
        workspace_root?: string;
        replace_all?: boolean;
        }>(arg1, arg2);
        const root = workspaceRoot(config, params.workspace_root);
        const target = resolveInside(root, params.file_path);
        const original = await fs.readFile(target, "utf8");
        if (!original.includes(params.old_string)) {
          throw new Error("old_string not found in target file");
        }

        const occurrences = original.split(params.old_string).length - 1;
        if (!params.replace_all && occurrences !== 1) {
          throw new Error(
            `old_string matched ${occurrences} times; pass replace_all=true or provide a more specific old_string`,
          );
        }

        const updated = params.replace_all
          ? original.split(params.old_string).join(params.new_string)
          : original.replace(params.old_string, params.new_string);
        await fs.writeFile(target, updated, "utf8");

        let refresh: unknown;
        try {
          refresh = await postJson(
            config,
            "/api/v1/call/code_refresh_workspace_path",
            {
              ...identityFields(config.sessionId),
              accountId: config.accountId,
              userId: config.userId,
              agentId: config.agentId,
              workspaceRoot: root,
              file_path: target,
              wait_for_index: ["1", "true", "yes", "on"].includes((process.env.CONTEXTSNIPER_EDIT_REFRESH_WAIT || "0").toLowerCase()),
              refresh_timeout_sec: Number(process.env.CONTEXTSNIPER_EDIT_REFRESH_TIMEOUT_SEC || "5"),
            },
            8000,
          );
        } catch (error) {
          refresh = { ok: false, background_refresh_dispatched: false, error: String(error) };
        }

        const relativePath = path.relative(root, target);
        const editDebug = ["1", "true", "yes", "on"].includes((process.env.CONTEXTSNIPER_EDIT_DEBUG || "").toLowerCase());
        if (editDebug) {
          return toolResult({
            ok: true,
            file_path: target,
            relative_path: relativePath,
            replace_all: Boolean(params.replace_all),
            occurrences,
            bytes_before: Buffer.byteLength(original, "utf8"),
            bytes_after: Buffer.byteLength(updated, "utf8"),
            memory_refresh: refresh,
          });
        }

        const refreshData = asRecord(refresh);
        return toolResult({
          ok: true,
          relative_path: relativePath,
          occurrences,
          refresh_ok: refreshData ? Boolean(refreshData.ok) : false,
        });
      },
    },
    ["contextsniper_edit_file"],
  );
}
