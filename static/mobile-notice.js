(() => {
    const noticeId = 'mobile-notice';
    const dismissedKey = 'mobile-notice-dismissed';
    const phoneViewport = window.matchMedia('(max-width: 768px)');
    let dismissed = false;
    let previousFocus = null;
    let backgroundStates = [];

    const setBackgroundInert = (inert) => {
        if (inert) {
            backgroundStates = Array.from(document.body.children)
                .filter((element) => element.id !== noticeId)
                .map((element) => [element, element.inert]);
            backgroundStates.forEach(([element]) => {
                element.inert = true;
            });
            return;
        }

        backgroundStates.forEach(([element, wasInert]) => {
            element.inert = wasInert;
        });
        backgroundStates = [];
    };

    const focusableElements = (notice) => Array.from(notice.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
        'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter((element) => !element.hidden);

    const hideNotice = (notice, restoreFocus = true) => {
        notice.hidden = true;
        setBackgroundInert(false);
        if (restoreFocus && previousFocus?.isConnected) previousFocus.focus();
        previousFocus = null;
    };

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
            previousFocus = document.activeElement === document.body
                ? document.querySelector('.brand-link')
                : document.activeElement;
            setBackgroundInert(true);
            focusableElements(notice)[0]?.focus();
        } else if (!shouldShow && !wasHidden) {
            hideNotice(notice);
        }
    };

    const dismissNotice = () => {
        dismissed = true;
        const notice = document.getElementById(noticeId);
        if (notice) hideNotice(notice);

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
        const notice = document.getElementById(noticeId);
        if (!notice || notice.hidden) return;

        if (event.key === 'Escape') {
            event.preventDefault();
            dismissNotice();
            return;
        }

        if (event.key === 'Tab') {
            const focusable = focusableElements(notice);
            if (focusable.length === 0) {
                event.preventDefault();
                return;
            }

            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            const focusIsOutside = !notice.contains(document.activeElement);
            if (event.shiftKey && (document.activeElement === first || focusIsOutside)) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && (document.activeElement === last || focusIsOutside)) {
                event.preventDefault();
                first.focus();
            }
        }
    });

    document.addEventListener('focusin', (event) => {
        const notice = document.getElementById(noticeId);
        if (notice && !notice.hidden && !notice.contains(event.target)) {
            focusableElements(notice)[0]?.focus();
        }
    });

    document.addEventListener('turbo:before-cache', () => {
        const notice = document.getElementById(noticeId);
        if (notice && !notice.hidden) hideNotice(notice, false);
    });

    document.addEventListener('DOMContentLoaded', updateNotice);
    document.addEventListener('turbo:load', updateNotice);
    phoneViewport.addEventListener('change', updateNotice);
})();
