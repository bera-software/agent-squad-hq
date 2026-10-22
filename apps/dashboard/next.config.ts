import path from "node:path";
import type { NextConfig } from "next";

/**
 * Static security headers. The page Content-Security-Policy carries a per-request
 * nonce, so it is set in middleware.ts; API routes (JSON only) get a locked-down
 * static CSP here.
 */
const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  // Optional separate build dir (e.g. to build while `next dev` uses .next). Default .next.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  // The workspace root (apps/) so standalone output traces @squadhq/shared-api.
  outputFileTracingRoot: path.join(__dirname, ".."),
  transpilePackages: ["@squadhq/shared-api"],
  poweredByHeader: false,
  reactStrictMode: true,
  eslint: { ignoreDuringBuilds: true }, // `npm run lint` runs separately in CI
  async headers() {
    return [
      { source: "/:path*", headers: securityHeaders },
      {
        source: "/api/:path*",
        headers: [
          { key: "Content-Security-Policy", value: "default-src 'none'; frame-ancestors 'none'" },
          { key: "Cache-Control", value: "no-store" },
        ],
      },
    ];
  },
};

export default nextConfig;
