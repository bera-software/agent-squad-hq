"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export const NAV_ITEMS = [
  { href: "/", label: "Overview" },
  { href: "/pipeline", label: "Pipeline" },
  { href: "/approvals", label: "Approvals" },
  { href: "/runs", label: "Runs" },
  { href: "/costs", label: "Costs" },
  { href: "/accounts", label: "Accounts" },
  { href: "/audit", label: "Audit log" },
  { href: "/brief", label: "Daily brief" },
  { href: "/settings", label: "Settings" },
] as const;

export function Nav() {
  const pathname = usePathname() ?? "/";
  return (
    <nav className="nav" aria-label="Main">
      <ul>
        {NAV_ITEMS.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <li key={item.href}>
              <Link href={item.href} aria-current={active ? "page" : undefined}>
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
