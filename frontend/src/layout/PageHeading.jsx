/**
 * Page title block shared by every route.
 *
 * @param {{ eyebrow?: string, title: string, subtitle?: string, description?: string }} props
 *   `title` is the first line of the h1, `subtitle` the dimmed second line.
 */
export default function PageHeading({ eyebrow = 'LONG FORM. NEW POSSIBILITIES.', title, subtitle, description }) {
  return (
    <div className="page-heading">
      <div className="eyebrow">
        <span className="accent-line" /> {eyebrow}
      </div>
      <h1>
        {title}
        {subtitle && (
          <>
            <br />
            <span>{subtitle}</span>
          </>
        )}
      </h1>
      {description && <p>{description}</p>}
    </div>
  );
}
