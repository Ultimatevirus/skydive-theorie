(() => {
    const supportsViewTransitions = 'ViewTransition' in window;
    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    if (prefersReducedMotion) return;

    if (supportsViewTransitions) {
        document.documentElement.style.viewTransitionName = 'root';
        return;
    }

    document.addEventListener('click', (event) => {
        const link = event.target.closest('a');
        if (!link || event.defaultPrevented || event.button !== 0) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        if (link.target && link.target !== '_self') return;

        const destination = new URL(link.href, window.location.href);
        if (destination.origin !== window.location.origin || destination.pathname === window.location.pathname) return;

        event.preventDefault();
        document.documentElement.classList.add('page-leaving');
        window.setTimeout(() => {
            window.location.href = link.href;
        }, 160);
    });
})();