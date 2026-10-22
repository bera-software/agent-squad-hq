import ReactMarkdown, { type Components } from "react-markdown";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

/**
 * Safe Markdown rendering for server-authored (but LLM-written, so untrusted) text.
 * - raw HTML is dropped (`skipHtml`), never injected;
 * - the HAST tree is sanitised against GitHub's schema (rehype-sanitize);
 * - only http(s)/mailto/relative links survive, and they open with noopener;
 * - headings are shifted down one level beneath the page <h1>.
 */
const SAFE_URL = /^(https?:|mailto:|#|\/)/i;

export function safeUrl(url: string): string {
  return SAFE_URL.test(url.trim()) ? url : "";
}

/** Document headings sit under the page's own <h1>, so shift them down one level. */
const components: Components = {
  h1: ({ children }) => <h2>{children}</h2>,
  h2: ({ children }) => <h3>{children}</h3>,
  h3: ({ children }) => <h4>{children}</h4>,
  h4: ({ children }) => <h5>{children}</h5>,
  h5: ({ children }) => <h6>{children}</h6>,
  a: ({ href, children }) =>
    href ? (
      <a href={href} rel="noopener noreferrer nofollow" target={href.startsWith("#") ? undefined : "_blank"}>
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  img: ({ alt }) => <span className="subtle">[image: {alt ?? "untitled"}]</span>,
};

const schema = { ...defaultSchema, tagNames: (defaultSchema.tagNames ?? []).filter((t) => t !== "img") };

export function Markdown({ source }: { source: string }) {
  return (
    <div className="prose">
      <ReactMarkdown skipHtml urlTransform={safeUrl} rehypePlugins={[[rehypeSanitize, schema]]} components={components}>
        {source}
      </ReactMarkdown>
    </div>
  );
}
