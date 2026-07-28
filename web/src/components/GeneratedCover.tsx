import "./generated-cover.css";

/**
 * A deterministic warm cover for a cookbook or recipe that has no photo, so
 * the photo-forward grid never shows a broken/empty tile. Seeded by a stable
 * string (id or name) → a warm gradient in the terracotta/amber family + the
 * subject's initial in Fraunces. Same seed always yields the same cover.
 */
function hashSeed(seed: string): number {
  let h = 0;
  for (let i = 0; i < seed.length; i++) {
    h = (h << 5) - h + seed.charCodeAt(i);
    h |= 0;
  }
  return Math.abs(h);
}

// Warm, food-magazine gradient pairs (terracotta, amber, clay, olive, plum).
const PALETTE: Array<[string, string]> = [
  ["#e8825a", "#c2410c"],
  ["#e6a14c", "#c26a12"],
  ["#d98b6a", "#a3502f"],
  ["#cf9b52", "#9a6b12"],
  ["#c98a86", "#9a4038"],
  ["#a6a15c", "#6f6a22"],
  ["#d47b6e", "#a83b46"],
  ["#dba05a", "#b06b1f"],
];

type Props = {
  seed: string;
  label: string;
  className?: string;
};

export function GeneratedCover({ seed, label, className }: Props) {
  const h = hashSeed(seed);
  const [from, to] = PALETTE[h % PALETTE.length];
  const angle = 115 + (h % 60);
  const initial = [...label.trim()][0]?.toUpperCase() ?? "•";
  return (
    <div
      className={className ? `gen-cover ${className}` : "gen-cover"}
      style={{ background: `linear-gradient(${angle}deg, ${from}, ${to})` }}
      aria-hidden="true"
    >
      <span className="gen-cover__initial">{initial}</span>
    </div>
  );
}
