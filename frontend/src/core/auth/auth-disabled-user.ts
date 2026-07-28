import type { User } from "./types";

export const AUTH_DISABLED_USER: User = {
  id: "default",
  email: "default@test.local",
  system_role: "admin",
  needs_setup: false,
  // Auth-disabled mode stamps a system-admin stub user. /me surfaces
  // compute_permissions_for_user which short-circuits to SYSTEM_PERMISSIONS
  // (the system:org:* set) for system_role=admin, scoped to the default Org.
  // This fixture MUST match the real backend /me response — the
  // auth-disabled-contract e2e locks the two together via deep-equal.
  effective_permissions: [
    "system:org:create",
    "system:org:operate_all",
    "system:org:read_all",
  ],
  org_id: "default",
};

const PRODUCTION_ENV_VALUES = new Set(["prod", "production"]);

function isExplicitProductionEnvironment() {
  return ["DEER_FLOW_ENV", "ENVIRONMENT"].some((name) =>
    PRODUCTION_ENV_VALUES.has((process.env[name] ?? "").trim().toLowerCase()),
  );
}

export function isAuthDisabledMode() {
  return (
    process.env.DEER_FLOW_AUTH_DISABLED === "1" &&
    !isExplicitProductionEnvironment()
  );
}
