/* Thin fetch wrapper: every request carries initData in the tma auth header. */
/* global Telegram */
(function () {
  const webApp = Telegram.WebApp;

  function headers() {
    return {
      "Content-Type": "application/json",
      Authorization: "tma " + (webApp.initData || ""),
    };
  }

  async function request(method, path, params, body) {
    const url = new URL(path, window.location.origin);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== "" && v !== null && v !== undefined) url.searchParams.set(k, v);
    });
    const resp = await fetch(url, {
      method,
      headers: headers(),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try {
      data = await resp.json();
    } catch (err) {
      /* empty body */
    }
    if (!resp.ok) {
      const err = new Error((data && data.error) || "Ошибка сети (" + resp.status + ")");
      err.status = resp.status;
      err.payload = data;
      throw err;
    }
    return data;
  }

  window.API = {
    me: () => request("GET", "/api/me"),
    listTickets: (p) => request("GET", "/api/tickets", p),
    ticket: (id) => request("GET", "/api/tickets/" + id),
    reopen: (id, version) => request("POST", "/api/tickets/" + id + "/reopen", null, { expected_version: version }),
    reassign: (id, version, target) =>
      request("POST", "/api/tickets/" + id + "/reassign", null, { expected_version: version, target_telegram_id: target }),
    restorePost: (id) => request("POST", "/api/tickets/" + id + "/restore_post", null, {}),
    users: (q, executorsOnly) => request("GET", "/api/users", { q, executors_only: executorsOnly ? "true" : "" }),
    setRole: (tgId, role) => request("PUT", "/api/users/" + tgId + "/role", null, { role }),
    webApp,
  };
})();
