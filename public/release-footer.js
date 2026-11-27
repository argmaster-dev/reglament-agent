(async () => {
  const root = document.getElementById("root");
  if (!root) return;

  const response = await fetch("/ui/release-footer", {cache: "no-store"});
  if (!response.ok) return;
  const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
  const footer = documentFragment.querySelector("footer[data-release-footer]");
  if (!footer) return;

  // Chainlit can recreate the composer when switching the active thread.
  const show = () => {
    const watermark = root.querySelector(".watermark");
    if (watermark && !watermark.nextElementSibling?.matches("[data-release-footer]")) {
      watermark.after(document.importNode(footer, true));
    }
  };
  show();
  new MutationObserver(show).observe(root, {childList: true, subtree: true});
})().catch(() => {});
