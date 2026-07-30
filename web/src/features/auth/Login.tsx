/** Sign-in form. */

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, api } from "../../api/client";

export function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const login = useMutation({
    mutationFn: () => api.login(username, password),
    onSuccess: onSignedIn,
  });

  return (
    <section className="page narrow">
      <h1>Sign in</h1>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          login.mutate();
        }}
      >
        <label>
          Username
          <input
            value={username}
            autoComplete="username"
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {login.error && (
          <p className="tone-danger" role="alert">
            {login.error instanceof ApiError
              ? login.error.message
              : "Sign in failed"}
          </p>
        )}
        <button
          type="submit"
          className="button primary"
          disabled={login.isPending}
        >
          Sign in
        </button>
      </form>
    </section>
  );
}
