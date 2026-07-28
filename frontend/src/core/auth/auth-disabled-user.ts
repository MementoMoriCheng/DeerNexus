import type { User } from "./types";

export const AUTH_DISABLED_USER: User = {
  id: "default",
  email: "default@test.local",
  system_role: "admin",
  needs_setup: false,
  // Auth-disabled mode bypasses RBAC; surface full studio perms so the UI is
  // fully usable (matches the system-admin short-circuit on the backend).
  effective_permissions: [
    "studio:package:read",
    "studio:package:write",
    "studio:release:promote_dev",
    "studio:release:promote",
    "studio:release:rollback",
  ],
  org_id: null,
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
