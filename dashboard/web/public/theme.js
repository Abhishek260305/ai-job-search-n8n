// Runs before the first paint (index.html loads it in <head>, not as a module). The page starts dark, the default;
// a saved light theme removes the class here, so it never flashes dark first. The key is THEME_KEY in
// src/lib/theme.ts. A separate file, not an inline script: the Content-Security-Policy allows same-origin scripts only.
try {
  if (window.localStorage.getItem('jd-theme') === 'light') document.documentElement.classList.remove('dark')
} catch (e) {
  // storage blocked: stay dark
}
