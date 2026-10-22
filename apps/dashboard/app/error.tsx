"use client";

export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="alert bad" role="alert">
      <strong>Something went wrong.</strong>{" "}
      <button type="button" onClick={reset}>
        Try again
      </button>
    </div>
  );
}
