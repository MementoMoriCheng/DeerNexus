"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * Reusable dynamic row editor for the Manifest editor's `list[dict]` fields
 * (model_requirements / skills / mcp_servers / dependencies /
 * network_requirements / secret_requirements) and the `string[]` tools field.
 *
 * Each row is a record (or a single string when `fields` is empty — the tools
 * case). Rows can be added/removed; empty rows are filtered by the parent on
 * submit. The backend stores `list[dict]` free-form (no sub-key validation),
 * so the field definitions here are the authoritative shape the editor emits.
 */

export interface DynamicField {
  /** Object key for this field within the row record. */
  key: string;
  /** Placeholder/label shown in the input. */
  label: string;
  /** Input type — "text" (default) or "number". */
  type?: "text" | "number";
  /** Whether the field must be non-empty for the row to count on submit. */
  required?: boolean;
}

interface DynamicListRowProps<T> {
  /** Field definitions; empty array means each row is a bare string (tools). */
  fields: DynamicField[];
  /** Current rows. */
  value: T[];
  /** Callback with the updated rows. */
  onChange: (rows: T[]) => void;
  /** Label for the "add" button, e.g. "Add skill". */
  addLabel: string;
  /** Factory creating a fresh empty row. */
  createRow: () => T;
}

/**
 * Coerce a row field value to a string for an <Input>. Only scalars are
 * expected (text/number); fall back to "" for anything else (objects/arrays
 * are not editable in this row editor) instead of String()'s "[object Object]".
 */
function fieldValueToString(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return "";
}

export function DynamicListRow<T>({
  fields,
  value,
  onChange,
  addLabel,
  createRow,
}: DynamicListRowProps<T>) {
  function updateRow(index: number, key: string, raw: string) {
    const field = fields.find((f) => f.key === key);
    const coerced =
      field?.type === "number" ? (raw === "" ? undefined : Number(raw)) : raw;
    const next = value.map((row, i) =>
      i === index ? ({ ...(row as object), [key]: coerced } as T) : row,
    );
    onChange(next);
  }

  function removeRow(index: number) {
    onChange(value.filter((_, i) => i !== index));
  }

  function addRow() {
    onChange([...value, createRow()]);
  }

  return (
    <div className="space-y-2">
      {value.map((row, index) => (
        <div key={index} className="flex items-start gap-2">
          {fields.length === 0 ? (
            // Bare-string row (tools): no fields defined → edit the row itself.
            <Input
              value={(row as unknown as string) ?? ""}
              onChange={(e) => {
                const next = [...value] as unknown as string[];
                next[index] = e.target.value;
                onChange(next as unknown as T[]);
              }}
              className="flex-1"
            />
          ) : (
            fields.map((field) => (
              <Input
                key={field.key}
                type={field.type ?? "text"}
                value={fieldValueToString(
                  (row as Record<string, unknown>)[field.key],
                )}
                onChange={(e) => updateRow(index, field.key, e.target.value)}
                placeholder={field.label}
                className="flex-1"
              />
            ))
          )}
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => removeRow(index)}
            aria-label="Remove row"
          >
            ✕
          </Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="outline" onClick={addRow}>
        {addLabel}
      </Button>
    </div>
  );
}
