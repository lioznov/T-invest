document.addEventListener("DOMContentLoaded", () => {
    // Эффект прилипания и тени для шапки сайта при прокрутке
    const header = document.getElementById("header");

    window.addEventListener("scroll", () => {
        if (window.scrollY > 20) {
            header.style.background = "rgba(255, 255, 255, 0.95)";
            header.style.boxShadow = "0 10px 30px rgba(0,0,0,0.08)";

            // ИСПРАВЛЕНО: Сужаем шапку при скролле, но сохраняем боковые отступы 25px!
            header.style.padding = "10px 25px";
        } else {
            // Возвращаем исходное состояние
            header.style.background = "rgba(255, 255, 255, 0.65)";
            header.style.boxShadow = "none";
            header.style.padding = "15px 25px";
        }
    });
});