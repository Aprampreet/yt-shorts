/**
 * Main navigation, in sidebar order. `match` decides which item is active for
 * a pathname; the Topbar uses the same table for its breadcrumb.
 */
export const NAV_ITEMS = [
  { key: 'shorts', to: '/', label: 'Create Shorts', icon: '▣', match: path => path === '/' },
  { key: 'story', to: '/story', label: 'AI Story Studio', icon: '◇', match: path => path === '/story' || path.startsWith('/story/') },
  { key: 'publish', to: '/publish', label: 'Publish & Schedule', icon: '▦', match: path => path.startsWith('/publish') },
  { key: 'library', to: '/library', label: 'Media Library', icon: '▤', match: path => path.startsWith('/library') },
  { key: 'history', to: '/history', label: 'Recent projects', icon: '◷', match: path => path.startsWith('/history') },
];

export function activeNavItem(pathname) {
  return NAV_ITEMS.find(item => item.match(pathname)) || null;
}
