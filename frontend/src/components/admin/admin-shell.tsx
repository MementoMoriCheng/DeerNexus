"use client";

import {
  ActivityIcon,
  AlertTriangleIcon,
  BarChart3Icon,
  ShieldCheckIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  /** Match prefix for active-state detection. */
  matchPrefix: string;
}

const NAV_ITEMS: NavItem[] = [
  {
    href: "/admin/runs",
    label: "Runs",
    icon: ActivityIcon,
    matchPrefix: "/admin/runs",
  },
  {
    href: "/admin/usage",
    label: "Usage",
    icon: BarChart3Icon,
    matchPrefix: "/admin/usage",
  },
  {
    href: "/admin/audit",
    label: "Failure / Audit",
    icon: AlertTriangleIcon,
    matchPrefix: "/admin/audit",
  },
];

export function AdminShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="bg-background text-foreground flex min-h-svh flex-col">
      <header className="bg-card/95 supports-[backdrop-filter]:bg-card/60 sticky top-0 z-30 border-b backdrop-blur">
        <div className="mx-auto flex w-full max-w-(--container-width-lg) items-center gap-6 px-8 py-3">
          <Link
            href="/admin/runs"
            className="flex items-center gap-2 font-semibold"
          >
            <ShieldCheckIcon className="text-primary size-5" />
            <span>Admin Console</span>
          </Link>
          <nav className="flex items-center gap-1">
            {NAV_ITEMS.map((item) => {
              const isActive = pathname.startsWith(item.matchPrefix);
              const Icon = item.icon;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    isActive
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                  )}
                >
                  <Icon className="size-4" />
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>
      </header>
      <main className="mx-auto w-full max-w-(--container-width-lg) flex-1 px-8 py-8">
        {children}
      </main>
    </div>
  );
}
