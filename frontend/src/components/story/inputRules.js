export const TRANSCRIPT_LIMIT = 60000;


export function validateInput(form) {
  const errors = {};
  const url = form.url.trim();
  const transcript = form.transcript.trim();
  if (!url && !transcript) errors.source = 'Add a YouTube URL, a transcript or notes (at least one).';
  if (form.transcript.length > TRANSCRIPT_LIMIT) errors.transcript = `Keep it within ${TRANSCRIPT_LIMIT.toLocaleString()} characters.`;
  if (url && !form.rights_declaration) errors.rights = 'Confirm the rights declaration to use a URL.';
  return errors;
}

