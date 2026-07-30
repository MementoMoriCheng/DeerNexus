"use client";

import { useMemo } from "react";
import type { AnchorHTMLAttributes } from "react";
import type { ExtraProps } from "streamdown";

import {
  MessageResponse,
  type MessageResponseProps,
} from "@/components/ai-elements/message";
import {
  preprocessStreamdownMarkdown,
  streamdownPlugins,
} from "@/core/streamdown";
import { cn } from "@/lib/utils";

import { CitationLink } from "../citations/citation-link";

function isExternalUrl(href: string | undefined): boolean {
  return !!href && /^https?:\/\//.test(href);
}

export type MarkdownContentProps = {
  content: string;
  isLoading: boolean;
  rehypePlugins: MessageResponseProps["rehypePlugins"];
  className?: string;
  remarkPlugins?: MessageResponseProps["remarkPlugins"];
  components?: MessageResponseProps["components"];
  plugins?: MessageResponseProps["plugins"];
};

/** Renders markdown content. */
export function MarkdownContent({
  content,
  rehypePlugins,
  className,
  remarkPlugins = streamdownPlugins.remarkPlugins,
  components: componentsFromProps,
  // Default to the streamdown v2 companion plugins (Mermaid diagrams + Shiki code
  // highlighting) so renderers that only customize rehypePlugins/components still
  // get diagrams + syntax highlighting. v2 unbundled these into opt-in plugins, so
  // omitting plugins would leave code blocks unhighlighted and mermaid as plain text.
  plugins = streamdownPlugins.plugins,
}: MarkdownContentProps) {
  const normalizedContent = useMemo(
    () => preprocessStreamdownMarkdown(content),
    [content],
  );
  const components = useMemo(() => {
    return {
      a: (props: AnchorHTMLAttributes<HTMLAnchorElement> & ExtraProps) => {
        if (typeof props.children === "string") {
          const match = /^citation:(.+)$/.exec(props.children);
          if (match) {
            const [, text] = match;
            return <CitationLink {...props}>{text}</CitationLink>;
          }
        }
        // streamdown v2 injects a hast `node` prop into custom component overrides;
        // strip it so it isn't forwarded onto the native <a> (React "unknown prop").
        const { className, target, rel, node: _node, ...rest } = props;
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
      },
      ...componentsFromProps,
    };
  }, [componentsFromProps]);

  if (!content) return null;

  return (
    <MessageResponse
      className={className}
      remarkPlugins={remarkPlugins}
      rehypePlugins={rehypePlugins}
      components={components}
      plugins={plugins}
    >
      {normalizedContent}
    </MessageResponse>
  );
}
