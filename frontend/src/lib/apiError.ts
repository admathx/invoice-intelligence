/** Turns a FastAPI error body into a sentence a person can act on.
 *
 * The API answers in three shapes: a plain string ("file is not a PDF"), the
 * review endpoints' {message, reasons}, and FastAPI's own validation list
 * ([{loc, msg}]). Screens used to handle one or two of these and fall back to
 * "Request failed" for the rest, which hid the one thing that mattered: which
 * of twenty editable cells was wrong, or why an upload was refused.
 */

type ValidationIssue = { loc?: (string | number)[]; msg?: string };

/** Names a validation location for a person. Gets the full `loc` so a screen
 *  can map array positions back to what it showed (e.g. "line 3"). */
export type LocationLabeller = (loc: (string | number)[]) => string | null;

const defaultLabel: LocationLabeller = (loc) => {
  const field = loc.filter((part) => typeof part === "string" && part !== "body").at(-1);
  return typeof field === "string" ? field.replaceAll("_", " ") : null;
};

// Pydantic's messages for the inputs people actually get wrong, reworded for
// someone typing a price rather than someone reading a schema.
function friendly(msg: string): string {
  if (/valid decimal|valid number/i.test(msg)) return "enter a plain number, like 1234.50 (no $ or commas)";
  if (/at least 1 character/i.test(msg)) return "required";
  if (/more than \d+ digits|decimal places/i.test(msg)) return "too many digits";
  return msg;
}

export function formatApiError(detail: unknown, fallback: string, label: LocationLabeller = defaultLabel): string {
  if (typeof detail === "string" && detail.trim()) return detail;

  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const { message, reasons } = detail as { message?: string; reasons?: string[] };
    if (message && Array.isArray(reasons) && reasons.length) return `${message}: ${reasons.join("; ")}`;
    if (message) return message;
  }

  if (Array.isArray(detail) && detail.length) {
    return (detail as ValidationIssue[])
      .map((issue) => {
        const where = issue.loc ? label(issue.loc) ?? defaultLabel(issue.loc) : null;
        const what = friendly(issue.msg ?? "invalid");
        return where ? `${where}: ${what}` : what;
      })
      .join("; ");
  }

  return fallback;
}
