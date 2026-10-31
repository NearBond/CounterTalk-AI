import { Link } from "react-router";

export function NotFoundPage() {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-10">
      <h1 className="text-2xl font-semibold">Page not found</h1>
      <p className="text-gray-600">There's nothing at this address.</p>
      <Link to="/" className="text-blue-700 underline">
        Go to the home page
      </Link>
    </main>
  );
}
