/** Returns a map of field → message. Exported for tests. */
export function validateUpload(file, form) {
  const errors = {};
  if (!file) errors.file = 'Choose a file to upload.';
  if (!form.license_status) errors.license_status = 'Choose a licence status. Nothing is assumed.';
  if (form.license_status === 'attribution_required' && !form.attribution_text.trim()) {
    errors.attribution_text = 'Attribution text is required for this licence status.';
  }
  return errors;
}
