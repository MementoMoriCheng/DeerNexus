import { redirect } from "next/navigation";
import { Toaster } from "sonner";

import { AdminShell } from "@/components/admin/admin-shell";
import { QueryClientProvider } from "@/components/query-client-provider";
import { GatewayOfflineFallback } from "@/components/workspace/gateway-offline-fallback";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever } from "@/core/auth/types";


export const dynamic = "force-dynamic";

export default async function AdminLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const result = await getServerSideUser();

  switch (result.tag) {
    case "authenticated": {
      // Admin Console is per-Org, read-only, gated on system_role === "admin".
      // Mirrors the backend's temporary `require_admin_user` gate (PR-060):
      // Track C RBAC (PR-030/031) will replace both with
      // @require_permission("admin", "console:read"). A regular user landing
      // here via direct URL is redirected to /workspace — no client flicker,
      // the check runs server-side.
      if (result.user.system_role !== "admin") {
        redirect("/workspace");
      }
      return (
        <AuthProvider initialUser={result.user}>
          <QueryClientProvider>
            <AdminShell>{children}</AdminShell>
            <Toaster position="top-center" />
          </QueryClientProvider>
        </AuthProvider>
      );
    }
    case "needs_setup":
      redirect("/setup");
    case "system_setup_required":
      redirect("/setup");
    case "unauthenticated":
      // Preserve the deep-link target so login returns here.
      redirect("/login?next=/admin/runs");
    case "gateway_unavailable":
      return (
        <GatewayOfflineFallback>
          <AdminShell>{children}</AdminShell>
        </GatewayOfflineFallback>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
}
