(() => {
    const noticeId = 'mobile-notice';
    const dismissedKey = 'mobile-notice-dismissed';
    const phoneViewport = window.matchMedia('(max-width: 768px)');
    let dismissed = false;

    try {
        dismissed = sessionStorage.getItem(dismissedKey) === 'true';
    } catch (error) {
        console.warn('Could not read the mobile notice preference.', error);
    }

    const updateNotice = () => {
        const notice = document.getElementById(noticeId);
        if (!notice) return;

        const shouldShow = phoneViewport.matches && !dismissed;
        const wasHidden = notice.hidden;
        notice.hidden = !shouldShow;

        if (shouldShow && wasHidden) {
            notice.querySelector('[data-mobile-notice-dismiss]')?.focus();
        }
    };

    const dismissNotice = () => {
        dismissed = true;
        const notice = document.getElementById(noticeId);
        if (notice) notice.hidden = true;

        try {
            sessionStorage.setItem(dismissedKey, 'true');
        } catch (error) {
            console.warn('Could not save the mobile notice preference.', error);
        }
    };

    document.addEventListener('click', (event) => {
        if (event.target.closest('[data-mobile-notice-dismiss]')) dismissNotice();
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !document.getElementById(noticeId)?.hidden) {
            dismissNotice();
        }
    });

    document.addEventListener('DOMContentLoaded', updateNotice);
    document.addEventListener('turbo:load', updateNotice);
    phoneViewport.addEventListener('change', updateNotice);
})();
