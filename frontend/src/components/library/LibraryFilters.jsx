import { useEffect, useState } from 'react';
import Button from '../ui/Button';
import { FILTER_KINDS, LICENSE_OPTIONS } from './licenses';

/**
 * Search, kind, licence and tag filters. Selects apply immediately; the text
 * inputs apply on submit so the library is not re-queried on every keystroke.
 */
export default function LibraryFilters({ filters, onChange }) {
  const [text, setText] = useState({ q: filters.q, tag: filters.tag });
  // Keep the inputs in step when a filter is set elsewhere (for example a tag clicked in the drawer).
  useEffect(() => setText({ q: filters.q, tag: filters.tag }), [filters.q, filters.tag]);

  function submit(event) {
    event.preventDefault();
    onChange({ q: text.q.trim(), tag: text.tag.trim().toLowerCase() });
  }

  function clear() {
    setText({ q: '', tag: '' });
    onChange({ q: '', tag: '', kind: '', license_status: '' });
  }

  const active = filters.q || filters.tag || filters.kind || filters.license_status;

  return (
    <form className="library-filters" onSubmit={submit} role="search" aria-label="Filter assets">
      <label className="grow">
        <span>Search</span>
        <input
          type="search"
          value={text.q}
          placeholder="Title, tag or credit"
          onChange={event => setText(old => ({ ...old, q: event.target.value }))}
        />
      </label>
      <label>
        <span>Kind</span>
        <select value={filters.kind} onChange={event => onChange({ kind: event.target.value })}>
          <option value="">All kinds</option>
          {FILTER_KINDS.map(kind => <option key={kind.value} value={kind.value}>{kind.label}</option>)}
        </select>
      </label>
      <label>
        <span>Licence</span>
        <select value={filters.license_status} onChange={event => onChange({ license_status: event.target.value })}>
          <option value="">Any licence</option>
          {LICENSE_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </label>
      <label>
        <span>Tag</span>
        <input
          value={text.tag}
          placeholder="e.g. boss-fight"
          onChange={event => setText(old => ({ ...old, tag: event.target.value }))}
        />
      </label>
      <Button type="submit" size="sm">Apply</Button>
      {active && <Button size="sm" variant="ghost" onClick={clear}>Clear filters</Button>}
    </form>
  );
}
