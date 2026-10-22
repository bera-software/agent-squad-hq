import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { Nav } from "@/components/Nav";
import { isMockMode } from "@/lib/config";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Agent Squad HQ", template: "%s · Agent Squad HQ" },
  description: "Control plane for your AI software team.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f6f6f4" },
    { media: "(prefers-color-scheme: dark)", color: "#121211" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a className="skip-link" href="#main">
          Skip to content
        </a>
        <div className="shell">
          <aside className="sidebar">
            <p className="brand">Agent Squad HQ</p>
            {isMockMode() ? (
              <p className="mode-banner" role="status">
                Mock mode: showing sample data
              </p>
            ) : null}
            <Nav />
          </aside>
          <main id="main" tabIndex={-1}>
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
