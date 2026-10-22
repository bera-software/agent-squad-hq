// Runs the `output: "standalone"` server after copying the assets it does not
// include (.next/static and public/), mirroring what the Dockerfile does.
// Usage: npm run build && npm start   (PORT / HOSTNAME env as usual)
import { cpSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// outputFileTracingRoot is the apps/ workspace root, so the server lives under dashboard/.
const distDir = process.env.NEXT_DIST_DIR || ".next";
const appDir = path.join(root, distDir, "standalone", "dashboard");
const server = path.join(appDir, "server.js");

if (!existsSync(server)) {
  console.error(`standalone server not found at ${server}; run \`npm run build\` first`);
  process.exit(1);
}
cpSync(path.join(root, distDir, "static"), path.join(appDir, distDir, "static"), { recursive: true });
if (existsSync(path.join(root, "public"))) {
  cpSync(path.join(root, "public"), path.join(appDir, "public"), { recursive: true });
}

const child = spawn(process.execPath, [server], { stdio: "inherit", env: process.env });
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => child.kill(sig));
child.on("exit", (code, signal) => process.exit(signal ? 1 : (code ?? 0)));
