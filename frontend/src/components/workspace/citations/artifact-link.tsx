import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

import { CitationLink } from "./citation-link";

function isExternalUrl(href: string | undefined): boolean {
  return !!href && /^https?:\/\//.test(href);
}

/**
 * Props shape compatible with streamdown v2's ``Components['a']`` mapping,
 * which passes ``Record<string, unknown> & ExtraProps`` (ExtraProps carries
 * an optional ``node``). Using ``ComponentProps<"a">`` keeps the element
 * attributes while the extra ``node`` field is accepted and ignored.
 */
type StreamdownAnchorProps = ComponentProps<"a"> & { node?: unknown };

/** Link renderer for artifact markdown: citation: prefix → CitationLink, otherwise underlined text. */
export function ArtifactLink(props: StreamdownAnchorProps) {
  if (typeof props.children === "string") {
    const match = /^citation:(.+)$/.exec(props.children);
    if (match) {
      const [, text] = match;
      return <CitationLink {...props}>{text}</CitationLink>;
    }
  }
  const { className, target, rel, ...rest } = props;
  const external = isExternalUrl(props.href);
  return (
    <a
      {...rest}
      className={cn(
        "text-primary decoration-primary/30 hover:decoration-primary/60 underline underline-offset-2 transition-colors",
        className,
      )}
      target={target ?? (external ? "_blank" : undefined)}
      rel={rel ?? (external ? "noopener noreferrer" : undefined)}
    />
  );
}
