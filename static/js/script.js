document.addEventListener("DOMContentLoaded", () => {
    // Состояние прокрутки меняет только класс: вся визуальная тема остаётся в CSS.
    const header = document.getElementById("header");
    if (!header) return;

    const syncHeaderState = () => {
        header.classList.toggle("is-scrolled", window.scrollY > 20);
    };

    window.addEventListener("scroll", syncHeaderState, { passive: true });
    syncHeaderState();
});
