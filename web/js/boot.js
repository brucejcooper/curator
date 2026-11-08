// The boot watchdog: a classic script, run before the app's modules, so it
// works when they can't (a module that fails to load or to link stops the
// whole graph, and the spinner would turn forever, #20). If app.js hasn't
// started within BOOT_MS, or fails outright, the spinner gives way to a
// message and a button that drops this copy's service worker and caches and
// loads the app afresh. Separate from index.html because the CSP allows no
// inline script.
(() => {
  const BOOT_MS = 8000;
  let failed = false;

  async function reset() {
    try {
      const reg = await navigator.serviceWorker?.getRegistration();
      const scope = reg?.scope ?? new URL('./', location.href).href;
      await reg?.unregister();
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k.startsWith('mastorss-') && k.endsWith(` ${scope}`)).map((k) => caches.delete(k)));
    } catch {}
    location.reload();
  }

  function fail(why) {
    if (failed || window.readerStarted) return;
    failed = true;
    const show = () => {
      const boot = document.getElementById('boot');
      if (!boot) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'primary';
      button.textContent = 'Reset and reload';
      button.onclick = reset;
      const text = document.createElement('p');
      text.textContent = "Couldn't start the reader.";
      boot.replaceChildren(text, button);
      boot.classList.add('failed');
      boot.dataset.why = why;
      boot.hidden = false;
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', show);
    else show();
  }

  setTimeout(() => fail('timeout'), BOOT_MS);
  // A module that fails to load fires `error` on its script (or, for a module
  // it imports, on the window); one that fails to link reports a SyntaxError.
  window.addEventListener('error', (e) => {
    const target = e.target;
    if (target instanceof HTMLScriptElement && target.type === 'module') fail('load');
    else if (e.error instanceof SyntaxError) fail('link');
  }, true);
})();
