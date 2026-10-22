/**
 * Which upstream API v1 calls the browser may make through /api/proxy.
 * Anything else is refused, so a compromised page script cannot use the
 * session to reach admin endpoints (tokens, jobs, …).
 */
const GET_ALLOW: readonly RegExp[] = [
  /^whoami$/,
  /^projects$/,
  /^tickets$/,
  /^runs$/,
  /^approvals$/,
  /^approvals\/[A-Za-z0-9._:-]{1,128}$/,
  /^costs$/,
  /^capacity$/,
  /^audit$/,
  /^audit\/verify$/,
  /^briefs\/latest$/,
];
const POST_ALLOW: readonly RegExp[] = [/^approvals\/[A-Za-z0-9._:-]{1,128}\/decision$/];

export const MAX_PROXY_BODY_BYTES = 16 * 1024;

export function isProxyAllowed(method: string, path: string): boolean {
  if (path.includes("..") || path.includes("//")) return false;
  const list = method === "GET" ? GET_ALLOW : method === "POST" ? POST_ALLOW : [];
  return list.some((re) => re.test(path));
}
