import { type FormEvent, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { RetryAlert } from "../components/RetryAlert";
import { messageOf, signIn } from "../lib/api";
import { HOME } from "../lib/session";

/** Only our own pages, never sign-in itself: an address from elsewhere ("//evil.example", or
 * "/\\evil.example", which a browser reads as the same) goes home instead. */
function ownPath(next: string | null): string | null {
  if (next === null || !/^\/(?![/\\])/.test(next) || next.startsWith("/signin")) return null;
  return next;
}

/** `/signin` (batch 16): email and password, and one line saying what went wrong. The server's
 * words: "Wrong email or password" whatever was wrong, or how long to wait after too many. */
export function SignInPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setFailed(null);
    try {
      await signIn({ email, password });
      navigate(ownPath(params.get("next")) ?? HOME, { replace: true });
    } catch (error) {
      setFailed(messageOf(error, "Signing in didn't work. Try again."));
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
      <h1 className="text-2xl font-semibold">Sign in</h1>
      <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1">
          <span className="font-medium">Email</span>
          <input
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="rounded-md border border-gray-300 px-3 py-2"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="font-medium">Password</span>
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="rounded-md border border-gray-300 px-3 py-2"
          />
        </label>
        <button
          type="submit"
          disabled={busy}
          className="self-start rounded-md bg-blue-700 px-4 py-2 font-medium text-white disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {failed !== null && <RetryAlert>{failed}</RetryAlert>}
      </form>
    </main>
  );
}
