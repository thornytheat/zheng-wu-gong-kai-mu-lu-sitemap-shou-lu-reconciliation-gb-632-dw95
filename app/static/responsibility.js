/* 地址责任账：登记变更、执行到期迁移、时点归属试算。判定全在后端，脚本只提交与回显。 */
(function () {
  const form = document.getElementById("resp-form");
  const notice = document.getElementById("resp-notice");
  const applyBtn = document.getElementById("apply-btn");
  const resolveBtn = document.getElementById("resolve-btn");
  const applyStatus = document.getElementById("apply-status");

  function show(text, kind) {
    SitemapAudit.notice(notice, text, kind);
  }

  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      try {
        const body = await SitemapAudit.json("/api/responsibility/changes", {
          method: "POST",
          body: JSON.stringify(data),
        });
        show("已登记 " + body.change.change_id + "（" + body.change.status + "），责任版本 " + body.version, "ok");
        window.setTimeout(() => window.location.reload(), 800);
      } catch (err) {
        show(err.message + "（" + err.code + "）", "bad");
      }
    });
  }

  if (applyBtn) {
    applyBtn.addEventListener("click", async () => {
      try {
        const body = await SitemapAudit.json("/api/responsibility/apply", {
          method: "POST",
          body: "{}",
        });
        const tail = "处理 " + body.processed + " 条，生效 " +
          body.activated.length + "，失败 " + body.failed.length;
        SitemapAudit.setStatus(applyStatus, tail, body.failed.length ? "warn" : "ok");
        show("迁移完成：" + tail + "；责任版本 " + body.version,
          body.failed.length ? "bad" : "ok");
        window.setTimeout(() => window.location.reload(), 900);
      } catch (err) {
        SitemapAudit.setStatus(applyStatus, "失败", "bad");
        show(err.message + "（" + err.code + "）", "bad");
      }
    });
  }

  if (resolveBtn) {
    resolveBtn.addEventListener("click", async () => {
      const url = form && form.elements.url ? form.elements.url.value.trim() : "";
      if (!url) {
        show("先在上方填写要试算的地址。", "bad");
        return;
      }
      try {
        const body = await SitemapAudit.json(
          "/api/responsibility/resolve?url=" + encodeURIComponent(url));
        show("该地址当前责任站：" + body.site + "（依据：" + body.basis_label +
          (body.ref ? " " + body.ref : "") + "）", "ok");
      } catch (err) {
        show(err.message + "（" + err.code + "）", "bad");
      }
    });
  }
})();
