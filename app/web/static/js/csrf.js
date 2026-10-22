(function () {
  function token() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  var orig = window.fetch;
  window.fetch = function (input, init) {
    init = init ? Object.assign({}, init) : {};
    var method = String(init.method || "GET").toUpperCase();
    if (["POST", "PUT", "PATCH", "DELETE"].indexOf(method) !== -1) {
      var headers = new Headers(init.headers || {});
      if (!headers.has("X-CSRFToken") && !headers.has("X-CSRF-Token")) {
        var t = token();
        if (t) headers.set("X-CSRFToken", t);
      }
      init.headers = headers;
    }
    return orig.call(this, input, init);
  };
})();
