import { useState } from 'react';
import Button from '../ui/Button';
import PostMetadataPanel from './PostMetadataPanel';
import RightsReview from './RightsReview';

/**
 * Expandable per-post tools shown under each queue item: platform metadata and,
 * for posts that can still be approved, the rights review.
 */
export default function PostExtras({ post, onChanged }) {
  const [open, setOpen] = useState(false);
  const approvable = ['draft', 'failed'].includes(post.status);
  return (
    <div className="post-extras">
      <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen(value => !value)}>
        {open ? 'Hide' : 'Show'} metadata{approvable ? ' & rights' : ''}
      </Button>
      {open && (
        <div className="post-extras-body">
          <PostMetadataPanel post={post} onSaved={onChanged} />
          {approvable && post.short_id && <RightsReview shortId={post.short_id} onDocumented={onChanged} />}
        </div>
      )}
    </div>
  );
}
