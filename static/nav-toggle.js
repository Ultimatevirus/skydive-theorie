(() => {
    document.querySelectorAll('.site-header').forEach((header) => {
        const toggle = header.querySelector('.nav-toggle');
        const nav = header.querySelector('.main-nav');
        if (!toggle || !nav) return;

        const setOpen = (isOpen) => {
            nav.classList.toggle('is-open', isOpen);
            toggle.setAttribute('aria-expanded', String(isOpen));
        };

        toggle.addEventListener('click', (event) => {
            event.stopPropagation();
            setOpen(!nav.classList.contains('is-open'));
        });

        document.addEventListener('click', (event) => {
            if (!header.contains(event.target)) setOpen(false);
        });

        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') setOpen(false);
        });

        window.addEventListener('resize', () => {
            if (window.innerWidth > 768) setOpen(false);
        });
    });
})();
