import { useId } from 'react';

/** Honest empty/unavailable state built on the shared `.empty-state` class. */
export default function EmptyState({ title, children, action }) {
  const id = useId();
  return (
    <section className="empty-state" aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      {children && <p>{children}</p>}
      {action}
    </section>
  );
}
