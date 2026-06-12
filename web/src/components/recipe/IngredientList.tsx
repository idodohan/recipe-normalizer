/**
 * IngredientList — index-entry rows for the recipe detail page.
 *
 * Every line renders the server-built `display` string verbatim. When the
 * server included a normalized conversion, `display` contains the stable
 * " → " separator: we split on it purely for styling (original measure in
 * ink, dotted leader, arrow + normalized measure in terracotta) and never
 * alter either part.
 */

export type DisplayLine = {
  key: string;
  /** Server-built line text — the single source of truth. */
  display: string;
  note?: string | null;
  isOptional: boolean;
  /** Scaled view only: quantity is unscalable ("salt to taste"). */
  passesThrough?: boolean;
  /**
   * Scaled view only: the cook's original wording, kept visible beneath the
   * recalculated row ("from 2 cups all-purpose flour") — scaling must never
   * hide the original.
   */
  originalText?: string | null;
};

export type DisplayGroup = {
  key: string;
  name?: string | null;
  lines: DisplayLine[];
};

const ARROW = " → ";

function IngredientRow({ line }: { line: DisplayLine }) {
  const splitAt = line.display.indexOf(ARROW);
  const original = splitAt === -1 ? line.display : line.display.slice(0, splitAt);
  // Keep the arrow with the normalized part: "→ ~120 g (approx.)".
  const normalized = splitAt === -1 ? null : line.display.slice(splitAt + 1);

  return (
    <li className="ing-line">
      <p className="ing-line__row">
        <span className="ing-line__orig">
          {original}
          {line.isOptional ? (
            <span className="ing-line__optional"> optional</span>
          ) : null}
        </span>
        {normalized || line.passesThrough ? (
          <span className="ing-line__leader" aria-hidden="true" />
        ) : null}
        {normalized ? <span className="ing-line__norm">{normalized}</span> : null}
        {line.passesThrough ? (
          <span className="ing-line__pass">doesn’t scale</span>
        ) : null}
      </p>
      {line.originalText ? (
        <p className="ing-line__from">from {line.originalText}</p>
      ) : null}
      {line.note ? <p className="ing-line__note">{line.note}</p> : null}
    </li>
  );
}

export function IngredientList({ groups }: { groups: DisplayGroup[] }) {
  return (
    <div className="ing">
      {groups.map((group) => (
        <section key={group.key} className="ing__group">
          {group.name ? <h3 className="ing__group-name">{group.name}</h3> : null}
          <ul className="ing__lines">
            {group.lines.map((line) => (
              <IngredientRow key={line.key} line={line} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
