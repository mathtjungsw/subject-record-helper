(() => {
  const SESSION_KEY = "workbox.authenticated.v1";
  const PASSWORD_HASH = "01b4b091ee945c5c4cd653e3ca26ea62c1d74a9821d0ef8ecbc9cf3451ea8c2a";
  const root = document.documentElement;

  root.style.visibility = "hidden";

  const readSession = () => {
    try {
      return sessionStorage.getItem(SESSION_KEY) === PASSWORD_HASH;
    } catch (_) {
      return false;
    }
  };

  const saveSession = () => {
    try {
      sessionStorage.setItem(SESSION_KEY, PASSWORD_HASH);
    } catch (_) {}
  };

  const revealPage = () => {
    root.style.visibility = "visible";
  };

  const showLockedPage = () => {
    window.stop();
    root.innerHTML = `
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>WORKBOX · 잠김</title>
        <style>
          * { box-sizing: border-box; }
          body {
            display: grid;
            place-items: center;
            min-height: 100vh;
            margin: 0;
            color: #f1f3f5;
            background: #090a0c;
            font-family: Pretendard, "Noto Sans KR", system-ui, sans-serif;
          }
          main { text-align: center; }
          strong { display: block; font-size: 18px; }
          p { margin: 10px 0 0; color: #858b96; font-size: 12px; }
          button {
            margin-top: 22px;
            padding: 10px 16px;
            border: 1px solid #343942;
            border-radius: 9px;
            color: #c9ff63;
            background: #15181d;
            cursor: pointer;
          }
        </style>
      </head>
      <body>
        <main>
          <strong>WORKBOX 잠김</strong>
          <p>비밀번호 입력을 취소했습니다.</p>
          <button type="button" onclick="location.reload()">다시 입력</button>
        </main>
      </body>`;
    root.style.visibility = "visible";
  };

  const hashText = async (value) => {
    const bytes = new TextEncoder().encode(value);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) =>
      byte.toString(16).padStart(2, "0")
    ).join("");
  };

  if (readSession()) {
    revealPage();
    return;
  }

  (async () => {
    while (true) {
      const input = window.prompt("WORKBOX 비밀번호를 입력하세요.");

      if (input === null) {
        showLockedPage();
        return;
      }

      try {
        if ((await hashText(input)) === PASSWORD_HASH) {
          saveSession();
          revealPage();
          return;
        }
      } catch (_) {
        window.alert("이 브라우저에서는 비밀번호 확인 기능을 사용할 수 없습니다.");
        showLockedPage();
        return;
      }

      window.alert("비밀번호가 맞지 않습니다.");
    }
  })();
})();
