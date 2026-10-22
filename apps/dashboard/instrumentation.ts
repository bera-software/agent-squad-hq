/** Runs once when the Next.js server starts. */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { configWarnings } = await import("./lib/config");
  for (const w of configWarnings()) console.warn(`[squadhq-dashboard] WARNING: ${w}`);
}
