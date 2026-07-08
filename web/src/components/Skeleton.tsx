import "./skeleton.css";

type SkeletonVariant = "card-grid" | "detail" | "rows";

type SkeletonProps = {
  variant: SkeletonVariant;
  /** card-grid: number of cards (default 6). rows: number of rows (default 5). */
  count?: number;
};

/**
 * Paper-tone shimmer placeholder. Each variant's markup replicates the
 * grid/padding/border of the real layout it stands in for (cookbook grid,
 * recipe detail, job rows) so swapping it for content causes no layout
 * shift. Purely decorative — hidden from assistive tech.
 */
export function Skeleton({ variant, count }: SkeletonProps) {
  if (variant === "card-grid") return <CardGridSkeleton count={count ?? 6} />;
  if (variant === "detail") return <DetailSkeleton />;
  return <RowsSkeleton count={count ?? 5} />;
}

function CardGridSkeleton({ count }: { count: number }) {
  return (
    <ul className="skel-cookbook-grid" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <li key={i}>
          <div className="skel-card">
            <div className="skel-card__kicker">
              <span className="skel" style={{ width: "42%" }} />
              <span className="skel" style={{ width: "14%" }} />
            </div>
            <span className="skel skel-card__initial" />
            <span className="skel skel-card__title" style={{ width: "88%" }} />
            <span
              className="skel skel-card__title skel-card__title--last"
              style={{ width: "58%" }}
            />
            <div className="skel-card__foot">
              <span className="skel" style={{ width: "28%" }} />
              <span className="skel" style={{ width: "18%" }} />
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

function DetailSkeleton() {
  const ingredientLines = [72, 58, 66, 44, 60, 50];
  const steps = 4;

  return (
    <div className="skel-detail" aria-hidden="true">
      <div className="skel-detail__header">
        <span className="skel skel-detail__kicker" />
        <span className="skel skel-detail__title" />
        <span className="skel skel-detail__desc" />
      </div>

      <div className="skel-detail__meta">
        <span className="skel" />
        <span className="skel" />
        <span className="skel" />
      </div>

      <div className="skel-detail__body">
        <div>
          <span className="skel skel-detail__colhead" />
          {ingredientLines.map((width, i) => (
            <div key={i} className="skel-detail__ing-line">
              <span className="skel" style={{ width: `${width}%` }} />
            </div>
          ))}
        </div>
        <div>
          <span className="skel skel-detail__colhead" />
          {Array.from({ length: steps }, (_, i) => (
            <div key={i} className="skel-detail__step">
              <span className="skel skel--num" />
              <span className="skel skel--text" />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function RowsSkeleton({ count }: { count: number }) {
  return (
    <ul className="skel-rows" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <li key={i} className="skel-row">
          <div className="skel-row__main">
            <span className="skel skel--kind" />
            <span
              className="skel skel--summary"
              style={{ width: `${45 - (i % 3) * 6}%`, maxWidth: "18rem" }}
            />
          </div>
          <p className="skel-row__reason">
            <span className="skel" />
          </p>
          <div className="skel-row__side">
            <span className="skel" />
          </div>
        </li>
      ))}
    </ul>
  );
}
