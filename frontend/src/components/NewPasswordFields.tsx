"use client";

import { useId } from "react";

/** Backend app/users.py MIN_PASSWORD_LENGTH. */
export const MIN_PASSWORD_LENGTH = 12;

/** Why two typed passwords can't be used, or null if they can. */
export function newPasswordProblem(next: string, again: string): string | null {
  return next === again ? null : "the new passwords don't match";
}

/** "New password" and "New password again", with the rule under the first.
 *  Shared by changing a password and resetting a forgotten one. */
export default function NewPasswordFields({
  next,
  again,
  onNext,
  onAgain,
  autoFocus = false,
}: {
  next: string;
  again: string;
  onNext: (value: string) => void;
  onAgain: (value: string) => void;
  autoFocus?: boolean;
}) {
  const hintId = useId();
  const input = "input w-full";
  return (
    <>
      <div className="text-sm">
        <label className="block">
          <span className="mb-1 block text-gray-600">New password</span>
          <input
            type="password"
            autoComplete="new-password"
            required
            autoFocus={autoFocus}
            minLength={MIN_PASSWORD_LENGTH}
            aria-describedby={hintId}
            value={next}
            onChange={(e) => onNext(e.target.value)}
            className={input}
          />
        </label>
        {/* Outside the label, so the field's name stays "New password". */}
        <span id={hintId} className="mt-1 block text-xs text-gray-500">
          At least {MIN_PASSWORD_LENGTH} characters. A few unrelated words work well.
        </span>
      </div>
      <label className="block text-sm">
        <span className="mb-1 block text-gray-600">New password again</span>
        <input
          type="password"
          autoComplete="new-password"
          required
          value={again}
          onChange={(e) => onAgain(e.target.value)}
          className={input}
        />
      </label>
    </>
  );
}
