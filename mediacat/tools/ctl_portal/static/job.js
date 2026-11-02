// Polls the job until it finishes and streams its output into the log box.
(function () {
  const box = document.getElementById("job");
  const id = box.dataset.id;
  const $ = (x) => document.getElementById(x);
  const badge = { done: "ok", failed: "bad", cancelled: "off", running: "warn" };
  async function tick() {
    let j;
    try { j = await (await fetch(`/api/jobs/${id}`)).json(); } catch (e) { return setTimeout(tick, 2000); }
    const log = $("log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.textContent = (j.truncated ? `… ${j.truncated} earlier lines in the job log file\n` : "") + j.log.join("\n");
    if (atBottom) log.scrollTop = log.scrollHeight;
    $("elapsed").textContent = `${Math.round(j.elapsed_s)} s`;
    $("status").textContent = j.status;
    $("status").className = "badge " + (badge[j.status] || "off");
    if (j.status === "running") return setTimeout(tick, 1000);
    $("cancel").classList.add("hidden");
    document.querySelector(".spinner").classList.add("hidden");
    $("stage-text").textContent =
      j.status === "done" ? "Finished." :
      j.status === "cancelled" ? "Cancelled." : `Failed (exit code ${j.exit_code}).`;
  }
  tick();
})();
