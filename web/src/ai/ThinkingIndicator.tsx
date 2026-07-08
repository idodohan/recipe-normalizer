/**
 * Small animated ellipsis shown in place of an assistant bubble's content
 * while a reply is in flight. Pure CSS animation — killed by the site-wide
 * `prefers-reduced-motion` rule in `base.css` (dots just sit still instead
 * of bouncing), so no reduced-motion handling is needed here.
 */
export function ThinkingIndicator() {
  return (
    <span className="ai-thinking" role="status" aria-label="Thinking">
      <span className="ai-thinking__dot" />
      <span className="ai-thinking__dot" />
      <span className="ai-thinking__dot" />
    </span>
  );
}
