function openCitation(id) {
  const target = id ? document.getElementById("seg-" + id) : document.querySelector(".seg");
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.style.background = "#eff6ff";
  const audio = document.getElementById("meeting-audio");
  const start = Number(target.getAttribute("data-start") || 0);
  if (audio) {
    audio.currentTime = start / 1000;
    audio.play().catch(() => {});
  }
}

document.querySelectorAll(".citation").forEach((el) => {
  el.addEventListener("click", () => openCitation(el.getAttribute("data-segment")));
});

// Per-speaker viewpoints can cite several source turns.  They use links for
// keyboard accessibility but should retain the same seek-and-highlight
// behavior as decisions and action items.
document.querySelectorAll(".view-source").forEach((el) => {
  el.addEventListener("click", (event) => {
    event.preventDefault();
    const href = el.getAttribute("href") || "";
    openCitation(href.startsWith("#seg-") ? href.slice(5) : "");
  });
});
