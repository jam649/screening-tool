import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  ApiError,
  sessionQuery,
  setSession,
  type Session,
  type SetupStatus,
} from "./api";
import {
  INVITE_FRAGMENT,
  messageOf,
  useAcceptInvitation,
  validateInvitation,
  type InvitationPreview,
} from "./adminApi";
import { LocalTime } from "./CSLPanel";
import { Field } from "./SettingsControls";
import "./admin.css";

// undefined: no fragment seen; null: a fragment that is not a valid token.
let capturedToken: string | null | undefined;

function captureInviteFragment() {
  const { pathname, search, hash } = window.location;
  if (pathname !== "/accept-invite" || !hash) return;
  const match = INVITE_FRAGMENT.exec(hash);
  capturedToken = match ? match[1] : null;
  window.history.replaceState(window.history.state, "", pathname + search);
}
// Runs when the bundle loads, before any query starts, so the secret leaves the address bar first.
captureInviteFragment();

type Check =
  | { status: "missing" }
  | { status: "checking" }
  | { status: "valid"; preview: InvitationPreview }
  | { status: "invalid"; message: string }
  | { status: "error"; message: string }
  | { status: "done" };

const ROLE_LABELS: Record<string, string> = { admin: "Admin", reviewer: "Reviewer" };

export function AcceptInvite({ setup }: { setup: SetupStatus }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const accept = useAcceptInvitation();
  const current = useQuery({
    queryKey: ["session"],
    queryFn: sessionQuery,
    enabled: false,
  });
  const [token, setToken] = useState<string | null>(() => {
    captureInviteFragment();
    return capturedToken ?? null;
  });
  const [check, setCheck] = useState<Check>(() =>
    token ? { status: "checking" } : { status: "missing" },
  );
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [nameError, setNameError] = useState("");
  const started = useRef(false);

  const run = useCallback((value: string) => {
    validateInvitation(value).then(
      (preview) => setCheck({ status: "valid", preview }),
      (error: unknown) =>
        setCheck(
          error instanceof ApiError && [400, 422].includes(error.status)
            ? { status: "invalid", message: error.message }
            : {
                status: "error",
                message: messageOf(error, "Something went wrong."),
              },
        ),
    );
  }, []);

  // The token now lives only in this component's state.
  useEffect(() => {
    capturedToken = undefined;
  }, []);
  // Validate once; trying again is always an explicit choice.
  useEffect(() => {
    if (!token || started.current) return;
    started.current = true;
    run(token);
  }, [token, run]);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!token || check.status !== "valid") return;
    const name = displayName.trim();
    if (!name) {
      setNameError("Enter your name.");
      return;
    }
    setNameError("");
    accept.mutate(
      { token, display_name: name, password },
      {
        onSuccess: (session: Session) => {
          setSession(session);
          client.removeQueries({
            predicate: (query) =>
              !["setup", "session"].includes(String(query.queryKey[0])),
          });
          client.setQueryData(["session"], session);
          client.setQueryData<SetupStatus>(
            ["setup"],
            (prev) => prev && { ...prev, needs_setup: false },
          );
          void client.invalidateQueries({ queryKey: ["setup"] });
          setToken(null);
          setPassword("");
          setCheck({ status: "done" });
          accept.reset();
          navigate("/", { replace: true });
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 400) {
            setToken(null);
            setPassword("");
            setCheck({ status: "invalid", message: error.message });
          }
        },
      },
    );
  }

  let body;
  if (check.status === "missing")
    body = (
      <>
        <h2>This invitation link isn’t valid.</h2>
        <p className="lede">
          The link is incomplete or was changed. Ask your workspace
          administrator for a new invitation link.
        </p>
      </>
    );
  else if (check.status === "checking")
    body = (
      <>
        <h2>Checking your invitation…</h2>
        <p className="lede" role="status">
          Please wait while we confirm the link.
        </p>
      </>
    );
  else if (check.status === "invalid")
    body = (
      <>
        <h2>This invitation can’t be used.</h2>
        <div className="notice danger" role="alert">
          {check.message}
        </div>
        <p className="lede">
          Invitation links work once and expire. Ask your workspace
          administrator for a new one.
        </p>
      </>
    );
  else if (check.status === "error")
    body = (
      <>
        <h2>We couldn’t check your invitation.</h2>
        <div className="notice danger" role="alert">
          {check.message}
        </div>
        <button
          type="button"
          className="button primary"
          onClick={() => {
            if (!token) return;
            setCheck({ status: "checking" });
            run(token);
          }}
        >
          Try again
        </button>
      </>
    );
  else if (check.status === "done")
    body = (
      <h2 role="status">Opening your workspace…</h2>
    );
  else {
    const { preview } = check;
    const role = ROLE_LABELS[preview.role] ?? preview.role;
    body = (
      <>
        <h2>Create your account.</h2>
        <p className="lede">
          You’ve been invited to join as {role === "Admin" ? "an" : "a"}{" "}
          {role}.
        </p>
        <dl className="detail-list admin-accept-summary">
          <div>
            <dt>Email</dt>
            <dd>{preview.email}</dd>
          </div>
          <div>
            <dt>Role</dt>
            <dd>{role}</dd>
          </div>
          <div>
            <dt>Expires</dt>
            <dd>
              <LocalTime value={preview.expires_at} />
            </dd>
          </div>
        </dl>
        {current.data && (
          <p className="admin-warning">
            You’re signed in as {current.data.user.email}. Creating this
            account signs this browser in as {preview.email} instead.
          </p>
        )}
        <form onSubmit={submit}>
          <fieldset className="admin-fieldset" disabled={accept.isPending}>
            <div className="form-fields">
              <Field label="Your name" error={nameError}>
                {(props) => (
                  <input
                    {...props}
                    autoComplete="name"
                    value={displayName}
                    onChange={(event) => setDisplayName(event.target.value)}
                    required
                    maxLength={200}
                    autoFocus
                  />
                )}
              </Field>
              <Field
                label="Password"
                help="Use at least 12 characters. A few memorable words work well."
              >
                {(props) => (
                  <input
                    {...props}
                    type="password"
                    autoComplete="new-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                    minLength={12}
                    maxLength={1024}
                  />
                )}
              </Field>
            </div>
            {accept.isError && (
              <div className="notice danger" role="alert">
                {messageOf(accept.error, "Your account could not be created.")}
              </div>
            )}
            <div className="form-actions">
              <button className="button primary" type="submit">
                {accept.isPending ? "Please wait…" : "Create account"}
                <span aria-hidden="true">→</span>
              </button>
            </div>
          </fieldset>
        </form>
        <details className="admin-details">
          <summary>About using the Consolidated Screening List</summary>
          <blockquote className="disclaimer">{setup.disclaimer}</blockquote>
          <a
            className="text-link"
            href={setup.disclaimer_url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Read the ITA guidance <span aria-hidden="true">↗</span>
          </a>
        </details>
      </>
    );
  }

  return (
    <main className="auth-layout">
      <aside className="auth-aside">
        <span className="brand">
          <span className="brand-symbol" aria-hidden="true">
            s/
          </span>
          {setup.product_name}
        </span>
        <div className="auth-statement">
          <span className="eyebrow light">An invitation to review</span>
          <h1>
            Join your
            <br />
            screening workspace.
          </h1>
          <p>
            Review possible matches with your team and keep a clear record of
            every decision.
          </p>
        </div>
        <div className="auth-footnote">
          <span className="status-dot" /> Self-hosted. Built for human review.
        </div>
      </aside>
      <section className="auth-main" aria-label="Accept invitation">
        <div className="auth-form-wrap">
          <div className="eyebrow">Your invitation</div>
          {body}
        </div>
        <footer className="auth-footer">
          Built for a thoughtful compliance process.
        </footer>
      </section>
    </main>
  );
}
