/**
 * Environment variable access for the frontend.
 *
 * Previously used @t3-oss/env-nextjs for schema validation, but its internal
 * use of the zod `~standard` protocol is incompatible with zod v4 (throws
 * "expected nonoptional, received undefined" on optional env vars at SSR).
 * Since all env vars here are optional strings, direct process.env access is
 * simpler and avoids the t3-env + zod v4 coupling. Type-safe wrappers ensure
 * the access patterns remain consistent.
 */

function str(value, defaultValue) {
  if (!value || value.trim() === "") return defaultValue;
  return value;
}

export const env = {
  GITHUB_OAUTH_TOKEN: process.env.GITHUB_OAUTH_TOKEN || undefined,
  NODE_ENV: str(process.env.NODE_ENV, "development"),

  NEXT_PUBLIC_BACKEND_BASE_URL:
    process.env.NEXT_PUBLIC_BACKEND_BASE_URL || undefined,
  NEXT_PUBLIC_LANGGRAPH_BASE_URL:
    process.env.NEXT_PUBLIC_LANGGRAPH_BASE_URL || undefined,
  NEXT_PUBLIC_STATIC_WEBSITE_ONLY:
    process.env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY || undefined,
};
