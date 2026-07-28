import type { User } from "./types";

export const STATIC_WEBSITE_USER: User = {
  id: "static-website-user",
  email: "static@example.local",
  system_role: "admin",
  needs_setup: false,
  // Static-website mode: full studio perms so the (read-only) site renders
  // without spurious button-disabling.
  effective_permissions: [
    "studio:package:read",
    "studio:package:write",
    "studio:release:promote_dev",
    "studio:release:promote",
    "studio:release:rollback",
  ],
  org_id: null,
};
