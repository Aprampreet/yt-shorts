import Spinner from './Spinner';

/**
 * Button built on the shared `.button` classes.
 *
 * @param {{ variant?: 'primary'|'secondary'|'ghost'|'danger', size?: 'md'|'sm', busy?: boolean }} props
 *   `busy` disables the button and shows a spinner; pair it with a changed label for screen readers.
 */
export default function Button({
  variant = 'secondary',
  size = 'md',
  busy = false,
  disabled = false,
  type = 'button',
  className = '',
  children,
  ...rest
}) {
  const classes = ['button', variant, size === 'sm' ? 'small' : '', className].filter(Boolean).join(' ');
  return (
    <button type={type} className={classes} disabled={disabled || busy} aria-busy={busy || undefined} {...rest}>
      {busy && <Spinner />}
      {children}
    </button>
  );
}
