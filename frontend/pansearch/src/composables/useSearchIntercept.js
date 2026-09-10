import {nextTick, ref} from "vue";

/**
 * 拦截平台顶部搜索框和移动端搜索弹窗，将搜索结果渲染在当前页面。
 */
export function useSearchIntercept({onSearch, onExit}) {
  const isSearchMode = ref(false);
  const searchKeyword = ref("");
  const currentSearchText = ref("");
  const searching = ref(false);

  let originalSearchPlaceholder = "";

  function closeTopSearchDialog() {
    const closeBtn = document.querySelector(
      ".search-bar-dialog .search-close-btn, .search-dialog .close-btn, .v-overlay--active .v-btn[aria-label=\"关闭\"], .search-bar-dialog button",
    );
    if (closeBtn) {
      closeBtn.click();
    } else {
      window.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", code: "Escape", bubbles: true}));
    }
  }

  async function handleSearch(keyword) {
    const kw = (typeof keyword === "string" ? keyword : searchKeyword.value)?.trim();
    if (!kw) return;
    searchKeyword.value = kw;
    searching.value = true;
    isSearchMode.value = true;
    currentSearchText.value = kw;
    await onSearch?.(kw);
    searching.value = false;
  }

  function handleTopSearchKeydown(e) {
    if (e.key === "Enter") {
      const target = e.target;
      if (
        target &&
        (target.matches?.(
            "header.layout-navbar input, #global-media-search, .search-bar-dialog input, .search-dialog input",
          ) ||
          target.closest?.("header.layout-navbar, .search-bar-dialog, .search-dialog"))
      ) {
        const kw = target.value?.trim();
        if (kw) {
          e.preventDefault();
          e.stopPropagation();
          e.stopImmediatePropagation();
          handleSearch(kw);
          closeTopSearchDialog();
        }
      }
    } else if (e.key === "Escape") {
      if (isSearchMode.value) {
        exitSearchMode();
      }
    }
  }

  function handleTopSearchClick(e) {
    const target = e.target;
    const submitBtn = target?.closest?.(
      ".search-bar-dialog .v-btn--icon, .search-dialog .v-btn, .search-input-wrapper .v-icon, .search-bar-dialog button",
    );
    if (submitBtn) {
      const input = document.querySelector(
        ".search-bar-dialog input, .search-dialog input, header.layout-navbar input, #global-media-search",
      );
      const kw = input?.value?.trim();
      if (kw) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        handleSearch(kw);
        closeTopSearchDialog();
      }
    }
  }

  function setup() {
    window.addEventListener("keydown", handleTopSearchKeydown, true);
    window.addEventListener("click", handleTopSearchClick, true);
    nextTick(() => {
      const topInput = document.querySelector("header.layout-navbar input, #global-media-search");
      if (topInput) {
        originalSearchPlaceholder = topInput.getAttribute("placeholder") || "";
        topInput.setAttribute("placeholder", "在网盘资源中搜索电影、剧集... (Ctrl+K)");
      }
    });
  }

  function cleanup() {
    window.removeEventListener("keydown", handleTopSearchKeydown, true);
    window.removeEventListener("click", handleTopSearchClick, true);
    const topInput = document.querySelector("header.layout-navbar input, #global-media-search");
    if (topInput && originalSearchPlaceholder) {
      topInput.setAttribute("placeholder", originalSearchPlaceholder);
    }
  }

  function exitSearchMode() {
    isSearchMode.value = false;
    searchKeyword.value = "";
    currentSearchText.value = "";
    onExit?.();
  }

  function clearSearch() {
    searchKeyword.value = "";
    if (isSearchMode.value) exitSearchMode();
  }

  return {
    isSearchMode,
    searchKeyword,
    currentSearchText,
    searching,
    handleSearch,
    exitSearchMode,
    clearSearch,
    setup,
    cleanup,
  };
}
