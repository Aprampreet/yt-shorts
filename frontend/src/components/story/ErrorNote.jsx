/**
 * Inline API error. Shows the backend message, its `reasons` list (provider unavailable)
 * and the retry-after seconds of a rate limit (HTTP 429).
 */
export default function ErrorNote({ error }) {
  if (!error) return null;
  const data = error.data || {};
  const reasons = Array.isArray(data.reasons) ? data.reasons : [];
  const seconds = data.retry_after_seconds;
  return (
    <div className="story-error" role="alert">
      <p>{error.message}</p>
      {seconds ? <p><strong>Retry in {seconds} seconds.</strong></p> : null}
      {reasons.length > 0 && (
        <ul>
          {reasons.map(reason => <li key={reason}>{reason}</li>)}
        </ul>
      )}
    </div>
  );
}
