import { type FormEvent, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { RetryAlert } from "../components/RetryAlert";
import { getDemoAccounts, messageOf, signIn } from "../lib/api";
import { usePolling } from "../lib/hooks";
import { HOME } from "../lib/session";

/** Only our own pages, never sign-in itself: an address from elsewhere ("//evil.example", or
 * "/\\evil.example", which a browser reads as the same) goes home instead. */
function ownPath(next: string | null): string | null {
  if (next === null || !/^\/(?![/\\])/.test(next) || next.startsWith("/signin")) return null;
  return next;
}

/** `/signin` (batch 16): email and password, and one line saying what went wrong. The server's
 * words: "Wrong email or password" whatever was wrong, or how long to wait after too many. Below
 * it (batch F9), the demo accounts and their password, when the host lists them: tapping one
 * fills in its email. */
export function SignInPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const demo = usePolling((signal) => getDemoAccounts(signal), { key: "demo-accounts", shouldPoll: () => false });
  const accounts = demo.data?.accounts ?? [];

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
      {accounts.length > 0 && (
        <section aria-labelledby="demo-accounts" className="flex flex-col gap-2 rounded-md border border-gray-300 p-3">
          <h2 id="demo-accounts" className="text-lg font-semibold">
            Demo accounts
          </h2>
          <p className="text-gray-700">
            Every demo account's password is <span className="font-mono font-medium">{demo.data?.password}</span>
          </p>
          <p className="text-sm text-gray-600">Tap an account to fill in its email.</p>
          <ul aria-label="Demo accounts" className="flex flex-col gap-1">
            {accounts.map((account) => (
              <li key={account.email}>
                <button
                  type="button"
                  onClick={() => setEmail(account.email)}
                  className="flex w-full flex-col items-start rounded-md px-2 py-1.5 text-left hover:bg-gray-100 focus-visible:outline-2"
                >
                  <span className="font-medium break-all">{account.email}</span>
                  <span className="text-sm text-gray-700">
                    {account.label}
                    {account.note !== null && ` · ${account.note}`}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
