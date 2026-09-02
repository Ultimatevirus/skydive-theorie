(() => {
    document.querySelectorAll('.language-picker').forEach((picker) => {
        const trigger = picker.querySelector('.language-trigger');
        const menu = picker.querySelector('.language-menu');
        const input = picker.querySelector('input[name="language"]');

        if (!trigger || !menu || !input) return;

        const setOpen = (isOpen) => {
            picker.classList.toggle('is-open', isOpen);
            trigger.setAttribute('aria-expanded', String(isOpen));
        };

        trigger.addEventListener('click', (event) => {
            event.stopPropagation();
            setOpen(!picker.classList.contains('is-open'));
        });

        menu.querySelectorAll('[data-language]').forEach((option) => {
            option.addEventListener('click', () => {
                input.value = option.dataset.language;
                input.form.submit();
            });
        });

        document.addEventListener('click', (event) => {
            if (!picker.contains(event.target)) setOpen(false);
        });

        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') setOpen(false);
        });
    });
})();
