/**
 * Shell-level error banner and status notice, rendered by each page directly
 * under its PageHeading.
 */
export default function Notices({ error, onDismissError, notice }) {
  return (
    <>
      {error && (
        <div className="error-banner" role="alert">
          {error}
          <button type="button" aria-label="Dismiss error" onClick={onDismissError}>×</button>
        </div>
      )}
      {notice && <div className="notice" role="status">{notice}</div>}
    </>
  );
}
