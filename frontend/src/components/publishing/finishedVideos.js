const FINISHED = new Set(['completed', 'needs_review']);

/** Finished videos from recent jobs, flattened to { short, jobTitle }. */
export function finishedVideos(jobs) {
  if (!Array.isArray(jobs)) return [];
  return jobs
    .filter(job => FINISHED.has(job.status))
    .flatMap(job => (job.results || []).map(short => ({ short, jobTitle: job.title || 'Untitled project' })));
}
