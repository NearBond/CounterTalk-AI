/* CounterTalk's landing page — no dependencies. */

// ---------- Header: border once the page scrolls ----------
const header = document.querySelector('[data-header]');

if (header) {
  const onScroll = () => header.classList.toggle('is-scrolled', window.scrollY > 8);
  onScroll();
  window.addEventListener('scroll', onScroll, { passive: true });
}

// ---------- Header: mobile menu ----------
const nav = document.querySelector('[data-nav]');
const navToggle = document.querySelector('[data-nav-toggle]');

if (nav && navToggle) {
  const setMenu = (open) => {
    nav.classList.toggle('is-open', open);
    navToggle.setAttribute('aria-expanded', String(open));
  };
  const isOpen = () => navToggle.getAttribute('aria-expanded') === 'true';

  navToggle.addEventListener('click', () => setMenu(!isOpen()));

  // Close after choosing a link, pressing Escape or clicking outside the header
  nav.addEventListener('click', (e) => {
    if (e.target.closest('a')) setMenu(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isOpen()) {
      setMenu(false);
      navToggle.focus();
    }
  });
  document.addEventListener('click', (e) => {
    if (isOpen() && !header.contains(e.target)) setMenu(false);
  });
  window.matchMedia('(min-width: 861px)').addEventListener('change', (e) => {
    if (e.matches) setMenu(false);
  });
}

// ---------- Scroll reveal (sections fade up as they enter the viewport) ----------
const revealEls = document.querySelectorAll('[data-reveal]');

if ('IntersectionObserver' in window) {
  const revealer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('is-visible');
      revealer.unobserve(entry.target);
    });
  }, { threshold: 0.2, rootMargin: '0px 0px -8% 0px' });

  revealEls.forEach((el) => revealer.observe(el));
} else {
  revealEls.forEach((el) => el.classList.add('is-visible'));
}

// ---------- How it works: recording timer ticks while on screen ----------
const recTimer = document.querySelector('[data-rec-timer]');

if (recTimer && 'IntersectionObserver' in window) {
  let seconds = 252; // starts at 04:12
  let interval = 0;

  new IntersectionObserver(([entry]) => {
    clearInterval(interval);
    if (!entry.isIntersecting) return;
    interval = setInterval(() => {
      seconds += 1;
      const m = String(Math.floor(seconds / 60)).padStart(2, '0');
      const s = String(seconds % 60).padStart(2, '0');
      recTimer.textContent = `${m}:${s}`;
    }, 1000);
  }).observe(recTimer);
}

// ---------- Features: Original / English quote toggle ----------
const langToggle = document.querySelector('[data-lang-toggle]');

if (langToggle) {
  const buttons = langToggle.querySelectorAll('button[data-lang]');
  const versions = langToggle.parentElement.querySelectorAll('[data-lang-version]');

  langToggle.addEventListener('click', (e) => {
    const chosen = e.target.closest('button[data-lang]');
    if (!chosen) return;
    buttons.forEach((b) => b.setAttribute('aria-pressed', String(b === chosen)));
    versions.forEach((v) => { v.hidden = v.dataset.langVersion !== chosen.dataset.lang; });
  });
}

// ---------- Waitlist form: inline validation, then an email with the details ----------
// Nothing is sent from the page: submitting opens the visitor's email app with a message
// to the address in data-mailto, every field on its own line, for them to send.
const waitlistForm = document.querySelector('[data-waitlist-form]');

if (waitlistForm) {
  const mailTo = waitlistForm.dataset.mailto.trim();
  const success = document.querySelector('[data-waitlist-success]');

  // Every field, in the form's order, as it's labelled in the email
  const emailFields = [
    ['name', 'Name'],
    ['email', 'Work email'],
    ['company', 'Company'],
    ['role', 'Role'],
    ['reps', 'Field reps'],
    ['industry', 'Industry'],
    ['software', 'Sales software'],
    ['whatsapp', 'WhatsApp number'],
    ['question', 'What reps hear that never reaches head office'],
  ];

  const mailtoUrl = (data) => {
    const subject = `CounterTalk waitlist: ${(data.company || '').trim()}`;
    const body = emailFields
      .map(([key, label]) => `${label}: ${(data[key] || '').trim() || '(not given)'}`)
      .join('\n')
      .replace(/\r?\n/g, '\r\n'); // email line breaks are CRLF, typed ones included
    return `mailto:${mailTo}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  };

  // One control per field; a radio group is represented by its first radio
  const controls = Array.from(waitlistForm.querySelectorAll('[data-field]'))
    .map((field) => field.querySelector('input, select, textarea'));

  const messageFor = (control) => {
    const { validity } = control;
    if (validity.valid) return '';
    if (control.type === 'email' && validity.typeMismatch) return "That email doesn't look right.";
    return control.dataset.error || 'Please fill this in.';
  };

  const check = (control) => {
    const field = control.closest('[data-field]');
    const message = messageFor(control);
    const errorEl = field.querySelector('[data-error-text]');
    field.toggleAttribute('data-invalid', Boolean(message));
    if (errorEl) errorEl.textContent = message;
    field.querySelectorAll('input, select, textarea').forEach((el) => {
      if (message) el.setAttribute('aria-invalid', 'true');
      else el.removeAttribute('aria-invalid');
    });
    return !message;
  };

  // After the first attempt, errors clear as people fix them
  let attempted = false;
  const recheck = (e) => {
    if (!attempted) return;
    const field = e.target.closest('[data-field]');
    if (field) check(field.querySelector('input, select, textarea'));
  };
  waitlistForm.addEventListener('input', recheck);
  waitlistForm.addEventListener('change', recheck);

  const showSuccess = (data) => {
    const firstName = (data.name || '').trim().split(/\s+/)[0];
    success.querySelector('[data-success-title]').textContent =
      firstName ? `Thanks, ${firstName}. One last step.` : 'Thanks. One last step.';
    waitlistForm.hidden = true;
    success.hidden = false;
    success.querySelector('[data-success-title]').focus();
  };

  waitlistForm.addEventListener('submit', (e) => {
    e.preventDefault();
    attempted = true;

    const invalid = controls.filter((control) => !check(control));
    if (invalid.length) {
      invalid[0].focus();
      return;
    }

    const data = Object.fromEntries(new FormData(waitlistForm));
    if (data.website) { // bots fill the hidden field; pretend it worked
      showSuccess({});
      return;
    }

    window.location.href = mailtoUrl(data);
    showSuccess(data);
  });
}

// ---------- Footer: keep the copyright year current ----------
document.querySelectorAll('[data-year]').forEach((el) => {
  el.textContent = new Date().getFullYear();
});

// ---------- Hero: looping video with a pause button ----------
const heroVideo = document.querySelector('[data-hero-video]');

if (heroVideo) {
  const video = heroVideo.querySelector('video');
  const toggle = heroVideo.querySelector('[data-video-toggle]');

  // Plays from the start, muted, and stays paused only once the visitor presses pause
  let pausedByUser = false;

  const sync = () => {
    heroVideo.classList.toggle('is-paused', video.paused);
    toggle.setAttribute('aria-label', video.paused ? 'Play the video' : 'Pause the video');
  };
  const play = () => video.play().catch(sync); // a blocked autoplay just leaves the play button showing

  video.addEventListener('play', sync);
  video.addEventListener('pause', sync);
  sync();

  toggle.addEventListener('click', () => {
    pausedByUser = !video.paused;
    if (pausedByUser) video.pause();
    else play();
  });

  // Only play while the video is on screen
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) video.pause();
      else if (!pausedByUser) play();
    }).observe(video);
  }
}
