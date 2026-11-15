/** Compact label. `tone`: neutral | accent | success | warn | danger. */
export default function Badge({ tone = 'neutral', children, ...rest }) {
  return (
    <span className={`badge ${tone}`} {...rest}>
      {children}
    </span>
  );
}
