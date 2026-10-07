// kairix setup wizard — reveal-on-success glue (F91: first-party, served
// from 'self' instead of inline). A hidden action button opts in with
// data-kx-reveal-on="<selector>"; once an HTMX swap renders a node that
// matches the selector (e.g. the validation-success or scan-result card),
// the button gets the kx-revealed class and becomes visible.
document.addEventListener('htmx:afterSwap', function () {
  document.querySelectorAll('[data-kx-reveal-on]').forEach(function (btn) {
    if (document.querySelector(btn.getAttribute('data-kx-reveal-on'))) {
      btn.classList.add('kx-revealed');
    }
  });
});
