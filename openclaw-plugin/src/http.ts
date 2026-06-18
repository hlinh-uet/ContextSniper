import type { ResolvedConfig } from "./config.ts";

export interface IdentityFields {
  accountId: string;
  userId: string;
  agentId: string;
  sessionId?: string;
}

export function identityFields(sessionId?: string): IdentityFields {
  const out: IdentityFields = {
    accountId: "acct-demo",
    userId: "u-openclaw",
    agentId: "openclaw",
  };
  const sid = sessionId;
  if (sid) out.sessionId = sid;
  return out;
}

function headers(): Record<string, string> {
  return { "Content-Type": "application/json" };
}

async function waitForBackend(config: ResolvedConfig): Promise<void> {
  const timeoutMs = 60000;
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${config.contextsniperUrl}/api/v1/health`, {
        method: "GET",
        headers: headers(),
        signal: AbortSignal.timeout(2000),
      });
      if (response.ok) return;
      lastError = new Error(`health ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(`ContextSniper backend is unavailable at ${config.contextsniperUrl}: ${String(lastError)}`);
}

export async function getJson(config: ResolvedConfig, route: string, timeoutMs = 10000): Promise<unknown> {
  const response = await fetch(`${config.contextsniperUrl}${route}`, {
    method: "GET",
    headers: headers(),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`GET ${route} failed: ${response.status} ${text}`);
  return text.trim() ? JSON.parse(text) : {};
}

export async function postJson(
  config: ResolvedConfig,
  route: string,
  body: Record<string, unknown>,
  timeoutMs = 30000,
): Promise<unknown> {
  await waitForBackend(config);
  const response = await fetch(`${config.contextsniperUrl}${route}`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`POST ${route} failed: ${response.status} ${text}`);
  return text.trim() ? JSON.parse(text) : {};
}
