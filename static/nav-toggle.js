(() => {
    const setOpen = (header, isOpen) => {
        const toggle = header.querySelector('.nav-toggle');
        const nav = header.querySelector('.main-nav');
        if (!toggle || !nav) return;
        nav.classList.toggle('is-open', isOpen);
        toggle.setAttribute('aria-expanded', String(isOpen));
    };

    document.addEventListener('click', (event) => {
        const toggle = event.target.closest('.nav-toggle');
        if (toggle) {
            const header = toggle.closest('.site-header');
            event.stopPropagation();
            const nav = header && header.querySelector('.main-nav');
            if (nav) setOpen(header, !nav.classList.contains('is-open'));
            return;
        }

        document.querySelectorAll('.site-header').forEach((header) => {
            if (!header.contains(event.target)) setOpen(header, false);
        });
    });

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        document.querySelectorAll('.site-header').forEach((header) => setOpen(header, false));
    });

    window.addEventListener('resize', () => {
        if (window.innerWidth <= 768) return;
        document.querySelectorAll('.site-header').forEach((header) => setOpen(header, false));
    });
})();
