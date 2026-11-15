import { cloneElement, useId } from 'react';

/**
 * Label + control + optional hint and error. The single child control receives `id`,
 * `aria-describedby` and `aria-invalid`.
 */
export default function Field({ label, hint, error, className = '', children }) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined;
  return (
    <div className={`field ${className}`.trim()}>
      <label htmlFor={id}>{label}</label>
      {cloneElement(children, { id, 'aria-describedby': describedBy, 'aria-invalid': error ? true : undefined })}
      {hint && <small id={hintId} className="field-hint">{hint}</small>}
      {error && <small id={errorId} className="field-error">{error}</small>}
    </div>
  );
}
