(() => {
    const root = document.documentElement;
    const themeToggle = document.querySelector('.theme-toggle');

    function applyTheme(theme) {
        const isDark = theme === 'dark';
        root.setAttribute('data-theme', isDark ? 'dark' : 'light');
        if (themeToggle) {
            themeToggle.setAttribute(
                'aria-label',
                isDark ? themeToggle.dataset.lightLabel : themeToggle.dataset.darkLabel,
            );
        }
    }

    applyTheme(localStorage.getItem('theme-preference') || 'light');

    if (themeToggle) {
        themeToggle.addEventListener('click', () => {
            const currentTheme = root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
            const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
            localStorage.setItem('theme-preference', nextTheme);
            applyTheme(nextTheme);
        });
    }
})();
