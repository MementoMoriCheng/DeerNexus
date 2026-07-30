import { code } from "@streamdown/code";
import { mermaid } from "@streamdown/mermaid";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import type { StreamdownProps } from "streamdown";

import { rehypeSplitWordsIntoSpans } from "../rehype";

// streamdown v2 unbundles Mermaid diagrams and Shiki code highlighting into
// opt-in companion plugins (@streamdown/mermaid, @streamdown/code). v1 had
// both built in, so we re-enable them here to preserve feature parity. The
// `mermaid` Streamdown prop only *configures* diagrams in v2; rendering itself
// requires passing a DiagramPlugin instance via `plugins.mermaid`. Likewise the
// `shikiTheme` prop is gone — Shiki themes are configured on the code plugin.
export const streamdownPlugins = {
  remarkPlugins: [
    remarkGfm,
    [remarkMath, { singleDollarTextMath: true }],
  ] as StreamdownProps["remarkPlugins"],
  rehypePlugins: [
    rehypeRaw,
    [rehypeKatex, { output: "html" }],
  ] as StreamdownProps["rehypePlugins"],
  plugins: { mermaid, code } as StreamdownProps["plugins"],
};

export const streamdownPluginsWithWordAnimation = {
  remarkPlugins: [
    remarkGfm,
    [remarkMath, { singleDollarTextMath: true }],
  ] as StreamdownProps["remarkPlugins"],
  rehypePlugins: [
    [rehypeKatex, { output: "html" }],
    rehypeSplitWordsIntoSpans,
  ] as StreamdownProps["rehypePlugins"],
  plugins: { mermaid, code } as StreamdownProps["plugins"],
};

// Plugins for reasoning/thinking content — derived from streamdownPlugins but without rehypeRaw,
// to prevent LLM-hallucinated HTML tags (e.g. <simd>) from being rendered as DOM elements. Mermaid
// is omitted: reasoning streams rarely contain diagrams, and the mermaid plugin is relatively
// heavy to carry on the thinking-content render path. Code highlighting is kept.
export const reasoningPlugins = {
  remarkPlugins: streamdownPlugins.remarkPlugins,
  rehypePlugins: streamdownPlugins.rehypePlugins?.filter(
    (p) => p !== rehypeRaw,
  ) as StreamdownProps["rehypePlugins"],
  plugins: { code } as StreamdownProps["plugins"],
};
