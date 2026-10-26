(() => {
    const root = document.documentElement;

    function applyTheme(theme) {
        const isDark = theme === 'dark';
        root.setAttribute('data-theme', isDark ? 'dark' : 'light');
    }

    function updateThemeToggleLabel() {
        const themeToggle = document.querySelector('.theme-toggle');
        if (!themeToggle) return;

        const isDark = root.getAttribute('data-theme') === 'dark';
        themeToggle.setAttribute(
            'aria-label',
            isDark ? themeToggle.dataset.lightLabel : themeToggle.dataset.darkLabel,
        );
    }

    applyTheme(localStorage.getItem('theme-preference') || 'light');
    updateThemeToggleLabel();
    document.addEventListener('turbo:load', updateThemeToggleLabel);

    document.addEventListener('click', (event) => {
        if (!event.target.closest('.theme-toggle')) return;

        const currentTheme = root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
        localStorage.setItem('theme-preference', nextTheme);
        applyTheme(nextTheme);
        updateThemeToggleLabel();
    });
})();
