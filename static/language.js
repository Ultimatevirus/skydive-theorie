(() => {
    const setOpen = (picker, isOpen) => {
        const trigger = picker.querySelector('.language-trigger');
        picker.classList.toggle('is-open', isOpen);
        if (trigger) trigger.setAttribute('aria-expanded', String(isOpen));
    };

    document.addEventListener('click', (event) => {
        const option = event.target.closest('.language-option[data-language]');
        if (option) {
            const picker = option.closest('.language-picker');
            const input = picker && picker.querySelector('input[name="language"]');
            if (input && input.form) {
                input.value = option.dataset.language;
                input.form.submit();
            }
            return;
        }

        const trigger = event.target.closest('.language-trigger');
        if (trigger) {
            const picker = trigger.closest('.language-picker');
            event.stopPropagation();
            setOpen(picker, !picker.classList.contains('is-open'));
            return;
        }

        document.querySelectorAll('.language-picker.is-open').forEach((picker) => {
            if (!picker.contains(event.target)) setOpen(picker, false);
        });
    });

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        document.querySelectorAll('.language-picker.is-open').forEach((picker) => setOpen(picker, false));
    });
})();
