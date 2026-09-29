// İlk boyamadan önce temayı uygula (beyaz/siyah yanıp sönmesin). CSP inline script'e izin vermediği için ayrı dosya.
(function () {
  try {
    var t = localStorage.getItem("mp-theme");
    if (t !== "light" && t !== "dark") t = "dark";
    document.documentElement.classList.toggle("dark", t === "dark");
  } catch (e) {
    document.documentElement.classList.add("dark");
  }
})();
