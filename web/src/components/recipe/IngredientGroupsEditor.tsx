import { useRef } from "react";
import type { KeyboardEvent } from "react";
import { moveItem, newGroup, newLine, parseQuantity } from "./draft";
import type { GroupDraft, LineDraft } from "./draft";
import { RowControls } from "./RowControls";

type IngredientGroupsEditorProps = {
  groups: GroupDraft[];
  onChange: (groups: GroupDraft[]) => void;
};

export function IngredientGroupsEditor({
  groups,
  onChange,
}: IngredientGroupsEditorProps) {
  // Focus the original_text input of a line created by pressing Enter:
  // the new input picks this up via callback ref when it mounts.
  const pendingFocusRef = useRef<string | null>(null);

  function lineInputRef(lineId: string) {
    return (element: HTMLInputElement | null) => {
      if (element && pendingFocusRef.current === lineId) {
        pendingFocusRef.current = null;
        element.focus();
      }
    };
  }

  function updateGroup(groupId: string, patch: Partial<GroupDraft>) {
    onChange(
      groups.map((group) =>
        group.id === groupId ? { ...group, ...patch } : group,
      ),
    );
  }

  function updateLine(
    groupId: string,
    lineId: string,
    patch: Partial<LineDraft>,
  ) {
    onChange(
      groups.map((group) =>
        group.id === groupId
          ? {
              ...group,
              lines: group.lines.map((line) =>
                line.id === lineId ? { ...line, ...patch } : line,
              ),
            }
          : group,
      ),
    );
  }

  function addLineAfter(groupId: string, lineId: string | null) {
    const line = newLine();
    onChange(
      groups.map((group) => {
        if (group.id !== groupId) return group;
        const at =
          lineId === null
            ? group.lines.length
            : group.lines.findIndex((l) => l.id === lineId) + 1;
        const lines = [...group.lines];
        lines.splice(at, 0, line);
        return { ...group, lines };
      }),
    );
    pendingFocusRef.current = line.id;
  }

  function onLineKeyDown(
    event: KeyboardEvent<HTMLInputElement>,
    groupId: string,
    lineId: string,
  ) {
    if (event.key === "Enter") {
      event.preventDefault();
      addLineAfter(groupId, lineId);
    }
  }

  return (
    <div className="groups">
      {groups.map((group, groupIndex) => (
        <fieldset className="group" key={group.id}>
          <div className="group__head">
            <input
              className="group__name-input"
              type="text"
              value={group.name}
              aria-label={`Name of ingredient group ${groupIndex + 1} (optional)`}
              placeholder={
                groupIndex === 0
                  ? "Group name (optional) — e.g. For the dough"
                  : "Group name (optional)"
              }
              onChange={(event) =>
                updateGroup(group.id, { name: event.target.value })
              }
            />
            <RowControls
              noun="group"
              upDisabled={groupIndex === 0}
              downDisabled={groupIndex === groups.length - 1}
              removeDisabled={groups.length === 1}
              onUp={() => onChange(moveItem(groups, groupIndex, -1))}
              onDown={() => onChange(moveItem(groups, groupIndex, 1))}
              onRemove={() =>
                onChange(groups.filter((g) => g.id !== group.id))
              }
            />
          </div>

          <ul className="group__lines">
            {group.lines.map((line, lineIndex) => (
              <li className="line" key={line.id}>
                <div className="line__primary">
                  <input
                    className="input line__original"
                    type="text"
                    ref={lineInputRef(line.id)}
                    data-line-input={line.id}
                    value={line.original_text}
                    aria-label="Ingredient line as written"
                    placeholder="As written — e.g. 1 heaping cup flour"
                    onChange={(event) =>
                      updateLine(group.id, line.id, {
                        original_text: event.target.value,
                      })
                    }
                    onKeyDown={(event) =>
                      onLineKeyDown(event, group.id, line.id)
                    }
                  />
                  <RowControls
                    noun="line"
                    upDisabled={lineIndex === 0}
                    downDisabled={lineIndex === group.lines.length - 1}
                    removeDisabled={group.lines.length === 1}
                    onUp={() =>
                      updateGroup(group.id, {
                        lines: moveItem(group.lines, lineIndex, -1),
                      })
                    }
                    onDown={() =>
                      updateGroup(group.id, {
                        lines: moveItem(group.lines, lineIndex, 1),
                      })
                    }
                    onRemove={() =>
                      updateGroup(group.id, {
                        lines: group.lines.filter((l) => l.id !== line.id),
                      })
                    }
                  />
                </div>
                <div className="line__meta">
                  <input
                    className="input input--sm"
                    type="text"
                    value={line.name}
                    aria-label="Ingredient name for matching"
                    placeholder="ingredient name for matching"
                    onChange={(event) =>
                      updateLine(group.id, line.id, {
                        name: event.target.value,
                      })
                    }
                  />
                  <input
                    className="input input--sm input--num"
                    type="text"
                    inputMode="decimal"
                    value={line.quantity}
                    aria-label="Quantity"
                    placeholder="qty"
                    onChange={(event) =>
                      updateLine(group.id, line.id, {
                        quantity: event.target.value,
                      })
                    }
                  />
                  <input
                    className="input input--sm"
                    type="text"
                    value={line.unit}
                    aria-label="Unit"
                    placeholder="unit"
                    onChange={(event) =>
                      updateLine(group.id, line.id, {
                        unit: event.target.value,
                      })
                    }
                  />
                  <input
                    className="input input--sm"
                    type="text"
                    value={line.note}
                    aria-label="Note"
                    placeholder="note"
                    onChange={(event) =>
                      updateLine(group.id, line.id, {
                        note: event.target.value,
                      })
                    }
                  />
                  <label className="line__optional">
                    <input
                      type="checkbox"
                      checked={line.is_optional}
                      onChange={(event) =>
                        updateLine(group.id, line.id, {
                          is_optional: event.target.checked,
                        })
                      }
                    />
                    optional
                  </label>
                </div>
                {line.quantity.trim() &&
                parseQuantity(line.quantity) === null ? (
                  <p className="line__qty-hint">
                    quantity not recognized — kept as text only
                  </p>
                ) : null}
              </li>
            ))}
          </ul>

          <button
            type="button"
            className="editor-add"
            onClick={() => addLineAfter(group.id, null)}
          >
            + Add ingredient
          </button>
        </fieldset>
      ))}

      <button
        type="button"
        className="editor-add editor-add--group"
        onClick={() => onChange([...groups, newGroup()])}
      >
        + Add group
      </button>
    </div>
  );
}
