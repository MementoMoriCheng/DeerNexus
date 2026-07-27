import { redirect } from "next/navigation";
import { Toaster } from "sonner";

import { QueryClientProvider } from "@/components/query-client-provider";
import { StudioShell } from "@/components/studio/studio-shell";
import { GatewayOfflineFallback } from "@/components/workspace/gateway-offline-fallback";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever } from "@/core/auth/types";

export const dynamic = "force-dynamic";

export default async function StudioLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const result = await getServerSideUser();

  switch (result.tag) {
    case "authenticated": {
      // Studio is the Agent artifact & release console (Track E PR-057).
      // OPEN entry: any authenticated user reaches the segment. The backend
      // `studio:*` RBAC is the authoritative gate (org:admin all / developer
      // read + dev promote / viewer read-only). The frontend does NOT mirror
      // permissions into the User object — a user lacking a permission sees the
      // UI but a write call returns 403, surfaced as a toast. Per-permission
      // button gating (passing effective_permissions) is a follow-up.
      return (
        <AuthProvider initialUser={result.user}>
          <QueryClientProvider>
            <StudioShell>{children}</StudioShell>
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
      redirect("/login?next=/studio/packages");
    case "gateway_unavailable":
      return (
        <GatewayOfflineFallback>
          <StudioShell>{children}</StudioShell>
        </GatewayOfflineFallback>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
}
