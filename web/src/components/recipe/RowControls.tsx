type RowControlsProps = {
  noun: string;
  onUp: () => void;
  onDown: () => void;
  onRemove: () => void;
  upDisabled?: boolean;
  downDisabled?: boolean;
  removeDisabled?: boolean;
};

/** Up / down / remove cluster used by lines, groups, and steps. */
export function RowControls({
  noun,
  onUp,
  onDown,
  onRemove,
  upDisabled = false,
  downDisabled = false,
  removeDisabled = false,
}: RowControlsProps) {
  return (
    <span className="row-controls">
      <button
        type="button"
        className="icon-btn"
        aria-label={`Move ${noun} up`}
        disabled={upDisabled}
        onClick={onUp}
      >
        ↑
      </button>
      <button
        type="button"
        className="icon-btn"
        aria-label={`Move ${noun} down`}
        disabled={downDisabled}
        onClick={onDown}
      >
        ↓
      </button>
      <button
        type="button"
        className="icon-btn icon-btn--remove"
        aria-label={`Remove ${noun}`}
        disabled={removeDisabled}
        onClick={onRemove}
      >
        ×
      </button>
    </span>
  );
}
