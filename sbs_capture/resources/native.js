
/* The native app owns opening, closing, swiping, title, domain and source URL.
   This adapter freezes capture controls, enforces no blur and forwards arrows. */
(() => {
  'use strict';
  const snapshotPath = location.pathname;
  for (const method of ['pushState', 'replaceState']) {
    const original = history[method];
    history[method] = function(state, title, value) {
      if (value != null) {
        const url = new URL(value, location.href);
        value = snapshotPath + url.search + url.hash;
      }
      return original.call(this, state, title, value);
    };
  }
  function sourceURL(value) {
    const url = new URL(value, location.href);
    if (url.origin === location.origin && url.pathname.startsWith('/images/')) {
      const source = new URL(document.documentElement.dataset.sbsSource);
      url.protocol = source.protocol;
      url.host = source.host;
    }
    return url.href;
  }
  // A saved S3 path is not an image-search API. Keep native resource requests
  // and modifier-click destinations on the configured source host.
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...args) {
    return open.call(this, method, sourceURL(url), ...args);
  };
  function prepare() {
    const filters = ['filter', '-webkit-filter', 'backdrop-filter', '-webkit-backdrop-filter'];
    // Inline overrides also beat highly specific native !important selectors.
    // Reset every ancestor: parent filters blur all of their children.
    const imageLayers = new Set();
    document.querySelectorAll('img,picture,video,canvas,[class*="Image"],[class*="Thumb"]').forEach(image => {
      for (let el = image; el; el = el.parentElement) imageLayers.add(el);
    });
    imageLayers.forEach(el => {
      for (const property of filters) {
        if (el.style.getPropertyValue(property) !== 'none' || el.style.getPropertyPriority(property) !== 'important') {
          el.style.setProperty(property, 'none', 'important');
        }
      }
    });
    // Native scripts can restore an inline filter after hydration or swiping.
    document.querySelectorAll('[style]').forEach(el => {
      for (const property of filters) {
        if (/blur\s*\(/i.test(el.style.getPropertyValue(property))) {
          el.style.setProperty(property, 'none', 'important');
        }
      }
    });
    document.querySelectorAll('.SerpItem a, .ViewerScene a, .ImagesViewer a').forEach(a => {
      if (a.hasAttribute('href')) a.href = sourceURL(a.href);
      if (a.target !== '_blank') a.target = '_blank';
      if (a.rel !== 'noopener noreferrer') a.rel = 'noopener noreferrer';
    });
    document.querySelectorAll('.HeaderPhone input, .HeaderPhone textarea, .HeaderPhone button, .FetchListButton-Button').forEach(el => {
      if (!el.disabled) el.disabled = true;
    });
  }
  document.addEventListener('DOMContentLoaded', () => {
    prepare();
    new MutationObserver(prepare).observe(document.documentElement, {
      childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class']
    });
  });
  window.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey ||
        !['ArrowLeft', 'ArrowRight'].includes(event.key) || event.target.closest?.('input,textarea,select,[contenteditable="true"]')) return;
    const selector = event.key === 'ArrowLeft'
      ? '.ImagesViewer-ButtonPrev, .ImagesTouchViewer-ClickableSide_left'
      : '.ImagesViewer-ButtonNext, .ImagesTouchViewer-ClickableSide_right';
    const visible = el => el.getClientRects().length && getComputedStyle(el).visibility === 'visible';
    const viewer = Array.from(document.querySelectorAll('.ViewerScene,.ImagesViewer')).find(visible);
    if (!viewer) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const button = Array.from(viewer.querySelectorAll(selector)).find(el => visible(el) && !el.disabled &&
      el.getAttribute('aria-disabled') !== 'true' && !el.classList.contains('CircleButton_disabled'));
    if (button) button.click();
  }, true);
})();
