const form = document.querySelector("#loginForm");
const message = document.querySelector("#loginMessage");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  message.textContent = "로그인 중입니다.";
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: data.get("username"), password: data.get("password") }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "로그인에 실패했습니다.");
    window.location.assign("/");
  } catch (error) {
    message.textContent = error.message;
  }
});
