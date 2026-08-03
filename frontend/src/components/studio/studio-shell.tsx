"use client";

import { ArrowLeftIcon, PackageIcon, UploadIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  labelKey: "packages" | "import";
  icon: React.ComponentType<{ className?: string }>;
  /** Match prefix for active-state detection. */
  matchPrefix: string;
}

const NAV_ITEMS: NavItem[] = [
  {
    href: "/studio/packages",
    labelKey: "packages",
    icon: PackageIcon,
    matchPrefix: "/studio/packages",
  },
  {
    href: "/studio/import",
    labelKey: "import",
    icon: UploadIcon,
    matchPrefix: "/studio/import",
  },
];

export function StudioShell({ children }: { children: React.ReactNode }) {
  const { t } = useI18n();
  const pathname = usePathname();
  return (
    <div className="bg-background text-foreground flex min-h-svh flex-col">
      <header className="bg-card/95 supports-[backdrop-filter]:bg-card/60 sticky top-0 z-30 border-b backdrop-blur">
        <div className="mx-auto flex w-full max-w-(--container-width-lg) items-center gap-6 px-8 py-3">
          <Link
            href="/studio/packages"
            className="flex items-center gap-2 font-semibold"
          >
            <PackageIcon className="text-primary size-5" />
            <span>{t.studio.title}</span>
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
                  {t.studio.nav[item.labelKey]}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto">
            <Link
              href="/workspace"
              className="text-muted-foreground hover:bg-accent hover:text-accent-foreground inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors"
            >
              <ArrowLeftIcon className="size-4" />
              {t.studio.backToWorkspace}
            </Link>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-(--container-width-lg) flex-1 px-8 py-8">
        {children}
      </main>
    </div>
  );
}
