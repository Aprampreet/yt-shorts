const pad = value => String(Math.abs(value)).padStart(2, '0');

/**
 * Convert a `datetime-local` value ("2026-05-01T18:30") in the browser's time
 * zone to an ISO 8601 string with an explicit offset ("2026-05-01T18:30:00+05:30"),
 * which the API requires. Returns '' for an empty or invalid value.
 */
export function toOffsetISO(localValue) {
  if (!localValue) return '';
  const date = new Date(localValue);
  if (Number.isNaN(date.getTime())) return '';
  const offset = -date.getTimezoneOffset();
  const sign = offset >= 0 ? '+' : '-';
  const stamp = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  return `${stamp}${sign}${pad(Math.trunc(offset / 60))}:${pad(offset % 60)}`;
}

/** The API rejects times that are not more than ten minutes ahead. */
export const MIN_LEAD_MINUTES = 10;

export function isFarEnough(localValue, now = Date.now()) {
  const date = new Date(localValue);
  return !Number.isNaN(date.getTime()) && date.getTime() > now + MIN_LEAD_MINUTES * 60000;
}
