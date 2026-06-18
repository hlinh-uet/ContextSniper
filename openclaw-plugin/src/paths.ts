import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export function currentPluginRoot(importMetaUrl: string): string {
  const file = fileURLToPath(importMetaUrl);
  let dir = path.dirname(file);
  if (path.basename(dir) === "src" || path.basename(dir) === "dist") {
    dir = path.dirname(dir);
  }
  return dir;
}

export function findRepoRoot(pluginRoot: string, configured?: string): string {
  const candidates = [];
  if (configured) candidates.push(path.resolve(configured));
  if (process.env.CONTEXTSNIPER_SOURCE_TREE) candidates.push(path.resolve(process.env.CONTEXTSNIPER_SOURCE_TREE));
  candidates.push(path.dirname(pluginRoot));
  candidates.push(process.cwd());

  for (const start of candidates) {
    let cursor = start;
    for (;;) {
      if (
        fs.existsSync(path.join(cursor, "claude-plugin", "scripts", "contextsniper_terminal.py")) &&
        fs.existsSync(path.join(cursor, "server", "app.py"))
      ) {
        return cursor;
      }
      const parent = path.dirname(cursor);
      if (parent === cursor) break;
      cursor = parent;
    }
  }

  return path.dirname(pluginRoot);
}

export function resolveInside(root: string, filePath: string): string {
  const target = path.resolve(root, remapOpenClawWorkspaceMirror(root, filePath));
  const relative = path.relative(root, target);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`Path is outside workspace root: ${filePath}`);
  }
  return target;
}

function remapOpenClawWorkspaceMirror(root: string, filePath: string): string {
  if (!path.isAbsolute(filePath)) return filePath;

  const rootParts = path.resolve(root).split(path.sep).filter(Boolean);
  const fileParts = path.resolve(filePath).split(path.sep).filter(Boolean);

  for (let suffixStart = 0; suffixStart < rootParts.length; suffixStart += 1) {
    const suffix = rootParts.slice(suffixStart);
    if (suffix.length < 2) continue;
    const index = indexOfParts(fileParts, suffix);
    if (index === -1) continue;
    const rest = fileParts.slice(index + suffix.length);
    return path.join(root, ...rest);
  }

  return filePath;
}

function indexOfParts(parts: string[], needle: string[]): number {
  outer:
  for (let index = 0; index <= parts.length - needle.length; index += 1) {
    for (let offset = 0; offset < needle.length; offset += 1) {
      if (parts[index + offset] !== needle[offset]) continue outer;
    }
    return index;
  }
  return -1;
}
