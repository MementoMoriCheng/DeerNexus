import { redirect } from "next/navigation";

// /studio → default landing on the packages list.
export default function StudioIndexPage() {
  redirect("/studio/packages");
}
