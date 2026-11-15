import Badge from '../ui/Badge';

const STATUSES = {
  draft: ['Draft', 'neutral'],
  queued: ['Queued', 'accent'],
  scheduled: ['Scheduled', 'accent'],
  uploading: ['Uploading', 'accent'],
  uploaded_private: ['Uploaded (private)', 'warn'],
  published: ['Published', 'success'],
  failed: ['Failed', 'danger'],
  cancelled: ['Cancelled', 'neutral'],
};

/** Renders the post status exactly as the API reports it. "Published" only ever appears for `published`. */
export default function PostStatusBadge({ status }) {
  const [label, tone] = STATUSES[status] || [String(status || 'unknown').replace(/_/g, ' '), 'neutral'];
  return <Badge tone={tone}>{label}</Badge>;
}
