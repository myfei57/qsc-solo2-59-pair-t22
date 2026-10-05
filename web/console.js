/* 控制台页面共享的取数与渲染基础。
 *
 * 所有页面每轮只拉一次 /api/view：它返回覆盖全部读接口的同一份不可变快照，
 * 顶部带 revision。页面渲染只读这一份数据，因此同一张表上的字段不可能分别
 * 落在两个瞬间。后续如果还要单独调读接口，把 at=<revision> 带上即可钉住同一
 * 代；钉到已过期版本（409 snapshot_stale）时重新取最新视图再渲染。
 */
(function () {
  "use strict";

  async function fetchView() {
    const response = await fetch("/api/view");
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw Object.assign(new Error(detail.message || ("读取失败 " + response.status)), {
        status: response.status,
        detail,
      });
    }
    return response.json();
  }

  function showError(targetId, error) {
    const node = document.getElementById(targetId);
    if (!node) return;
    const detail = error.detail || {};
    const where = detail.field ? `（参数 ${detail.field}）` : "";
    node.textContent = `${error.message}${where}`;
    node.classList.add("error");
  }

  function startPolling(render, intervalMs) {
    let timer = null;
    async function tick() {
      try {
        render(await fetchView());
      } catch (error) {
        // 取数失败时保留上一帧，不把页面清空。
        console.warn("view load failed", error);
      }
    }
    tick();
    timer = setInterval(tick, intervalMs || 5000);
    return { tick, stop: () => clearInterval(timer) };
  }

  window.ConsoleData = { fetchView, startPolling, showError };
})();
