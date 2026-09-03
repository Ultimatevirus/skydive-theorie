(() => {
    const savedTheme = localStorage.getItem('theme-preference');
    if (savedTheme === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
    }
})();