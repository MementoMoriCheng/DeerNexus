import { redirect } from "next/navigation";

export default function AdminIndexPage(): never {
  // Default admin landing — Runs is the most useful daily entry point.
  redirect("/admin/runs");
}
