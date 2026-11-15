/** Small inline activity indicator. Decorative unless `label` is given. */
export default function Spinner({ label }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" aria-hidden="true" />
      {label && <span className="visually-hidden">{label}</span>}
    </span>
  );
}
